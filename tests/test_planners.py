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


def _tool(diameter: float = 6.0, kind: ToolKind = ToolKind.FLAT) -> Tool:
    return Tool(kind, diameter_mm=diameter, length_mm=30.0)


def _plan(parameters=None, *, kind: ToolKind = ToolKind.FLAT):
    options = {"mode": "zigzag", "stepover_mm": 6.0, "direction_deg": 0.0,
               "feed_mm_per_min": 600.0}
    options.update(parameters or {})
    return run_plan(
        planner_id="raster",
        tool=_tool(kind=kind),
        region=build_region("square", {"side_mm": 80.0}),
        parameters=options,
    )


def _cut_moves(toolpath: Toolpath):
    return [move for move in toolpath.moves if move.kind is MoveKind.CUT]


def _pass_levels(outcome) -> list[float]:
    return sorted({round(float(move.points[0][1]), 6) for move in _cut_moves(outcome.toolpath)})


class RegistryTests(unittest.TestCase):
    def test_raster_is_registered_first(self) -> None:
        self.assertEqual(PLANNERS.ids()[0], "raster")

    def test_catalog_exposes_the_expected_parameters(self) -> None:
        entry = planner_catalog()[0]
        keys = [item["key"] for item in entry["parameters"]]
        self.assertEqual(
            keys,
            ["mode", "linking", "stepover_mm", "direction_deg", "feed_mm_per_min", "entry"],
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


class BallToolTests(unittest.TestCase):
    """球头刀只有刀尖接触（足迹半径 0），所以刀路不再内缩一个半径。"""

    def test_flat_tool_passes_are_inset_by_its_radius(self) -> None:
        levels = _pass_levels(_plan())
        self.assertAlmostEqual(levels[0], -37.0, places=6)
        self.assertAlmostEqual(levels[-1], 37.0, places=6)

    def test_ball_tool_passes_reach_the_contour(self) -> None:
        levels = _pass_levels(_plan(kind=ToolKind.BALL))
        self.assertAlmostEqual(levels[0], -40.0, places=6)
        self.assertAlmostEqual(levels[-1], 40.0, places=6)

    def test_ball_tool_needs_more_passes_than_the_flat_tool(self) -> None:
        self.assertGreater(
            _plan(kind=ToolKind.BALL).toolpath.pass_count,
            _plan().toolpath.pass_count,
        )

    def test_ball_tool_keeps_the_stepover(self) -> None:
        levels = _pass_levels(_plan(kind=ToolKind.BALL))
        self.assertTrue(all(gap <= 6.0 + 1e-6 for gap in np.diff(levels)))


def _ramp_plan(parameters=None, *, angle: float = 30.0, kind: ToolKind = ToolKind.FLAT,
               plateau: bool = False):
    options = {"mode": "one_way", "stepover_mm": 6.0, "direction_deg": 0.0,
               "feed_mm_per_min": 600.0}
    options.update(parameters or {})
    return run_plan(
        planner_id="raster",
        tool=_tool(kind=kind),
        region=build_region("ramp", {"side_mm": 80.0, "angle_deg": angle,
                                     "include_plateau": plateau}),
        parameters=options,
    )


def _pass_cuts(toolpath: Toolpath):
    """真正的刀线（排除"沿面切入"那段下刀）。"""

    return [move for move in _cut_moves(toolpath) if move.label != "沿面切入"]


def _lead_ins(toolpath: Toolpath):
    return [move for move in _cut_moves(toolpath) if move.label == "沿面切入"]


class SlopedSurfaceTests(unittest.TestCase):
    """斜面区域：XY 投影不变，Z 跟着加工面走；由低往高走刀，下刀沿面切入。"""

    def test_pass_levels_ignore_the_slope(self) -> None:
        flat = _pass_levels(_plan())
        sloped = _pass_levels(_ramp_plan())
        # 斜面会自动反向（由低往高走刀），把 y 取反之后两边的布刀完全一致
        self.assertEqual(flat, sorted(-level for level in sloped))

    def test_passes_follow_the_surface_height(self) -> None:
        slope = np.tan(np.radians(30.0))
        move = _pass_cuts(_ramp_plan().toolpath)[0]
        self.assertEqual(move.points.shape, (2, 3))
        for point in move.points:
            self.assertAlmostEqual(float(point[2]), (40.0 - float(point[0])) * slope, places=6)

    def test_passes_run_uphill_from_the_low_edge(self) -> None:
        slope = np.tan(np.radians(30.0))
        move = _pass_cuts(_ramp_plan(kind=ToolKind.BALL).toolpath)[0]
        self.assertAlmostEqual(float(move.points[0][0]), 40.0, places=6)
        self.assertAlmostEqual(float(move.points[0][2]), 0.0, places=6)
        self.assertAlmostEqual(float(move.points[-1][0]), -40.0, places=6)
        self.assertAlmostEqual(float(move.points[-1][2]), 80.0 * slope, places=6)

    def test_entry_mode_plunge_keeps_the_vertical_approach(self) -> None:
        toolpath = _ramp_plan({"entry": "plunge"}, kind=ToolKind.BALL).toolpath
        self.assertEqual(_lead_ins(toolpath), [])
        first = toolpath.moves[0]
        self.assertIs(first.kind, MoveKind.RAPID)
        # 垂直下刀：XY 不变、只降 Z；方向也不自动反向，所以第一刀从高边（−X）起刀
        self.assertAlmostEqual(float(first.points[0][0]), float(first.points[-1][0]), places=9)
        self.assertAlmostEqual(float(first.points[0][1]), float(first.points[-1][1]), places=9)
        self.assertAlmostEqual(float(first.points[-1][0]), -40.0, places=6)

    def test_lead_in_comes_from_outside_at_the_surface_height(self) -> None:
        slope = np.tan(np.radians(30.0))
        toolpath = _ramp_plan(kind=ToolKind.BALL).toolpath
        lead = _lead_ins(toolpath)
        self.assertEqual(len(lead), 1)
        self.assertAlmostEqual(float(lead[0].points[0][0]), 45.0, places=6)  # 40 + 引入 5
        self.assertAlmostEqual(float(lead[0].points[0][2]), 0.0, places=6)
        self.assertAlmostEqual(float(lead[0].points[-1][0]), 40.0, places=6)
        self.assertAlmostEqual(float(lead[0].points[-1][2]), 0.0, places=6)
        # 引入段之后紧跟着第一刀
        self.assertIs(toolpath.moves[2].kind, MoveKind.CUT)

    def test_one_way_links_on_the_surface(self) -> None:
        toolpath = _ramp_plan().toolpath
        rapids = [move for move in toolpath.moves if move.kind is MoveKind.RAPID]
        links = [move for move in toolpath.moves if move.kind is MoveKind.LINK]
        self.assertEqual(len(rapids), 2)  # 只剩"沿面切入"前的下刀与最后一次抬刀
        self.assertEqual(len(links), _ramp_plan().toolpath.pass_count - 1)
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 30.0})
        for move in links:
            self.assertTrue(
                np.allclose(move.points[:, 2], region.height_at(move.points[:, :2]), atol=1e-9)
            )

    def test_linking_can_still_retract(self) -> None:
        toolpath = _ramp_plan({"linking": "retract"}).toolpath
        rapids = [move for move in toolpath.moves if move.kind is MoveKind.RAPID]
        self.assertGreater(len(rapids), 2)

    def test_flat_regions_keep_the_old_behaviour(self) -> None:
        toolpath = _plan({"mode": "one_way"}).toolpath
        self.assertEqual(_lead_ins(toolpath), [])
        rapids = [move for move in toolpath.moves if move.kind is MoveKind.RAPID]
        self.assertGreater(len(rapids), 2)  # 平面仍然刀间抬刀
        self.assertFalse([move for move in toolpath.moves if move.kind is MoveKind.LINK])

    def test_crease_gets_its_own_vertex_when_the_plateau_is_machined(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0,
                                       "include_plateau": True})
        move = _pass_cuts(_ramp_plan(angle=60.0, plateau=True).toolpath)[0]
        self.assertEqual(move.points.shape, (3, 3))
        self.assertAlmostEqual(float(move.points[1][0]), region.crease_x_mm, places=6)
        self.assertAlmostEqual(float(move.points[1][2]), 80.0, places=6)

    def test_only_the_slope_is_machined_by_default(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0})
        toolpath = _ramp_plan(angle=60.0).toolpath
        self.assertIsNotNone(region.crease_x_mm)
        for move in _pass_cuts(toolpath):
            self.assertGreaterEqual(float(move.points[:, 0].min()),
                                    region.crease_x_mm - 1e-9)
        # 刀路正好停在折痕上（分界处那条边留了一个足迹半径再内缩）
        self.assertAlmostEqual(float(_pass_cuts(toolpath)[0].points[:, 0].min()),
                               region.crease_x_mm, places=6)
        for move in _pass_cuts(toolpath):
            self.assertTrue(np.allclose(move.points[:, 2],
                                        region.height_at(move.points[:, :2]), atol=1e-9))

    def test_plateau_is_machined_when_asked(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0})
        toolpath = _ramp_plan(angle=60.0, plateau=True).toolpath
        lowest = min(float(move.points[:, 0].min()) for move in _pass_cuts(toolpath))
        self.assertLess(lowest, float(region.crease_x_mm) - 1.0)

    def test_safe_height_is_measured_from_the_surface(self) -> None:
        for angle in (30.0, 60.0):
            toolpath = _ramp_plan(angle=angle).toolpath
            cuts = _cut_moves(toolpath)
            rapids = [m for m in toolpath.moves if m.kind is MoveKind.RAPID]
            highest_cut = max(float(m.points[:, 2].max()) for m in cuts)
            highest_rapid = max(float(m.points[:, 2].max()) for m in rapids)
            self.assertAlmostEqual(highest_rapid, highest_cut + SAFE_HEIGHT_MM, places=6)

    def test_cutting_length_grows_with_the_slope(self) -> None:
        flat = _plan({"mode": "one_way"}).toolpath.cut_length_mm
        sloped = _ramp_plan(angle=60.0).toolpath.cut_length_mm
        self.assertGreater(sloped, flat)

    def test_statistics_and_timeline_stay_consistent(self) -> None:
        toolpath = _ramp_plan(angle=60.0).toolpath
        statistics = toolpath.statistics()
        link_length = sum(
            move.length_mm for move in toolpath.moves if move.kind is MoveKind.LINK
        )
        self.assertAlmostEqual(
            statistics["total_length_mm"],
            statistics["cut_length_mm"] + statistics["rapid_length_mm"] + link_length,
            places=6,
        )


class RegionHeightTests(unittest.TestCase):
    """平面区域设了高度，刀路整条跟着抬：Z、安全面都相对它算。"""

    def _lifted(self, height: float, shape: str = "square", *, thickness: float = 20.0,
                **overrides):
        options = {"mode": "one_way", "stepover_mm": 20.0, "direction_deg": 0.0,
                   "feed_mm_per_min": 600.0}
        options.update(overrides)
        key = "side_mm" if shape == "square" else "diameter_mm"
        return run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region(shape, {key: 80.0, "height_mm": height,
                                        "thickness_mm": thickness}),
            parameters=options,
        )

    def test_passes_sit_on_the_chosen_height(self) -> None:
        for shape in ("square", "circle"):
            outcome = self._lifted(20.0, shape)
            for move in _cut_moves(outcome.toolpath):
                self.assertTrue(np.allclose(move.points[:, 2], 20.0, atol=1e-9))

    def test_safe_height_follows_the_region_height(self) -> None:
        outcome = self._lifted(20.0)
        rapids = [m for m in outcome.toolpath.moves if m.kind is MoveKind.RAPID]
        self.assertTrue(rapids)
        for move in rapids:
            self.assertLessEqual(float(move.points[:, 2].max()), 25.0 + 1e-9)
        self.assertAlmostEqual(
            max(float(move.points[:, 2].max()) for move in rapids), 25.0, places=6
        )

    def test_negative_height_stays_below_the_datum(self) -> None:
        outcome = self._lifted(-6.0)
        for move in _cut_moves(outcome.toolpath):
            self.assertTrue(np.allclose(move.points[:, 2], -6.0, atol=1e-9))

    def test_flat_regions_still_retract_between_passes(self) -> None:
        # 高度不改变"平面沿用抬刀连接"的判断（加工面仍然是水平的）
        outcome = self._lifted(20.0)
        self.assertFalse([m for m in outcome.toolpath.moves if m.kind is MoveKind.LINK])

    def test_part_thickness_does_not_change_the_toolpath(self) -> None:
        # 部件厚度只是"料有多厚"，刀路一模一样
        thin = self._lifted(0.0, thickness=5.0).toolpath
        thick = self._lifted(0.0, thickness=80.0).toolpath
        self.assertAlmostEqual(thin.cut_length_mm, thick.cut_length_mm, places=9)
        self.assertEqual(len(thin.moves), len(thick.moves))
        for first, second in zip(thin.moves, thick.moves):
            self.assertEqual(first.kind, second.kind)
            self.assertTrue(np.allclose(first.points, second.points))


class BullToolTests(unittest.TestCase):
    """圆鼻刀：足迹半径 = 半径 − 刀尖圆角，内缩量介于平底刀与球头刀之间。"""

    def _bull_plan(self, corner: float, stepover: float = 15.0):
        return run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=30.0,
                      corner_radius_mm=corner),
            region=build_region("square", {"side_mm": 80.0}),
            parameters={"mode": "one_way", "stepover_mm": stepover,
                        "feed_mm_per_min": 600.0},
        )

    def test_passes_are_inset_by_radius_minus_corner(self) -> None:
        levels = _pass_levels(self._bull_plan(1.5))
        self.assertAlmostEqual(levels[0], -(40.0 - 3.5), places=6)
        self.assertAlmostEqual(levels[-1], 40.0 - 3.5, places=6)

    def test_a_bigger_corner_insets_less(self) -> None:
        small_corner = _pass_levels(self._bull_plan(0.5, stepover=10.0))
        big_corner = _pass_levels(self._bull_plan(4.0, stepover=10.0))
        self.assertLess(abs(small_corner[0]), abs(big_corner[0]))

    def test_zero_corner_behaves_like_a_flat_tool(self) -> None:
        levels = _pass_levels(self._bull_plan(0.0, stepover=10.0))
        self.assertAlmostEqual(levels[0], -35.0, places=6)
        self.assertAlmostEqual(levels[-1], 35.0, places=6)

    def test_full_corner_behaves_like_a_ball_tool(self) -> None:
        levels = _pass_levels(self._bull_plan(5.0, stepover=20.0))
        self.assertAlmostEqual(levels[0], -40.0, places=6)
        self.assertAlmostEqual(levels[-1], 40.0, places=6)


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


if __name__ == "__main__":
    unittest.main()
