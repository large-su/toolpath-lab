"""受限刀轴滤波、真实转向时间和五轴安全连接。"""
from __future__ import annotations

import re
import unittest
import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.mathutil import axis_angle_deg, slerp_axis
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan
from toolpath_lab.planning.base import PlanningContext
from toolpath_lab.planning.crosshatch import _passes_for_direction
from toolpath_lab.planning.five_axis import _smooth_axes
from toolpath_lab.simulation import build_timeline


class OrientationSmoothingTests(unittest.TestCase):
    def plan(self, enabled=True, kind=ToolKind.BALL, rough=False, **options):
        params = {"smooth_orientation": enabled, "lead_deg": 18, "side_tilt_deg": 5,
                  "feed_mm_per_min": 1500, "max_angular_speed_deg_s": 10,
                  "smoothing_span_mm": 8, "max_axis_deviation_deg": 5, **options}
        return run_plan(planner_id="five_axis", tool=Tool(kind, 6, 40),
                        region=build_region("square", {"side_mm": 50}),
                        surface=build_surface("composite", {"secondary_wavelength_x_mm": 18,
                                                             "secondary_wavelength_y_mm": 20}),
                        parameters=params, roughing={"enabled": rough})

    def test_slerp_has_constant_angle_and_normalized_antipodal_midpoint(self):
        a, b = [0, 0, 1], [1, 0, 0]
        for t in [0.1, 0.25, 0.7, 1]:
            self.assertAlmostEqual(axis_angle_deg(a, slerp_axis(a, b, t)), 90 * t, places=7)
        opposite = slerp_axis(a, [0, 0, -1], 0.5)
        self.assertAlmostEqual(np.linalg.norm(opposite), 1)
        self.assertAlmostEqual(axis_angle_deg(a, opposite), 90)

    def test_constant_axis_is_not_artificially_animated(self):
        xy = np.column_stack((np.linspace(0, 10, 50), np.zeros(50)))
        axes = np.repeat([[0, 0, 1]], len(xy), axis=0)
        np.testing.assert_allclose(_smooth_axes(xy, axes, 6, 5), axes, atol=1e-12)

    def test_smoothing_deviation_is_bounded_and_axes_still_vary(self):
        raw = self.plan(False).toolpath
        filtered = self.plan().toolpath
        raw_cuts = [m for m in raw.moves if m.kind is MoveKind.CUT]
        cuts = [m for m in filtered.moves if m.kind is MoveKind.CUT]
        self.assertEqual(len(raw_cuts), len(cuts))
        deviations = [axis_angle_deg(a, b) for m, n in zip(raw_cuts, cuts)
                      for a, b in zip(m.tool_axes, n.tool_axes)]
        self.assertLessEqual(max(deviations), 5.000001)
        self.assertGreater(max(deviations), 0.1)
        self.assertGreater(np.ptp(cuts[0].tool_axes, axis=0).max(), 0.1)
        for original, smooth in zip(raw_cuts, cuts):
            np.testing.assert_allclose(original.points, smooth.points, atol=1e-12)
            np.testing.assert_allclose(np.linalg.norm(smooth.tool_axes, axis=1), 1, atol=1e-12)
        stats = filtered.metadata["orientation_smoothing"]
        self.assertLess(stats["smoothed_peak_gradient_deg_mm"], stats["raw_peak_gradient_deg_mm"])

    def test_flat_tool_contact_clearance_is_recomputed_for_smoothed_axes(self):
        result = self.plan(kind=ToolKind.FLAT)
        tool = Tool(ToolKind.FLAT, 6, 40)
        surface = build_surface("composite", {"secondary_wavelength_x_mm": 18, "secondary_wavelength_y_mm": 20})
        context = PlanningContext(tool, build_region("square", {"side_mm": 50}), {"feed_mm_per_min": 1500}, surface=surface)
        actual_cuts = [m for m in result.toolpath.moves if m.kind is MoveKind.CUT]
        passes = _passes_for_direction(context.boundary, direction_deg=0, stepover=6,
                                      offset=tool.footprint_radius_mm)
        self.assertEqual(len(passes), len(actual_cuts))
        for index, ((start, end, level, frame), move) in enumerate(zip(passes, actual_cuts)):
            planar = np.array([[end, level], [start, level]] if index % 2 else [[start, level], [end, level]]) @ frame.T
            nominal_xy = context.sample_cut_points(planar)
            expected = context.to_positions(nominal_xy, tool_axes=move.tool_axes, compensate_tool=True)
            np.testing.assert_allclose(move.points, expected, atol=1e-10)
        raw = self.plan(False, kind=ToolKind.FLAT).toolpath
        self.assertTrue(any(not np.allclose(a.points, b.points)
                            for a, b in zip(raw.moves, result.toolpath.moves) if a.kind is MoveKind.CUT))

    def test_off_and_zero_deviation_recover_original_cut_axes(self):
        raw = self.plan(False).toolpath
        zero = self.plan(max_axis_deviation_deg=0).toolpath
        self.assertNotIn("orientation_smoothing", raw.metadata)
        self.assertTrue(all(m.angular_speed_deg_s is None for m in raw.moves))
        for a, b in zip([m for m in raw.moves if m.kind is MoveKind.CUT],
                        [m for m in zero.moves if m.kind is MoveKind.CUT]):
            np.testing.assert_allclose(a.tool_axes, b.tool_axes, atol=1e-12)

    def test_speed_limit_applies_to_every_segment_and_timeline(self):
        path = self.plan().toolpath
        timeline = build_timeline(path, max_samples=2)
        self.assertAlmostEqual(path.estimated_time_s, timeline.duration_s, places=8)
        meta = path.metadata["orientation_smoothing"]
        self.assertGreater(meta["limited_segment_count"], 0)
        self.assertLessEqual(meta["peak_angular_speed_deg_s"], 10.000001)
        for i, dt in enumerate(np.diff(timeline.times_s)):
            angle = axis_angle_deg(timeline.tool_axes[i], timeline.tool_axes[i + 1])
            if dt > 1e-8:
                self.assertLessEqual(angle / dt, 10.0001)
        self.assertGreater(timeline.sample_count, 2)

    def test_rotation_only_keeps_nonzero_time_even_with_zero_length(self):
        move = Move(MoveKind.RAPID, [[0, 0, 10], [0, 0, 10]], 5000,
                    tool_axes=[[0, 0, 1], [1, 0, 0]], angular_speed_deg_s=30)
        path = Toolpath((move,))
        self.assertEqual(move.length_mm, 0)
        self.assertAlmostEqual(move.duration_s, 3)
        timeline = build_timeline(path, max_samples=2)
        self.assertAlmostEqual(timeline.duration_s, 3)
        self.assertAlmostEqual(axis_angle_deg([0, 0, 1], timeline.state_at(0.75).tool_axis), 22.5)

    def test_zero_length_rotation_inside_connector_is_not_dropped(self):
        move = Move(MoveKind.RAPID, [[0, 0, 0], [0, 0, 10], [0, 0, 10], [0, 0, 0]], 600,
                    tool_axes=[[0, 0, 1], [0, 0, 1], [1, 0, 0], [1, 0, 0]], angular_speed_deg_s=30)
        timeline = build_timeline(Toolpath((move,)), max_samples=2)
        np.testing.assert_allclose(timeline.times_s, [0, 1, 4, 5])
        np.testing.assert_allclose(timeline.state_at(2).position, [0, 0, 10])
        self.assertAlmostEqual(axis_angle_deg([0, 0, 1], timeline.state_at(2).tool_axis), 30)

    def test_downward_axis_is_not_serialized_as_default_vertical_pose(self):
        move = Move(MoveKind.RAPID, [[0, 0, 0], [0, 0, 0]], 600,
                    tool_axes=[[0, 0, 1], [0, 0, -1]], angular_speed_deg_s=30)
        self.assertTrue(move.is_oriented)
        self.assertIn("tool_axes", move.to_payload())
        timeline = build_timeline(Toolpath((move,)), max_samples=2)
        self.assertIsNotNone(timeline.tool_axes)
        self.assertAlmostEqual(timeline.duration_s, 6)

    def test_connectors_are_continuous_and_rotate_only_above_surface(self):
        result = self.plan()
        for a, b in zip(result.toolpath.moves[:-1], result.toolpath.moves[1:]):
            np.testing.assert_allclose(a.points[-1], b.points[0], atol=1e-10)
            np.testing.assert_allclose(a.tool_axes[-1], b.tool_axes[0], atol=1e-10)
        surface = build_surface("composite", {})
        for move in result.toolpath.moves:
            if move.kind is not MoveKind.RAPID or len(move.points) != 4:
                continue
            np.testing.assert_allclose(move.tool_axes[0], move.tool_axes[1])
            np.testing.assert_allclose(move.tool_axes[2], move.tool_axes[3])
            self.assertGreaterEqual(move.points[1, 2], surface.height_bounds()[1] + 5 - 1e-9)

    def test_roughing_keeps_smoothing_and_limits_transition(self):
        path = self.plan(rough=True).toolpath
        self.assertIn("orientation_smoothing", path.metadata)
        boundary = path.metadata["roughing"]["finish_start_move_index"]
        self.assertEqual(path.moves[boundary - 1].angular_speed_deg_s, 10)
        for a, b in zip(path.moves[:-1], path.moves[1:]):
            np.testing.assert_allclose(a.points[-1], b.points[0], atol=1e-10)
            np.testing.assert_allclose(a.tool_axes[-1], b.tool_axes[0], atol=1e-10)

    def test_parameters_and_model_reject_invalid_limits(self):
        for value in [0, float("nan"), float("inf"), -1]:
            with self.assertRaises(ParameterError):
                Move(MoveKind.RAPID, [[0, 0, 0], [0, 0, 1]], 100, angular_speed_deg_s=value)
        with self.assertRaises(ParameterError):
            self.plan(max_angular_speed_deg_s=0)

    def test_gcode_azimuth_wrap_is_unwrapped_and_time_limit_is_not_misrepresented(self):
        angles = np.radians([179, -179])
        axes = np.column_stack((np.sin(0.3) * np.cos(angles), np.sin(0.3) * np.sin(angles), np.full(2, np.cos(0.3))))
        move = Move(MoveKind.CUT, [[0, 0, 0], [1, 0, 0]], 600, tool_axes=axes, angular_speed_deg_s=20)
        code = toolpath_to_gcode(Toolpath((move,)))
        azimuths = [float(x) for x in re.findall(r" A(-?[\d.]+)", code)]
        self.assertEqual(azimuths, [179, 181])
        self.assertIn("simulation-only", code)


if __name__ == "__main__":
    unittest.main()
