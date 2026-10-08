"""栅格刀路：往复、单向、曲面加工与分层粗加工。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.mesh import Mesh, ModelLibrary
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.stock import build_stock
from toolpath_lab.core.surface import FlatSurface, WaveSurface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import (
    PLANNERS,
    RAPID_FEED_MM_PER_MIN,
    SAFE_HEIGHT_MM,
    planner_catalog,
    run_plan,
)


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(parameters=None):
    options = {"mode": "zigzag", "stepover_mm": 6.0, "direction_deg": 0.0,
               "feed_mm_per_min": 600.0}
    options.update(parameters or {})
    return run_plan(
        planner_id="raster",
        tool=_tool(),
        region=build_region("square", {"side_mm": 80.0}),
        parameters=options,
    )


def _cut_moves(toolpath: Toolpath):
    return [move for move in toolpath.moves if move.kind is MoveKind.CUT]


class RegistryTests(unittest.TestCase):
    def test_only_the_raster_strategy_is_registered(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster"])

    def test_catalog_exposes_the_expected_parameters(self) -> None:
        entry = planner_catalog()[0]
        keys = [item["key"] for item in entry["parameters"]]
        self.assertEqual(
            keys,
            ["mode", "stepover_mm", "direction_deg", "feed_mm_per_min",
             "sample_step_mm", "depth_per_pass_mm", "finish_pass"],
        )
        self.assertEqual(entry["label"], "栅格刀路")


class PassLayoutTests(unittest.TestCase):
    def test_pass_count_follows_stepover_and_tool_radius(self) -> None:
        # 80 mm 方形，刀具 D6（足迹半径 3），切宽 6 → v 从 -37 到 37，13 个间隔 + 末刀对齐 = 14
        toolpath = _plan().toolpath
        self.assertEqual(toolpath.pass_count, 14)

    def test_passes_are_inside_the_contour_by_the_tool_radius(self) -> None:
        passes = _cut_moves(_plan().toolpath)
        levels = sorted({round(float(move.points[0][1]), 6) for move in passes})
        self.assertAlmostEqual(levels[0], -37.0, places=6)
        self.assertAlmostEqual(levels[-1], 37.0, places=6)

    def test_ball_tool_paths_reach_the_contour(self) -> None:
        # 球头刀以刀尖对刀（足迹半径 0），刀路因此一直走到区域轮廓上。
        toolpath = run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=30.0),
            region=build_region("square", {"side_mm": 80.0}),
            parameters={"stepover_mm": 4.0},
        ).toolpath
        levels = sorted({round(float(move.points[0][1]), 6) for move in _cut_moves(toolpath)})
        self.assertAlmostEqual(levels[0], -40.0, places=6)
        self.assertAlmostEqual(levels[-1], 40.0, places=6)
        # 球头刀还会在说明里给出相邻两刀之间的理论残留高度
        self.assertTrue(any("球头刀" in note for note in toolpath.notes))
        self.assertTrue(any("残留高度" in note for note in toolpath.notes))

    def test_each_pass_has_two_points_on_the_machining_plane(self) -> None:
        for move in _cut_moves(_plan().toolpath):
            self.assertEqual(move.points.shape, (2, 3))
            self.assertTrue(np.allclose(move.points[:, 2], 0.0))

    def test_stepover_is_respected(self) -> None:
        passes = _cut_moves(_plan({"stepover_mm": 10.0}).toolpath)
        levels = sorted({round(float(move.points[0][1]), 6) for move in passes})
        gaps = np.diff(levels)
        self.assertTrue(all(gap <= 10.0 + 1e-6 for gap in gaps))

    def test_larger_stepover_needs_fewer_passes(self) -> None:
        self.assertGreater(
            _plan({"stepover_mm": 4.0}).toolpath.pass_count,
            _plan({"stepover_mm": 12.0}).toolpath.pass_count,
        )


class ModeTests(unittest.TestCase):
    def test_zigzag_alternates_direction(self) -> None:
        for index, move in enumerate(_cut_moves(_plan({"mode": "zigzag"}).toolpath)):
            delta = float(move.points[-1][0] - move.points[0][0])
            self.assertEqual(delta > 0, index % 2 == 0)

    def test_zigzag_links_passes_without_retracting(self) -> None:
        toolpath = _plan({"mode": "zigzag"}).toolpath
        kinds = [move.kind for move in toolpath.moves]
        self.assertEqual(kinds.count(MoveKind.LINK), toolpath.pass_count - 1)
        # 只有下刀与最后抬刀两段快速移动
        self.assertEqual(kinds.count(MoveKind.RAPID), 2)

    def test_one_way_keeps_a_single_direction_and_retracts(self) -> None:
        toolpath = _plan({"mode": "one_way"}).toolpath
        passes = _cut_moves(toolpath)
        for move in passes:
            self.assertGreater(float(move.points[-1][0] - move.points[0][0]), 0.0)
        kinds = [move.kind for move in toolpath.moves]
        self.assertEqual(kinds.count(MoveKind.LINK), 0)
        # 每刀之间一次抬刀 + 首尾各一次
        self.assertEqual(kinds.count(MoveKind.RAPID), len(passes) + 1)

    def test_one_way_takes_longer_than_zigzag(self) -> None:
        zigzag = _plan({"mode": "zigzag"}).toolpath
        one_way = _plan({"mode": "one_way"}).toolpath
        self.assertAlmostEqual(zigzag.cut_length_mm, one_way.cut_length_mm)
        self.assertGreater(one_way.rapid_length_mm, zigzag.rapid_length_mm)


class DirectionTests(unittest.TestCase):
    def test_zero_degrees_runs_along_x(self) -> None:
        move = _cut_moves(_plan({"direction_deg": 0.0}).toolpath)[0]
        delta = move.points[-1] - move.points[0]
        self.assertGreater(float(delta[0]), 0.0)
        self.assertAlmostEqual(float(delta[1]), 0.0, places=6)

    def test_ninety_degrees_runs_along_y(self) -> None:
        move = _cut_moves(_plan({"direction_deg": 90.0, "mode": "one_way"}).toolpath)[0]
        delta = move.points[-1] - move.points[0]
        self.assertAlmostEqual(float(delta[0]), 0.0, places=6)
        self.assertGreater(float(delta[1]), 0.0)

    def test_oblique_direction_keeps_the_cut_length(self) -> None:
        straight = _plan({"direction_deg": 0.0}).toolpath.cut_length_mm
        oblique = _plan({"direction_deg": 45.0}).toolpath.cut_length_mm
        self.assertGreater(oblique, 0.8 * straight)


class CircleRegionTests(unittest.TestCase):
    def test_circle_passes_are_shorter_than_the_square(self) -> None:
        circle = run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("circle", {"diameter_mm": 80.0}),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        self.assertLess(circle.cut_length_mm, _plan().toolpath.cut_length_mm)

    def test_circle_first_and_last_passes_are_short(self) -> None:
        passes = _cut_moves(
            run_plan(
                planner_id="raster",
                tool=_tool(),
                region=build_region("circle", {"diameter_mm": 80.0}),
                parameters={"stepover_mm": 6.0},
            ).toolpath
        )
        self.assertLess(passes[0].length_mm, passes[len(passes) // 2].length_mm)


class SafetyTests(unittest.TestCase):
    def test_rapid_moves_use_the_fixed_safe_height(self) -> None:
        rapid = [m for m in _plan({"mode": "one_way"}).toolpath.moves
                 if m.kind is MoveKind.RAPID]
        highest = max(float(move.points[:, 2].max()) for move in rapid)
        self.assertAlmostEqual(highest, SAFE_HEIGHT_MM, places=6)

    def test_rapid_moves_use_the_fixed_rapid_feed(self) -> None:
        rapid = [m for m in _plan({"mode": "one_way"}).toolpath.moves
                 if m.kind is MoveKind.RAPID]
        self.assertTrue(all(move.feed_mm_per_min == RAPID_FEED_MM_PER_MIN for move in rapid))

    def test_oversized_tool_is_reported_as_unprocessable(self) -> None:
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="raster",
                tool=_tool(120.0),
                region=build_region("square", {"side_mm": 40.0}),
                parameters={"stepover_mm": 5.0},
            )

    def test_large_stepover_is_warned_about(self) -> None:
        outcome = _plan({"stepover_mm": 20.0})
        self.assertTrue(any("切宽" in warning for warning in outcome.warnings))

    def test_notes_describe_the_configuration(self) -> None:
        notes = _plan({"mode": "one_way", "stepover_mm": 6.0}).toolpath.notes
        self.assertTrue(any("单向" in note for note in notes))
        self.assertTrue(any("固定值" in note for note in notes))

    def test_statistics_are_consistent(self) -> None:
        statistics = _plan().toolpath.statistics()
        self.assertGreater(statistics["estimated_time_s"], 0.0)
        moves = _plan().toolpath.moves
        link_length = sum(
            move.length_mm for move in moves if move.kind is MoveKind.LINK
        )
        self.assertAlmostEqual(
            statistics["total_length_mm"],
            statistics["cut_length_mm"] + statistics["rapid_length_mm"] + link_length,
            places=6,
        )


class CurvedSurfaceTests(unittest.TestCase):
    """曲面（非平面加工面）下的栅格刀路：逐点跟随、安全面抬高。"""

    def _curved(self, **parameters):
        options = {"stepover_mm": 6.0, "sample_step_mm": 2.0}
        options.update(parameters)
        return run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 80.0}),
            parameters=options,
            surface=WaveSurface(amplitude_mm=5.0, wavelength_mm=40.0),
        )

    def test_flat_surface_default_is_unchanged(self) -> None:
        # 不传 surface 时仍然是平面：一刀两点、Z = 0。
        for move in _cut_moves(_plan().toolpath):
            self.assertEqual(move.points.shape, (2, 3))
            self.assertTrue(np.allclose(move.points[:, 2], 0.0))

    def test_curved_surface_discretises_each_pass(self) -> None:
        passes = _cut_moves(self._curved().toolpath)
        self.assertGreater(passes[0].points.shape[0], 2)
        # 波浪面沿 X 方向起伏，所以同一条刀线上的 Z 不再是一个常数。
        self.assertGreater(float(np.ptp(passes[0].points[:, 2])), 0.0)

    def test_curved_surface_points_stay_on_the_surface(self) -> None:
        surface = WaveSurface(amplitude_mm=5.0, wavelength_mm=40.0)
        for move in _cut_moves(self._curved().toolpath):
            expected = surface.heights(move.points[:, :2])
            self.assertTrue(np.allclose(move.points[:, 2], expected, atol=1e-9))

    def test_smaller_step_gives_more_points(self) -> None:
        coarse = _cut_moves(self._curved(sample_step_mm=5.0).toolpath)[0]
        fine = _cut_moves(self._curved(sample_step_mm=1.0).toolpath)[0]
        self.assertGreater(fine.points.shape[0], coarse.points.shape[0])

    def test_safe_plane_sits_above_the_surface(self) -> None:
        toolpath = self._curved(mode="one_way").toolpath
        rapid = [move for move in toolpath.moves if move.kind is MoveKind.RAPID]
        highest = max(float(move.points[:, 2].max()) for move in rapid)
        self.assertAlmostEqual(highest, SAFE_HEIGHT_MM + 5.0, places=6)

    def test_curved_surface_is_explained_in_notes_and_warnings(self) -> None:
        outcome = self._curved()
        self.assertTrue(any("波浪面" in note for note in outcome.toolpath.notes))
        self.assertTrue(any("离散" in warning for warning in outcome.warnings))

    def test_offset_raises_the_whole_path(self) -> None:
        plain = _cut_moves(self._curved().toolpath)[0]
        shifted = _cut_moves(
            run_plan(
                planner_id="raster",
                tool=_tool(),
                region=build_region("square", {"side_mm": 80.0}),
                parameters={"stepover_mm": 6.0, "sample_step_mm": 2.0},
                surface=FlatSurface(z_offset_mm=3.0),
            ).toolpath
        )[0]
        self.assertTrue(np.allclose(shifted.points[:, 2], 3.0))
        self.assertTrue(np.all(np.abs(plain.points[:, 2]) <= 5.0 + 1e-9))


def _plate_stock(top: float = 10.0, side: float = 60.0, **margins):
    """一块方板的毛坯（模型的最小六面体包容体）。"""

    model = ModelLibrary().add("plate.stl", b"", mesh=_plate_mesh(side=side, top=top))
    return build_stock("model", margins, model=model)


def _plate_mesh(side: float = 60.0, top: float = 10.0) -> Mesh:
    """测试用模型：底面 Z=0、顶面 Z=top 的方板。"""

    half = side / 2.0
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    triangles = []
    for index in range(4):
        x0, y0 = corners[index]
        x1, y1 = corners[(index + 1) % 4]
        triangles.append([(x0, y0, top), (x1, y1, top), (0.0, 0.0, top)])
        triangles.append([(x0, y0, 0.0), (0.0, 0.0, 0.0), (x1, y1, 0.0)])
    return Mesh(np.array(triangles, dtype=np.float64))


def _passes(toolpath: Toolpath):
    """真正的刀轨（不含分层的下刀段，那些的 pass_index 是 -1）。"""

    return [
        move for move in toolpath.moves
        if move.kind is MoveKind.CUT and move.pass_index >= 0
    ]


class LayeredRoughingTests(unittest.TestCase):
    """切深（分层粗加工）：毛坯顶面 → 一层层往下切 → 沿加工面精加工。"""

    def _layered(self, **parameters):
        options = {"stepover_mm": 6.0, "depth_per_pass_mm": 4.0}
        options.update(parameters)
        return run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 60.0}),
            parameters=options,
            stock=_plate_stock(top=10.0, side=60.0),
        )

    def test_zero_depth_keeps_the_single_surface_pass(self) -> None:
        outcome = run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 60.0}),
            parameters={"stepover_mm": 6.0, "depth_per_pass_mm": 0.0},
            stock=_plate_stock(top=10.0),
        )
        toolpath = outcome.toolpath
        for move in _cut_moves(toolpath):
            self.assertEqual(move.points.shape, (2, 3))
            self.assertTrue(np.allclose(move.points[:, 2], 0.0))
        self.assertEqual(toolpath.pass_count, 10)

    def test_depth_without_a_stock_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            run_plan(
                planner_id="raster",
                tool=_tool(),
                region=build_region("square", {"side_mm": 60.0}),
                parameters={"stepover_mm": 6.0, "depth_per_pass_mm": 2.0},
            )

    def test_layers_are_stacked_from_the_stock_top(self) -> None:
        toolpath = self._layered().toolpath
        # 毛坯顶面 10、加工面 0、切深 4 → 粗加工两层 Z=6、2，最后一刀精加工到 Z=0
        layers = sorted({round(float(move.points[0][2]), 6) for move in _passes(toolpath)})
        self.assertEqual(layers, [0.0, 2.0, 6.0])
        # 两层粗加工 × 10 刀 + 精加工 10 刀
        self.assertEqual(toolpath.pass_count, 30)

    def test_each_pass_is_flat_within_a_layer(self) -> None:
        for move in _passes(self._layered().toolpath):
            heights = move.points[:, 2]
            self.assertTrue(np.allclose(heights, heights[0]))

    def test_finish_pass_can_be_switched_off(self) -> None:
        toolpath = self._layered(finish_pass=False).toolpath
        layers = sorted({round(float(move.points[0][2]), 6) for move in _passes(toolpath)})
        self.assertEqual(layers, [2.0, 6.0])
        self.assertEqual(toolpath.pass_count, 20)
        self.assertTrue(any("精加工" in warning for warning in
                            self._layered(finish_pass=False).warnings))

    def test_thickness_smaller_than_the_depth_makes_one_pass(self) -> None:
        outcome = self._layered(depth_per_pass_mm=20.0)
        layers = sorted(
            {round(float(move.points[0][2]), 6) for move in _passes(outcome.toolpath)}
        )
        self.assertEqual(layers, [0.0])
        self.assertTrue(any("不分层" in warning for warning in outcome.warnings))

    def test_first_cut_of_a_layer_plunges_at_the_cutting_feed(self) -> None:
        moves = self._layered().toolpath.moves
        # 第一段：从安全平面（毛坯顶面 10 + 5）以进给速度下到第一层 Z=6
        self.assertIs(moves[0].kind, MoveKind.CUT)
        self.assertEqual(moves[0].label, "分层下刀")
        self.assertTrue(np.allclose(moves[0].points[:, 2], [15.0, 6.0]))
        self.assertEqual(moves[0].pass_index, -1)

    def test_layers_are_separated_by_retracts(self) -> None:
        moves = self._layered().toolpath.moves
        rapid = [move for move in moves if move.kind is MoveKind.RAPID]
        # 层间一次、进入精加工一次抬刀-横移，收尾一次抬刀
        self.assertEqual(len(rapid), 3)
        self.assertTrue(all(move.feed_mm_per_min == RAPID_FEED_MM_PER_MIN for move in rapid))
        # 层内的刀间连接不抬刀
        self.assertEqual([m.kind for m in moves].count(MoveKind.LINK), 27)

    def test_safe_plane_clears_the_stock(self) -> None:
        moves = self._layered().toolpath.moves
        rapid = [move for move in moves if move.kind is MoveKind.RAPID]
        highest = max(float(move.points[:, 2].max()) for move in rapid)
        self.assertAlmostEqual(highest, 15.0, places=6)

    def test_only_the_material_above_the_layer_is_cut(self) -> None:
        # 波浪面最低 -5、最高 5；毛坯顶面 10、切深 5 → 布到 Z=5、0、-5，最后一层没有材料
        surface = WaveSurface(amplitude_mm=5.0, wavelength_mm=40.0)
        outcome = run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 60.0}),
            parameters={"stepover_mm": 10.0, "depth_per_pass_mm": 5.0, "finish_pass": False},
            surface=surface,
            stock=_plate_stock(top=10.0),
        )
        passes = _passes(outcome.toolpath)
        layers = sorted({round(float(move.points[0][2]), 6) for move in passes})
        self.assertEqual(layers, [0.0, 5.0])
        for move in passes:
            level = float(move.points[0][2])
            heights = surface.heights(move.points[:, :2])
            # 不会切到该层之上的材料（那部分是零件），量边上也不会超出该层
            self.assertTrue(np.all(heights <= level + 1e-6))
            self.assertTrue(np.all(move.points[:, 2] <= level + 1e-9))
        # 曲面分层同样要离散
        self.assertTrue(any(move.points.shape[0] > 2 for move in passes))

    def test_too_many_layers_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            self._layered(depth_per_pass_mm=0.04, finish_pass=False)

    def test_notes_describe_the_layers(self) -> None:
        notes = self._layered().toolpath.notes
        self.assertTrue(any("分 2 层" in note for note in notes))
        self.assertTrue(any("包容体" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
