"""Retract height / rapid feed / boundary handling: behaviour after turning constants into parameters."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
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
from toolpath_lab.planning.base import MOTION_PARAMETERS, PlanningContext

_TOOL = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
_SQUARE = build_region("square", {"side_mm": 80.0})
_BASE = {"mode": "zigzag", "stepover_mm": 6.0, "direction_deg": 0.0, "feed_mm_per_min": 600.0}


def _plan(planner_id: str = "raster", parameters=None) -> Toolpath:
    """Plan with the raster defaults (contour ignores keys it does not know)."""

    return run_plan(
        planner_id=planner_id,
        tool=_TOOL,
        region=_SQUARE,
        parameters=dict(_BASE, **(parameters or {})),
    ).toolpath


def _rapid(toolpath: Toolpath):
    return [move for move in toolpath.moves if move.kind is MoveKind.RAPID]


def _levels(toolpath: Toolpath) -> list[float]:
    return sorted({round(float(move.points[0][1]), 6) for move in toolpath.moves
                   if move.kind is MoveKind.CUT})


class DeclarationTests(unittest.TestCase):
    def test_every_strategy_publishes_the_motion_parameters(self) -> None:
        for entry in planner_catalog():
            keys = [item["key"] for item in entry["parameters"]]
            with self.subTest(planner=entry["id"]):
                self.assertIn("safe_height_mm", keys)
                self.assertIn("rapid_feed_mm_per_min", keys)

    def test_defaults_keep_the_values_that_used_to_be_fixed(self) -> None:
        for planner_id in PLANNERS.ids():
            defaults = PLANNERS.get(planner_id).parameters.defaults()
            with self.subTest(planner=planner_id):
                self.assertEqual(defaults["safe_height_mm"], SAFE_HEIGHT_MM)
                self.assertEqual(defaults["rapid_feed_mm_per_min"], RAPID_FEED_MM_PER_MIN)

    def test_the_motion_parameters_are_one_shared_declaration(self) -> None:
        # One shared declaration: changing a range or unit later touches MOTION_PARAMETERS only.
        self.assertEqual([item.key for item in MOTION_PARAMETERS],
                         ["safe_height_mm", "rapid_feed_mm_per_min",
                          "corner_angle_deg", "corner_feed_ratio"])
        for planner_id in PLANNERS.ids():
            specs = {item.key: item for item in PLANNERS.get(planner_id).parameters}
            with self.subTest(planner=planner_id):
                for key in ("safe_height_mm", "rapid_feed_mm_per_min",
                            "corner_angle_deg", "corner_feed_ratio"):
                    self.assertEqual(specs[key].default, MOTION_PARAMETERS.spec(key).default)
                    self.assertEqual(specs[key].minimum, MOTION_PARAMETERS.spec(key).minimum)
                    self.assertEqual(specs[key].maximum, MOTION_PARAMETERS.spec(key).maximum)


class SafeHeightTests(unittest.TestCase):
    def test_safe_height_is_used_by_every_rapid_move(self) -> None:
        for planner_id in PLANNERS.ids():
            toolpath = _plan(planner_id, {"safe_height_mm": 12.0})
            with self.subTest(planner=planner_id):
                rapid = _rapid(toolpath)
                self.assertTrue(rapid)
                self.assertAlmostEqual(
                    max(float(move.points[:, 2].max()) for move in rapid), 12.0, places=6
                )

    def test_cutting_and_linking_moves_stay_on_the_machining_plane(self) -> None:
        for planner_id in PLANNERS.ids():
            toolpath = _plan(planner_id, {"safe_height_mm": 12.0})
            with self.subTest(planner=planner_id):
                for move in toolpath.moves:
                    if move.kind is not MoveKind.RAPID:
                        self.assertTrue(bool(np.allclose(move.points[:, 2], 0.0)))

    def test_zero_safe_height_means_no_lift(self) -> None:
        toolpath = _plan("raster", {"safe_height_mm": 0.0})
        for move in _rapid(toolpath):
            self.assertTrue(bool(np.allclose(move.points[:, 2], 0.0)))

    def test_out_of_range_safe_height_is_rejected(self) -> None:
        for value in (-1.0, 500.0):
            with self.subTest(value=value):
                with self.assertRaises(ParameterError):
                    _plan("raster", {"safe_height_mm": value})


class RapidFeedTests(unittest.TestCase):
    def test_rapid_feed_is_used_by_every_rapid_move(self) -> None:
        for planner_id in PLANNERS.ids():
            toolpath = _plan(planner_id, {"rapid_feed_mm_per_min": 2000.0})
            with self.subTest(planner=planner_id):
                for move in _rapid(toolpath):
                    self.assertEqual(move.feed_mm_per_min, 2000.0)

    def test_the_cutting_feed_is_unaffected(self) -> None:
        toolpath = _plan("raster", {"rapid_feed_mm_per_min": 2000.0})
        for move in toolpath.moves:
            if move.kind is not MoveKind.RAPID:
                self.assertEqual(move.feed_mm_per_min, 600.0)

    def test_slower_rapid_feed_only_costs_time_not_length(self) -> None:
        fast = _plan("raster", {"mode": "one_way", "rapid_feed_mm_per_min": 20000.0})
        slow = _plan("raster", {"mode": "one_way", "rapid_feed_mm_per_min": 500.0})
        self.assertAlmostEqual(fast.rapid_length_mm, slow.rapid_length_mm, places=6)
        self.assertAlmostEqual(fast.cut_length_mm, slow.cut_length_mm, places=6)
        self.assertGreater(slow.estimated_time_s, fast.estimated_time_s)

    def test_out_of_range_rapid_feed_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            _plan("raster", {"rapid_feed_mm_per_min": 10.0})


class FallbackTests(unittest.TestCase):
    """A third party strategy that declares neither parameter falls back to the defaults, not KeyError."""

    def test_context_without_motion_parameters_falls_back_to_the_defaults(self) -> None:
        context = PlanningContext(
            tool=_TOOL, region=_SQUARE, parameters={"feed_mm_per_min": 600.0}
        )
        self.assertEqual(context.safe_height_mm, SAFE_HEIGHT_MM)
        self.assertEqual(context.rapid_feed_mm_per_min, RAPID_FEED_MM_PER_MIN)
        move = context.rapid_between(
            np.array([0.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0])
        )
        self.assertAlmostEqual(float(move.points[:, 2].max()), SAFE_HEIGHT_MM, places=6)
        self.assertEqual(move.feed_mm_per_min, RAPID_FEED_MM_PER_MIN)


class BoundaryModeTests(unittest.TestCase):
    def test_default_inset_is_unchanged(self) -> None:
        toolpath = _plan("raster")
        self.assertEqual(toolpath.pass_count, 14)
        self.assertAlmostEqual(_levels(toolpath)[0], -37.0, places=6)
        self.assertAlmostEqual(_levels(toolpath)[-1], 37.0, places=6)
        self.assertAlmostEqual(toolpath.cut_length_mm, 1036.0, places=6)

    def test_touching_the_contour_reaches_the_boundary_and_warns(self) -> None:
        outcome = run_plan(
            planner_id="raster", tool=_TOOL, region=_SQUARE,
            parameters=dict(_BASE, boundary_mode="none"),
        )
        levels = _levels(outcome.toolpath)
        self.assertAlmostEqual(levels[0], -40.0, places=6)
        self.assertAlmostEqual(levels[-1], 40.0, places=6)
        self.assertAlmostEqual(outcome.toolpath.cut_length_mm, 1200.0, places=6)
        self.assertTrue(any("切出区域" in warning for warning in outcome.warnings))

    def test_stock_allowance_shrinks_the_toolpath_by_the_allowance(self) -> None:
        toolpath = _plan("raster", {"stock_allowance_mm": 2.0})
        levels = _levels(toolpath)
        self.assertAlmostEqual(levels[0], -35.0, places=6)
        self.assertAlmostEqual(levels[-1], 35.0, places=6)
        self.assertAlmostEqual(toolpath.cut_length_mm, 910.0, places=6)

    def test_the_allowance_is_ignored_when_touching_the_contour(self) -> None:
        with_allowance = _plan("raster", {"boundary_mode": "none", "stock_allowance_mm": 5.0})
        without = _plan("raster", {"boundary_mode": "none"})
        self.assertAlmostEqual(with_allowance.cut_length_mm, without.cut_length_mm, places=6)

    def test_only_the_raster_strategy_offers_the_boundary_mode(self) -> None:
        raster = [item.key for item in PLANNERS.get("raster").parameters]
        contour = [item.key for item in PLANNERS.get("contour").parameters]
        self.assertIn("boundary_mode", raster)
        self.assertIn("stock_allowance_mm", raster)
        # The first contour ring is always inset by one footprint radius: its offset geometry only
    # supports inward offsets, see contour.py.
        self.assertNotIn("boundary_mode", contour)

    def test_unknown_boundary_mode_is_rejected(self) -> None:
        for value in ("outset", "bogus"):
            with self.subTest(value=value):
                with self.assertRaises(ParameterError):
                    _plan("raster", {"boundary_mode": value})

    def test_notes_record_the_boundary_and_motion_settings(self) -> None:
        note = _plan("raster", {"stock_allowance_mm": 2.0}).notes[1]
        self.assertIn("边界处理：内缩一个刀具半径", note)
        self.assertIn("边界余量 2 mm", note)
        self.assertIn("安全高度 5 mm", note)
        self.assertIn("快移 5000 mm/min", note)
        self.assertNotIn("固定值", note)

    def test_notes_follow_the_chosen_values(self) -> None:
        note = _plan("raster", {"safe_height_mm": 8.0, "rapid_feed_mm_per_min": 9000.0}).notes[1]
        self.assertIn("安全高度 8 mm", note)
        self.assertIn("快移 9000 mm/min", note)


if __name__ == "__main__":
    unittest.main()
