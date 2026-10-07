"""Corner feed reduction: feeds ramp down where the path turns sharply."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import planner_catalog, run_plan
from toolpath_lab.planning.base import MOTION_PARAMETERS, PlanningContext
from toolpath_lab.planning.feeds import apply_corner_slowdown, feed_factors, turn_angles

_STRAIGHT = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
_RIGHT_ANGLE = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]])
_REVERSAL = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
_DEGENERATE = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]])


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(shape: str, planner_id: str, parameters: dict | None = None) -> Toolpath:
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0}
    options.update(parameters or {})
    return run_plan(
        planner_id=planner_id, tool=_tool(), region=build_region(shape, {}), parameters=options
    ).toolpath


def _cut_feeds(toolpath: Toolpath) -> list[float]:
    return [move.feed_mm_per_min for move in toolpath.moves if move.kind is MoveKind.CUT]


class TurnAngleTests(unittest.TestCase):
    def test_turn_angles_cover_the_three_cases(self) -> None:
        self.assertAlmostEqual(float(turn_angles(_STRAIGHT)[0]), 0.0, places=6)
        self.assertAlmostEqual(float(turn_angles(_RIGHT_ANGLE)[0]), 90.0, places=6)
        self.assertAlmostEqual(float(turn_angles(_REVERSAL)[0]), 180.0, places=6)

    def test_a_degenerate_segment_counts_as_straight(self) -> None:
        angles = turn_angles(_DEGENERATE)
        self.assertEqual(angles.shape, (2,))
        self.assertAlmostEqual(float(angles[0]), 0.0, places=6)  # the zero length step

    def test_a_two_point_polyline_has_no_interior_vertex(self) -> None:
        self.assertEqual(turn_angles(np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.0]])).shape, (0,))


class FeedFactorTests(unittest.TestCase):
    def test_a_right_angle_lands_where_the_linear_ramp_says(self) -> None:
        # 30° start, 35% at 180°: at 90° the factor is 1 - 0.65 * (90-30)/(180-30) = 0.74
        factors = feed_factors(_RIGHT_ANGLE, corner_angle_deg=30.0, corner_feed_ratio=0.35)
        self.assertTrue(np.allclose(factors, 0.74))

    def test_a_reversal_reaches_the_floor(self) -> None:
        factors = feed_factors(_REVERSAL, corner_angle_deg=30.0, corner_feed_ratio=0.35)
        self.assertTrue(np.allclose(factors, 0.35))

    def test_turns_below_the_start_angle_keep_full_feed(self) -> None:
        gentle = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.2, 0.0]])
        factors = feed_factors(gentle, corner_angle_deg=30.0, corner_feed_ratio=0.35)
        self.assertTrue(np.allclose(factors, 1.0))

    def test_disabled_settings_return_full_feed(self) -> None:
        for angle, ratio in ((0.0, 0.35), (30.0, 1.0)):
            with self.subTest(angle=angle, ratio=ratio):
                factors = feed_factors(_REVERSAL, corner_angle_deg=angle, corner_feed_ratio=ratio)
                self.assertTrue(np.allclose(factors, 1.0))


class ApplyTests(unittest.TestCase):
    def test_the_default_is_off(self) -> None:
        toolpath = _plan("square", "contour")
        self.assertEqual({move.feed_mm_per_min for move in toolpath.moves if move.kind is MoveKind.CUT},
                         {toolpath.moves[1].feed_mm_per_min})
        self.assertFalse(any("拐角减速" in note for note in toolpath.notes))

    def test_corners_slow_down_and_straight_stretches_do_not(self) -> None:
        toolpath = _plan("square", "contour", {"corner_angle_deg": 30.0})
        feeds = sorted(set(_cut_feeds(toolpath)))
        self.assertEqual(len(feeds), 2)
        self.assertAlmostEqual(feeds[0], 600.0 * 0.74, places=6)
        self.assertAlmostEqual(feeds[1], 600.0, places=6)
        self.assertTrue(any("拐角减速" in note for note in toolpath.notes))

    def test_only_the_feeds_change(self) -> None:
        plain = _plan("square", "contour")
        slowed = _plan("square", "contour", {"corner_angle_deg": 30.0})
        self.assertEqual(slowed.pass_count, plain.pass_count)
        self.assertAlmostEqual(slowed.cut_length_mm, plain.cut_length_mm, places=6)
        self.assertGreater(slowed.estimated_time_s, plain.estimated_time_s)
        self.assertGreater(len(slowed.moves), len(plain.moves))  # split into runs

    def test_split_runs_stay_continuous(self) -> None:
        """Every move keeps the geometry: the end of one run is the start of the next."""

        for move, following in zip(_plan("square", "contour", {"corner_angle_deg": 30.0}).moves,
                                   _plan("square", "contour", {"corner_angle_deg": 30.0}).moves[1:]):
            if move.kind is MoveKind.CUT and following.kind is MoveKind.CUT:
                with self.subTest(move=move.label):
                    self.assertTrue(np.allclose(move.points[-1], following.points[0]))

    def test_smooth_curves_are_left_alone(self) -> None:
        """A finely sampled circle turns by well under the start angle, so nothing is slowed."""

        toolpath = _plan("circle", "contour", {"corner_angle_deg": 30.0, "sample_step_mm": 0.1})
        self.assertEqual(set(_cut_feeds(toolpath)), {600.0})
        self.assertFalse(any("拐角减速" in note for note in toolpath.notes))

    def test_coarse_sampling_of_a_small_ring_really_does_have_corners(self) -> None:
        """With a 1 mm sample step the inner rings of a circle are coarse polygons.

        Each chord then spans several of the 180 original vertices, so the turn per sample exceeds
        the start angle and the slowdown applies -- which is correct: that ring is a polygon, not a
        smooth curve.
        """

        toolpath = _plan("circle", "contour", {"corner_angle_deg": 30.0})
        self.assertIn(600.0, set(_cut_feeds(toolpath)))
        self.assertTrue(any(feed < 600.0 for feed in _cut_feeds(toolpath)))

    def test_raster_passes_have_no_corners(self) -> None:
        toolpath = _plan("square", "raster", {"corner_angle_deg": 30.0})
        self.assertEqual(set(_cut_feeds(toolpath)), {600.0})
        self.assertFalse(any("拐角减速" in note for note in toolpath.notes))

    def test_an_already_straight_toolpath_is_returned_unchanged(self) -> None:
        toolpath = Toolpath(
            moves=(Move(MoveKind.CUT, _STRAIGHT, 600.0, pass_index=0),)
        )
        slowed = apply_corner_slowdown(toolpath, corner_angle_deg=30.0, corner_feed_ratio=0.35)
        self.assertIs(slowed, toolpath)


class ParameterTests(unittest.TestCase):
    def test_the_keys_are_shared_by_every_strategy(self) -> None:
        keys = [item.key for item in MOTION_PARAMETERS]
        self.assertIn("corner_angle_deg", keys)
        self.assertIn("corner_feed_ratio", keys)
        for entry in planner_catalog():
            with self.subTest(planner=entry["id"]):
                planner_keys = [item["key"] for item in entry["parameters"]]
                self.assertIn("corner_angle_deg", planner_keys)
                self.assertIn("corner_feed_ratio", planner_keys)

    def test_the_defaults_keep_the_feature_off(self) -> None:
        self.assertEqual(MOTION_PARAMETERS.spec("corner_angle_deg").default, 0.0)
        self.assertAlmostEqual(MOTION_PARAMETERS.spec("corner_feed_ratio").default, 0.35)

    def test_a_context_without_the_keys_falls_back_to_the_defaults(self) -> None:
        context = PlanningContext(tool=_tool(), region=build_region("square", {}), parameters={})
        self.assertEqual(context.corner_angle_deg, 0.0)
        self.assertAlmostEqual(context.corner_feed_ratio, 0.35)

    def test_out_of_range_values_are_rejected(self) -> None:
        for parameters in ({"corner_angle_deg": -1.0}, {"corner_angle_deg": 200.0},
                           {"corner_feed_ratio": 0.0}, {"corner_feed_ratio": 1.5}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    _plan("square", "contour", parameters)


if __name__ == "__main__":
    unittest.main()
