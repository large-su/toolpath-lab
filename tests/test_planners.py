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
    def test_only_the_raster_strategy_is_registered(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "contour", "spiral"])

    def test_catalog_exposes_the_expected_parameters(self) -> None:
        entry = planner_catalog()[0]
        keys = [item["key"] for item in entry["parameters"]]
        self.assertIn("mode", keys)
        self.assertIn("stepover_mm", keys)
        self.assertIn("direction_deg", keys)
        self.assertIn("feed_mm_per_min", keys)
        self.assertIn("tilt_a_deg", keys)
        self.assertIn("tilt_b_deg", keys)
        self.assertIn("SAFE_HEIGHT_MM", keys)
        self.assertIn("RAPID_FEED_MM_PER_MIN", keys)
        self.assertEqual(entry["label"], "栅格刀路")
        stepover = next(item for item in entry["parameters"] if item["key"] == "stepover_mm")
        self.assertEqual(stepover["default"], 3.0)

    def test_raster_and_spiral_keep_fixed_ab_pose_on_every_move(self) -> None:
        for planner_id in ("raster", "spiral"):
            with self.subTest(planner_id=planner_id):
                outcome = run_plan(
                    planner_id=planner_id,
                    tool=_tool(),
                    region=build_region("square", {"side_mm": 80.0}),
                    parameters={"tilt_a_deg": 23.0, "tilt_b_deg": -11.0},
                )
                for move in outcome.toolpath.moves:
                    self.assertIsNotNone(move.rotary_axes)
                    self.assertTrue(
                        np.allclose(
                            move.rotary_axes,
                            [[23.0, -11.0]] * len(move.points),
                        )
                    )
                self.assertTrue(any("机床运动学反解" in warning for warning in outcome.warnings))


class PassLayoutTests(unittest.TestCase):
    def test_default_stepover_overlaps_half_of_the_default_tool_diameter(self) -> None:
        outcome = run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 80.0}),
        )
        passes = _cut_moves(outcome.toolpath)
        levels = sorted({round(float(move.points[0][1]), 6) for move in passes})
        self.assertTrue(all(gap <= 3.0 + 1e-6 for gap in np.diff(levels)))

    def test_pass_count_follows_stepover_and_tool_radius(self) -> None:
        # 80 mm 方形，刀具 D6（足迹半径 3），切宽 6 → v 从 -37 到 37，13 个间隔 + 末刀对齐 = 14
        toolpath = _plan().toolpath
        self.assertEqual(toolpath.pass_count, 14)

    def test_passes_cover_the_region_edge_with_tool_radius_overtravel(self) -> None:
        passes = _cut_moves(_plan().toolpath)
        levels = sorted({round(float(move.points[0][1]), 6) for move in passes})
        self.assertAlmostEqual(levels[0], -37.0, places=6)
        self.assertAlmostEqual(levels[-1], 37.0, places=6)
        self.assertAlmostEqual(float(passes[0].points[0][0]), -43.0, places=6)
        self.assertAlmostEqual(float(passes[0].points[-1][0]), 43.0, places=6)

    def test_raster_cutter_sweep_reaches_all_sampled_square_stock_points(self) -> None:
        from toolpath_lab.simulation import HeightField

        outcome = run_plan(
            planner_id="raster",
            tool=_tool(),
            region=build_region("square", {"side_mm": 80.0}),
        )
        stock = HeightField(
            build_region("square", {"side_mm": 80.0}).boundary(),
            resolution_mm=1.0,
            top_z_mm=2.0,
            bottom_z_mm=-5.0,
        )

        stock.simulate_toolpath(outcome.toolpath, radius_mm=3.0)

        self.assertTrue(np.all(stock.heights_mm[stock.inside] <= 0.0))

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
    def test_contour_keeps_fixed_ab_orientation_on_all_moves(self) -> None:
        outcome = run_plan(
            planner_id="contour",
            tool=_tool(),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"tilt_a_deg": 12.5, "tilt_b_deg": -7.0},
        )
        for move in outcome.toolpath.moves:
            self.assertIsNotNone(move.rotary_axes)
            self.assertTrue(np.allclose(move.rotary_axes, [[12.5, -7.0]] * len(move.points)))
        self.assertTrue(any("运动学反解" in warning for warning in outcome.warnings))

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

    def test_stepover_equal_to_tool_diameter_warns_about_lack_of_overlap(self) -> None:
        outcome = _plan({"stepover_mm": 6.0})
        self.assertTrue(any("残留脊" in warning for warning in outcome.warnings))

    def test_edge_overtravel_is_reported_for_machine_clearance_check(self) -> None:
        outcome = _plan()
        self.assertTrue(any("越过区域边界 3 mm" in warning for warning in outcome.warnings))

    def test_contour_cutter_clears_square_corners(self) -> None:
        from toolpath_lab.simulation import HeightField

        region = build_region("square", {"side_mm": 80.0})
        outcome = run_plan(
            planner_id="contour",
            tool=_tool(),
            region=region,
            parameters={"stepover_mm": 3.0, "sample_step_mm": 0.5},
        )
        stock = HeightField(
            region.boundary(),
            resolution_mm=0.5,
            top_z_mm=2.0,
            bottom_z_mm=-5.0,
        )

        stock.simulate_toolpath(outcome.toolpath, radius_mm=3.0)

        self.assertTrue(np.all(stock.heights_mm[stock.inside] <= 0.0))
        self.assertTrue(any("用于清除方形角部" in warning for warning in outcome.warnings))

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
