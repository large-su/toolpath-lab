"""前置分层粗加工、目标曲面保护与时间轴安全拐点。"""

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.simulation import StockState, build_timeline, stock_spec_for


def plan(enabled=True, planner="raster", roughing=None, surface=None, region=None):
    return run_plan(planner_id=planner,
                    tool=Tool(ToolKind.BALL, diameter_mm=6, length_mm=30),
                    region=region or build_region("square", {"side_mm": 40}),
                    surface=surface or build_surface("freeform", {"amplitude_mm": 4}),
                    roughing={"enabled": enabled, **(roughing or {})})


class RoughingTests(unittest.TestCase):
    def test_disabled_preserves_original_toolpath(self):
        off = plan(False).toolpath
        default = run_plan(planner_id="raster", tool=Tool(ToolKind.BALL, 6, 30),
                           region=build_region("square", {"side_mm": 40}),
                           surface=build_surface("freeform", {"amplitude_mm": 4})).toolpath
        self.assertNotIn("roughing", off.metadata)
        self.assertEqual(len(off.moves), len(default.moves))
        for a, b in zip(off.moves, default.moves):
            np.testing.assert_array_equal(a.points, b.points)
            self.assertEqual(a.kind, b.kind)

    def test_all_finish_strategies_keep_their_original_points_and_axes(self):
        for planner in ("raster", "crosshatch", "five_axis", "adaptive_scallop", "five_axis_adaptive"):
            with self.subTest(planner=planner):
                off = plan(False, planner).toolpath
                on = plan(True, planner).toolpath
                meta = on.metadata["roughing"]
                finish = on.moves[meta["finish_start_move_index"]:]
                self.assertEqual(len(finish), len(off.moves))
                for a, b in zip(finish, off.moves):
                    np.testing.assert_array_equal(a.points, b.points)
                    np.testing.assert_array_equal(a.tool_axes, b.tool_axes)
                    self.assertEqual(a.feed_mm_per_min, b.feed_mm_per_min)
                self.assertEqual(on.pass_count, off.pass_count + meta["pass_count"])
                if planner in ("adaptive_scallop", "five_axis_adaptive"):
                    self.assertEqual(on.metadata["adaptive"], off.metadata["adaptive"])

    def test_descending_layers_limit_depth_and_keep_remaining_allowance(self):
        result = plan()
        path = result.toolpath
        meta = path.metadata["roughing"]
        self.assertEqual(meta["layer_count"], 5)
        levels = [item["z_mm"] for item in meta["layers"]]
        self.assertTrue(np.all(np.diff(levels) < 0))
        self.assertLessEqual(max(-np.diff([meta["initial_top_z_mm"]] + levels)), meta["depth_mm"] + 1e-8)
        self.assertAlmostEqual(levels[-1], -3.5)
        previous = None
        for layer in meta["layers"]:
            scans = [m for m in path.moves[layer["start_move_index"]:layer["end_move_index"] + 1]
                     if m.pass_index >= 0]
            if previous is not None:
                for a, b in zip(previous, scans):
                    delta = a.points[:, 2] - b.points[:, 2]
                    self.assertTrue(np.all(delta >= -1e-9))
                    self.assertTrue(np.all(delta <= meta["depth_mm"] + 1e-9))
            previous = scans

    def test_requested_depth_is_capped_to_teaching_cutting_length(self):
        result = plan(roughing={"depth_mm": 20})
        self.assertAlmostEqual(result.toolpath.metadata["roughing"]["depth_mm"], 2.4)
        self.assertTrue(any("限制" in note for note in result.warnings))

    def test_roughing_uses_vertical_axes_and_feed_approaches(self):
        path = plan(planner="five_axis").toolpath
        for move in path.moves[:path.metadata["roughing"]["finish_start_move_index"] - 1]:
            self.assertTrue(np.allclose(move.tool_axes, [0, 0, 1]))
            if "进刀" in move.label:
                self.assertEqual(move.kind, MoveKind.CUT)
                self.assertEqual(move.points[0, :2].tolist(), move.points[-1, :2].tolist())
                self.assertGreater(move.points[0, 2], move.points[-1, 2])

    def test_roughing_material_simulation_preserves_target_curve(self):
        surface = build_surface("freeform", {"amplitude_mm": 4})
        region = build_region("square", {"side_mm": 40})
        tool = Tool(ToolKind.BALL, 6, 30)
        path = plan(surface=surface, region=region).toolpath
        state = StockState(stock_spec_for(region, surface, tool))
        before = state.remaining_volume_mm3()
        for move in path.moves[:path.metadata["roughing"]["finish_start_move_index"]]:
            if move.kind is MoveKind.CUT:
                for a, b in zip(move.points[:-1], move.points[1:]):
                    state.remove_tool_segment(a, b)
        target = surface.height_at(np.column_stack((state.xx.ravel(), state.yy.ravel()))).reshape(state.heights.shape)
        self.assertLess(state.remaining_volume_mm3(), before)
        self.assertTrue(np.all(state.heights[state.active] >= target[state.active] + 0.4))

    def test_rough_finish_boundary_is_connected_with_safe_transfer(self):
        path = plan(planner="five_axis").toolpath
        for a, b in zip(path.moves[:-1], path.moves[1:]):
            np.testing.assert_allclose(a.points[-1], b.points[0], atol=1e-8)
            np.testing.assert_allclose(a.tool_axes[-1], b.tool_axes[0], atol=1e-8)
        transfer = path.moves[path.metadata["roughing"]["finish_start_move_index"] - 1]
        self.assertEqual(transfer.kind, MoveKind.RAPID)
        self.assertGreaterEqual(float(transfer.points[1, 2]), 10)

    def test_layer_and_point_limits_fail_with_actionable_error(self):
        with self.assertRaisesRegex(PlanningError, "层上限"):
            plan(roughing={"depth_mm": 0.1}, surface=build_surface("freeform", {"amplitude_mm": 30}))
        with self.assertRaisesRegex(PlanningError, "刀点过多"):
            run_plan(planner_id="raster", tool=Tool(ToolKind.BALL, 1, 30),
                     region=build_region("square", {"side_mm": 1000}), roughing={"enabled": True})

    def test_no_roughing_layers_if_allowance_exceeds_stock(self):
        result = plan(surface=build_surface("flat", {}), roughing={"allowance_mm": 2})
        self.assertEqual(result.toolpath.metadata["roughing"]["layer_count"], 0)
        self.assertTrue(any("未添加" in warning for warning in result.warnings))

    def test_invalid_settings_and_request_roundtrip(self):
        for rough in ({"enabled": "nonsense"}, {"depth_mm": 0}, {"allowance_mm": -1}):
            with self.subTest(rough=rough), self.assertRaises(ParameterError):
                PlanRequest.from_payload({"roughing": rough})
        request = PlanRequest.from_payload({"roughing": {"enabled": True, "depth_mm": 1.5}})
        self.assertEqual(request.to_payload()["roughing"], {"enabled": True, "depth_mm": 1.5, "allowance_mm": 0.5})

    def test_timeline_keeps_rapid_corners_even_under_tiny_sample_budget(self):
        move = retract_move(np.array([0, 0, 0]), np.array([10, 0, 0]), 10, 5000)
        path = Toolpath((move,))
        timeline = build_timeline(path, max_samples=2)
        self.assertEqual(timeline.sample_count, 4)
        np.testing.assert_array_equal(timeline.positions, move.points)
        self.assertAlmostEqual(timeline.duration_s, path.estimated_time_s)

    def test_roughing_envelope_vertices_and_axes_survive_compression(self):
        move = Move(MoveKind.CUT, np.array([[0, 0, 0], [1, 0, 5], [2, 0, 0]]), 600,
                    tool_axes=np.array([[0, 0, 1], [1, 0, 1], [0, 0, 1]]), preserve_vertices=True)
        timeline = build_timeline(Toolpath((move,)), max_samples=2)
        np.testing.assert_array_equal(timeline.positions, move.points)
        np.testing.assert_allclose(timeline.tool_axes, move.tool_axes)

    def test_gcode_includes_roughing_and_final_toolpath(self):
        path = plan().toolpath
        gcode = toolpath_to_gcode(path)
        self.assertIn("先分层粗加工", gcode)
        self.assertEqual(sum(line.startswith(("G0 ", "G1 ")) for line in gcode.splitlines()), path.point_count)


if __name__ == "__main__":
    unittest.main()
