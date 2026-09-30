"""栅格刀路：往复与单向。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import (
    PLANNERS,
    RAPID_FEED_MM_PER_MIN,
    SAFE_HEIGHT_MM,
    planner_catalog,
    run_plan,
)
from toolpath_lab.planning.offset import distance_to_boundary


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
    def test_registered_strategies_are_raster_and_spiral(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "spiral"])

    def test_catalog_exposes_the_expected_parameters(self) -> None:
        entry = planner_catalog()[0]
        keys = [item["key"] for item in entry["parameters"]]
        self.assertEqual(keys, ["mode", "stepover_mm", "direction_deg", "feed_mm_per_min"])
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


class EllipseRegionTests(unittest.TestCase):
    """椭圆区域：栅格刀路与螺旋刀路都应自动适配。"""

    @staticmethod
    def _ellipse(major: float = 60.0, minor: float = 40.0):
        return build_region("ellipse", {"semi_major_mm": major, "semi_minor_mm": minor})

    def test_passes_fit_inside_the_two_semi_axes(self) -> None:
        # 走刀方向 0° → 刀线沿 x、在 y 方向布刀；短半轴 40 内缩 3 后是 ±37。
        toolpath = run_plan(
            planner_id="raster", tool=_tool(), region=self._ellipse(),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        levels = sorted({round(float(move.points[0][1]), 6) for move in _cut_moves(toolpath)})
        self.assertAlmostEqual(levels[0], -37.0, places=6)
        self.assertAlmostEqual(levels[-1], 37.0, places=6)

    def test_a_round_ellipse_matches_the_equivalent_circle(self) -> None:
        ellipse = run_plan(
            planner_id="raster", tool=_tool(), region=self._ellipse(40.0, 40.0),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        circle = run_plan(
            planner_id="raster", tool=_tool(),
            region=build_region("circle", {"diameter_mm": 80.0}),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        self.assertAlmostEqual(ellipse.cut_length_mm, circle.cut_length_mm, places=6)

    def test_a_wide_ellipse_cuts_more_than_the_narrow_one(self) -> None:
        wide = run_plan(
            planner_id="raster", tool=_tool(), region=self._ellipse(80.0, 40.0),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        narrow = run_plan(
            planner_id="raster", tool=_tool(), region=self._ellipse(40.0, 40.0),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        self.assertGreater(wide.cut_length_mm, narrow.cut_length_mm)

    def test_ellipse_also_works_with_the_spiral(self) -> None:
        # 椭圆相对中心仍是星形，螺旋可用；且刀心不会切出轮廓。
        region = self._ellipse()
        move = _cut_moves(
            run_plan(
                planner_id="spiral", tool=_tool(), region=region,
                parameters={"stepover_mm": 6.0},
            ).toolpath
        )[0]
        clearance = distance_to_boundary(move.points[:, :2], region.boundary()).min()
        self.assertGreaterEqual(clearance, _tool().footprint_radius_mm - 1e-6)


class ToolKindIntegrationTests(unittest.TestCase):
    """刀型经由"足迹半径"影响刀路：球头刀为 0，圆鼻刀为 R − Rc。"""

    @staticmethod
    def _levels(tool: Tool):
        toolpath = run_plan(
            planner_id="raster",
            tool=tool,
            region=build_region("square", {"side_mm": 80.0}),
            parameters={"mode": "zigzag", "stepover_mm": 6.0},
        ).toolpath
        return sorted({round(float(move.points[0][1]), 6) for move in _cut_moves(toolpath)})

    def test_flat_tool_insets_by_the_radius(self) -> None:
        levels = self._levels(Tool(ToolKind.FLAT, 6.0, 30.0))
        self.assertAlmostEqual(levels[0], -37.0, places=6)
        self.assertAlmostEqual(levels[-1], 37.0, places=6)

    def test_ball_tool_runs_right_on_the_contour(self) -> None:
        # 足迹半径 0：刀尖可以贴着轮廓走，最外侧刀线落在 x/y = ±40。
        levels = self._levels(Tool(ToolKind.BALL, 6.0, 30.0))
        self.assertAlmostEqual(levels[0], -40.0, places=6)
        self.assertAlmostEqual(levels[-1], 40.0, places=6)

    def test_bull_tool_insets_by_radius_minus_corner_radius(self) -> None:
        # D10 → R=5，Rc=2 → 足迹 3，与 D6 平底刀一致。
        levels = self._levels(Tool(ToolKind.BULL, 10.0, 30.0, corner_radius_mm=2.0))
        self.assertAlmostEqual(levels[0], -37.0, places=6)
        self.assertAlmostEqual(levels[-1], 37.0, places=6)

    def test_bull_tool_without_a_corner_radius_matches_a_flat_tool(self) -> None:
        bull = self._levels(Tool(ToolKind.BULL, 6.0, 30.0))
        flat = self._levels(Tool(ToolKind.FLAT, 6.0, 30.0))
        self.assertEqual(bull, flat)

    def test_ball_tool_also_works_with_the_spiral(self) -> None:
        toolpath = run_plan(
            planner_id="spiral",
            tool=Tool(ToolKind.BALL, 8.0, 40.0),
            region=build_region("circle", {"diameter_mm": 80.0}),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        cut = _cut_moves(toolpath)[0]
        # 球头刀不内缩，螺旋最外圈半径就是区域半径 40。
        self.assertAlmostEqual(float(np.linalg.norm(cut.points[-1][:2])), 40.0, places=3)


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


if __name__ == "__main__":
    unittest.main()
