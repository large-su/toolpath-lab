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
    def test_the_built_in_strategies_are_registered(self) -> None:
        # 导入顺序即界面上排列顺序。
        self.assertEqual(PLANNERS.ids(), ["raster", "spiral", "contour"])

    def test_all_strategies_agree_on_the_stepover_range(self) -> None:
        # 同一个「切宽 ae」在三个策略里必须是一套范围，否则切策略就会突然 400。
        ranges = {}
        for entry in planner_catalog():
            spec = next(item for item in entry["parameters"] if item["key"] == "stepover_mm")
            ranges[entry["id"]] = (spec["min"], spec["max"], spec["step"])
        self.assertEqual(len(set(ranges.values())), 1, ranges)
        self.assertEqual(ranges["raster"], (0.5, 100.0, 0.5))

    def test_the_example_plugin_template_is_not_registered(self) -> None:
        # examples/plugins 里的模板不能占掉内置策略的 id，否则界面会出现两个"环切"。
        from examples.plugins.contour_planner import SketchPlanner, self_check

        self.assertNotIn(SketchPlanner.id, PLANNERS)
        report = self_check()
        self.assertFalse(report["registered"])
        self.assertGreater(report["rings"], 0)

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


class SpiralPlannerTests(unittest.TestCase):
    """螺旋铣：连续不断刀、旋向与进给变化。"""

    def _spiral(self, parameters=None, region=None):
        options = {"stepover_mm": 6.0, "sample_step_mm": 2.0, "feed_mm_per_min": 800.0}
        options.update(parameters or {})
        return run_plan(
            planner_id="spiral",
            tool=_tool(),
            region=region or build_region("circle", {"diameter_mm": 80.0}),
            parameters=options,
        )

    def test_is_registered(self) -> None:
        self.assertIn("spiral", PLANNERS)
        labels = {item["id"]: item["label"] for item in planner_catalog()}
        self.assertIn("spiral", labels)

    def test_rings_shrink_inward(self) -> None:
        passes = _cut_moves(self._spiral().toolpath)
        self.assertGreater(len(passes), 2)
        radii = [float(np.linalg.norm(move.points[:, :2], axis=1).mean())
                 for move in passes]
        for outer, inner in zip(radii, radii[1:]):
            self.assertLess(inner, outer)

    def test_only_one_plunge_and_one_retract(self) -> None:
        # 螺旋相对栅格的核心优势：全程只下一次刀、抬一次刀。
        rapid = [m for m in self._spiral().toolpath.moves if m.kind is MoveKind.RAPID]
        labels = [move.label for move in rapid]
        self.assertEqual(labels.count("下刀"), 1)
        self.assertEqual(labels.count("抬刀"), 1)

    def test_each_ring_is_a_closed_loop(self) -> None:
        for move in _cut_moves(self._spiral().toolpath):
            first = move.points[0]
            last = move.points[-1]
            np.testing.assert_allclose(first, last, atol=1e-6)

    def test_ccw_and_cw_reverse_the_winding(self) -> None:
        def winding(direction: str) -> float:
            points = _cut_moves(self._spiral({"direction": direction}).toolpath)[0].points[:, :2]
            return float(0.5 * np.sum(
                points[:, 0] * np.roll(points[:, 1], -1)
                - np.roll(points[:, 0], -1) * points[:, 1]
            ))

        self.assertGreater(winding("ccw"), 0.0)
        self.assertLess(winding("cw"), 0.0)

    def test_feed_ramps_down_towards_the_centre(self) -> None:
        passes = _cut_moves(self._spiral(
            {"ramp_feed": True, "feed_mm_per_min": 800.0, "center_feed_mm_per_min": 300.0}
        ).toolpath)
        feeds = [move.feed_mm_per_min for move in passes]
        self.assertAlmostEqual(feeds[0], 800.0, places=6)
        self.assertAlmostEqual(feeds[-1], 300.0, delta=1.0)
        for outer, inner in zip(feeds, feeds[1:]):
            self.assertLess(inner, outer)

    def test_feed_ramp_can_be_switched_off(self) -> None:
        passes = _cut_moves(self._spiral(
            {"ramp_feed": False, "feed_mm_per_min": 800.0, "center_feed_mm_per_min": 300.0}
        ).toolpath)
        self.assertTrue(all(move.feed_mm_per_min == 800.0 for move in passes))

    def test_inverted_ramp_is_warned_about(self) -> None:
        outcome = self._spiral(
            {"ramp_feed": True, "feed_mm_per_min": 300.0, "center_feed_mm_per_min": 900.0}
        )
        self.assertTrue(any("中心进给" in warning for warning in outcome.warnings))

    def test_leaving_a_pill_stops_short_of_the_centre(self) -> None:
        full = _cut_moves(self._spiral().toolpath)
        pilled = _cut_moves(self._spiral({"allow_leave_pill": True}).toolpath)
        self.assertLess(len(pilled), len(full))
        innermost = min(
            float(np.linalg.norm(move.points[:, :2], axis=1).min()) for move in pilled
        )
        self.assertGreater(innermost, 10.0)

    def test_reaches_the_centre_for_convex_shapes(self) -> None:
        for shape, parameters in (
            ("square", {"side_mm": 80.0}),
            ("circle", {"diameter_mm": 80.0}),
            ("ellipse", {"semi_major_mm": 60.0, "semi_minor_mm": 40.0}),
        ):
            with self.subTest(shape=shape):
                outcome = self._spiral(region=build_region(shape, parameters))
                self.assertFalse(outcome.warnings)
                innermost = min(
                    float(np.linalg.norm(move.points[:, :2], axis=1).min())
                    for move in _cut_moves(outcome.toolpath)
                )
                self.assertLess(innermost, 5.0)

    def test_corner_self_intersection_is_warned_about(self) -> None:
        # 圆角半径小于切宽时，偏置会在圆角处自交，中心切不到——必须提醒而不是静默收工。
        outcome = self._spiral(region=build_region(
            "rounded_rect",
            {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 12.0},
        ))
        self.assertTrue(outcome.warnings)
        self.assertTrue(_cut_moves(outcome.toolpath))

    def test_oversized_tool_is_unprocessable(self) -> None:
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="spiral",
                tool=_tool(120.0),
                region=build_region("square", {"side_mm": 40.0}),
                parameters={"stepover_mm": 5.0},
            )

    def test_every_move_is_typed_and_fed(self) -> None:
        for move in self._spiral().toolpath.moves:
            self.assertIsNotNone(move.kind)
            self.assertGreater(move.feed_mm_per_min, 0.0)
            self.assertGreaterEqual(move.points.shape[0], 2)


class ContourPlannerTests(unittest.TestCase):
    """环切：逐圈等距向内偏置，每圈一刀。"""

    def _contour(self, parameters=None, region=None):
        options = {"stepover_mm": 6.0, "sample_step_mm": 2.0, "feed_mm_per_min": 600.0}
        options.update(parameters or {})
        return run_plan(
            planner_id="contour",
            tool=_tool(),
            region=region or build_region("circle", {"diameter_mm": 80.0}),
            parameters=options,
        )

    def test_is_registered(self) -> None:
        self.assertIn("contour", PLANNERS)
        labels = {item["id"]: item["label"] for item in planner_catalog()}
        self.assertEqual(labels["contour"], "环切")

    def test_rings_shrink_inward_and_are_equally_spaced(self) -> None:
        passes = _cut_moves(self._contour({"stepover_mm": 6.0}).toolpath)
        self.assertGreater(len(passes), 2)
        radii = [float(np.linalg.norm(move.points[:, :2], axis=1).mean())
                 for move in passes]
        gaps = [outer - inner for outer, inner in zip(radii, radii[1:])]
        for gap in gaps:
            # 圆形区域的相邻圈半径差应当就是切宽（首圈还内缩了刀具半径，单独看）。
            self.assertAlmostEqual(gap, 6.0, delta=1.0)

    def test_each_ring_is_a_closed_loop(self) -> None:
        for move in _cut_moves(self._contour().toolpath):
            np.testing.assert_allclose(move.points[0], move.points[-1], atol=1e-6)

    def test_rapid_link_mode_retracts_between_rings(self) -> None:
        outcome = self._contour({"link": "rapid"})
        rapid = [m for m in outcome.toolpath.moves if m.kind is MoveKind.RAPID]
        # 起点一次下刀、每刀之间一次抬刀横移、终点一次抬刀。
        self.assertEqual(len(rapid), len(_cut_moves(outcome.toolpath)) + 1)
        highest = max(float(move.points[:, 2].max()) for move in rapid)
        self.assertAlmostEqual(highest, SAFE_HEIGHT_MM, places=6)

    def test_link_mode_stays_on_the_cut_plane(self) -> None:
        outcome = self._contour({"link": "link"})
        links = [m for m in outcome.toolpath.moves if m.kind is MoveKind.LINK]
        self.assertTrue(links)
        for move in links:
            self.assertAlmostEqual(float(move.points[:, 2].max()), 0.0, places=9)

    def test_link_mode_travels_less_than_rapid_mode(self) -> None:
        rapid = self._contour({"link": "rapid"}).toolpath.rapid_length_mm
        linked = self._contour({"link": "link"}).toolpath.rapid_length_mm
        self.assertLess(linked, rapid)

    def test_direction_reverses_the_winding(self) -> None:
        def winding(direction: str) -> float:
            points = _cut_moves(self._contour({"direction": direction}).toolpath)[0]
            plan = points.points[:, :2]
            return float(0.5 * np.sum(
                plan[:, 0] * np.roll(plan[:, 1], -1)
                - np.roll(plan[:, 0], -1) * plan[:, 1]
            ))

        self.assertGreater(winding("ccw"), 0.0)
        self.assertLess(winding("cw"), 0.0)

    def test_rings_do_not_overlap(self) -> None:
        # 环切与螺旋的关键区别：环切逐圈等距，相邻圈之间不该共享圆弧。
        passes = _cut_moves(self._contour({"stepover_mm": 6.0}).toolpath)
        outer = np.linalg.norm(passes[0].points[:, :2], axis=1).mean()
        inner = np.linalg.norm(passes[1].points[:, :2], axis=1).mean()
        self.assertGreater(outer - inner, 1.0)

    def test_every_region_shape_is_supported(self) -> None:
        for shape, parameters in (
            ("square", {"side_mm": 80.0}),
            ("circle", {"diameter_mm": 80.0}),
            ("ellipse", {"semi_major_mm": 60.0, "semi_minor_mm": 40.0}),
            ("rounded_rect", {"width_mm": 80.0, "height_mm": 60.0,
                              "corner_radius_mm": 12.0}),
        ):
            with self.subTest(shape=shape):
                outcome = self._contour(region=build_region(shape, parameters))
                self.assertGreater(outcome.toolpath.pass_count, 0)
                self.assertGreater(outcome.toolpath.cut_length_mm, 0.0)

    def test_corner_self_intersection_is_warned_about(self) -> None:
        outcome = self._contour(region=build_region(
            "rounded_rect",
            {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 12.0},
        ))
        self.assertTrue(any("中心" in warning for warning in outcome.warnings))

    def test_oversized_tool_is_unprocessable(self) -> None:
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="contour",
                tool=_tool(120.0),
                region=build_region("square", {"side_mm": 40.0}),
                parameters={"stepover_mm": 5.0},
            )

    def test_notes_mention_the_concave_limitation(self) -> None:
        notes = self._contour().toolpath.notes
        self.assertTrue(any("窄颈" in note for note in notes))


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
        self.assertTrue(any("安全高度" in note for note in notes))

    def test_rapid_moves_follow_the_configured_values(self) -> None:
        outcome = run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 80.0}),
            parameters={"mode": "one_way", "stepover_mm": 6.0, "feed_mm_per_min": 600.0},
            setup={"safe_height_mm": 12.0, "rapid_feed_mm_per_min": 9000.0},
        )
        rapid = [m for m in outcome.toolpath.moves if m.kind is MoveKind.RAPID]
        self.assertAlmostEqual(max(float(m.points[:, 2].max()) for m in rapid), 12.0)
        self.assertTrue(all(m.feed_mm_per_min == 9000.0 for m in rapid))

    def test_boundary_mode_changes_the_inward_offset(self) -> None:
        def widest(mode: str, offset: float = 0.0) -> float:
            outcome = run_plan(
                planner_id="raster",
                tool=_tool(),
                region=build_region("square", {"side_mm": 80.0}),
                parameters={"stepover_mm": 6.0, "feed_mm_per_min": 600.0},
                setup={"boundary_mode": mode, "boundary_offset_mm": offset},
            )
            return max(
                abs(float(point[0]))
                for move in outcome.toolpath.moves if move.kind is MoveKind.CUT
                for point in move.points
            )

        # D6 平底刀足迹半径 3：默认内缩到 37，不偏置到 40，外扩到 43。
        self.assertAlmostEqual(widest("tool_radius"), 37.0, places=3)
        self.assertAlmostEqual(widest("none"), 40.0, places=3)
        self.assertAlmostEqual(widest("outside"), 43.0, places=3)
        self.assertAlmostEqual(widest("custom", 1.0), 39.0, places=3)

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
