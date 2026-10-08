"""Spiral contouring: one continuous cut per level, and the shapes that do not suit one."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import measure_coverage, planner_catalog, run_plan
from toolpath_lab.planning.geometry2d import distance_to_boundary, signed_area

#: Shapes the spiral suits: the material is machined from a single front all the way in.
SINGLE_FRONTED = (("square", {}), ("circle", {"diameter_mm": 80.0}), ("ellipse", {}),
                  ("triangle", {}))
#: Shapes it does not: thin walls / a concave bar, where two fronts face each other.
DOUBLE_FRONTED = (("u_shape", {}), ("dumbbell", {}))


def _tool(diameter_mm: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter_mm, length_mm=30.0)


def _plan(shape: str, region_parameters: dict | None, planner_id: str = "spiral",
          parameters: dict | None = None):
    region = build_region(shape, region_parameters or {})
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0, **(parameters or {})}
    outcome = run_plan(
        planner_id=planner_id, tool=_tool(), region=region, parameters=options
    )
    return region, outcome


def _cuts(outcome):
    return [move for move in outcome.toolpath.moves if move.kind is MoveKind.CUT]


class ContinuityTests(unittest.TestCase):
    """What makes the path a spiral rather than rings with jumps between them."""

    def test_a_square_is_one_continuous_cut_with_no_lift(self) -> None:
        _region, outcome = _plan("square", {})
        kinds = [move.kind.value for move in outcome.toolpath.moves]
        # Entry rapid, seven revolutions, final retract: no link and no rapid inside the level.
        self.assertEqual(kinds, ["rapid"] + ["cut"] * 7 + ["rapid"])
        cuts = _cuts(outcome)
        for previous, following in zip(cuts, cuts[1:]):
            with self.subTest(pass_index=following.pass_index):
                self.assertTrue(np.allclose(previous.points[-1], following.points[0]))

    def test_the_first_revolution_walks_the_outer_ring_in_full(self) -> None:
        """A blend only touches the outer ring where it starts, so the wall band needs the full ring."""

        region, outcome = _plan("square", {})
        first = _cuts(outcome)[0]
        distances = distance_to_boundary(first.points[:, :2], region.boundary())
        self.assertAlmostEqual(float(distances.max() - distances.min()), 0.0, places=6)
        self.assertAlmostEqual(float(distances.mean()), 3.0, places=6)  # D6 flat: clearance R3
        self.assertTrue(np.allclose(first.points[0], first.points[-1]))

    def test_every_revolution_steps_in_by_exactly_one_stepover(self) -> None:
        region, outcome = _plan("square", {})
        polygon = region.boundary()
        cuts = _cuts(outcome)
        starts = [float(distance_to_boundary(move.points[:1, :2], polygon)[0]) for move in cuts]
        ends = [float(distance_to_boundary(move.points[-1:, :2], polygon)[0]) for move in cuts]
        self.assertAlmostEqual(starts[0], 3.0, places=6)  # the full outer ring
        for index in range(1, len(cuts)):
            with self.subTest(revolution=index):
                # A blend begins on the ring the previous one ended on and ends one stepover deeper,
                # so the distance to the wall grows by exactly one stepover along it.
                self.assertAlmostEqual(starts[index], ends[index - 1], places=3)
                self.assertAlmostEqual(ends[index] - starts[index], 6.0, places=3)

    def test_the_distance_to_the_wall_never_grows_inside_a_revolution(self) -> None:
        """The defining property: a spiral only ever moves inwards as it goes round."""

        region, outcome = _plan("square", {})
        polygon = region.boundary()
        for move in _cuts(outcome)[1:]:
            with self.subTest(pass_index=move.pass_index):
                distances = distance_to_boundary(move.points[:, :2], polygon)
                self.assertLessEqual(float(distances.max() - distances.min()), 6.0 + 0.01)
                self.assertGreaterEqual(float(distances[-1] - distances[0]), 5.9)

    def test_it_walks_the_same_offsets_as_contour(self) -> None:
        """Both planners share `ContourPlanner.ring_layers`, and the spiral reaches the same core."""

        for shape, parameters in SINGLE_FRONTED:
            with self.subTest(shape=shape):
                region, spiral = _plan(shape, parameters)
                _same, contour = _plan(shape, parameters, planner_id="contour")
                polygon = region.boundary()
                deepest = max(
                    float(distance_to_boundary(move.points[:, :2], polygon).max())
                    for move in _cuts(spiral)
                )
                plain = max(
                    float(distance_to_boundary(move.points[:, :2], polygon).max())
                    for move in _cuts(contour)
                )
                # The projection and the polygonal rings move points by a few hundredths of a
                # millimetre -- always *inwards*, so they can never touch the wall.
                self.assertAlmostEqual(deepest, plain, delta=0.05)


class CoverageTests(unittest.TestCase):
    def test_a_single_fronted_shape_keeps_contours_coverage(self) -> None:
        for shape, parameters in SINGLE_FRONTED:
            with self.subTest(shape=shape):
                region, spiral = _plan(shape, parameters)
                _same, contour = _plan(shape, parameters, planner_id="contour")
                spiral_coverage = measure_coverage(spiral.toolpath, region, _tool()).ratio
                contour_coverage = measure_coverage(contour.toolpath, region, _tool()).ratio
                self.assertGreaterEqual(spiral_coverage, contour_coverage - 0.02)
                self.assertGreater(spiral_coverage, 0.95)

    def test_the_notes_state_the_comparison(self) -> None:
        _region, outcome = _plan("circle", {"diameter_mm": 80.0})
        self.assertTrue(
            any("螺旋与环切的覆盖率对比" in note for note in outcome.toolpath.notes),
            outcome.toolpath.notes,
        )


class FallbackTests(unittest.TestCase):
    """A thin wall is cut from two fronts at once, which one spiral cannot sweep."""

    def test_a_double_fronted_shape_falls_back_to_ring_by_ring_contouring(self) -> None:
        for shape, parameters in DOUBLE_FRONTED:
            with self.subTest(shape=shape):
                _region, spiral = _plan(shape, parameters)
                _same, contour = _plan(shape, parameters, planner_id="contour")
                self.assertEqual(len(spiral.toolpath.moves), len(contour.toolpath.moves))
                for mine, theirs in zip(spiral.toolpath.moves, contour.toolpath.moves):
                    self.assertTrue(np.allclose(mine.points, theirs.points))
                # Still the spiral planner's own result, but with a note saying what happened.
                self.assertEqual(spiral.toolpath.planner, "spiral")
                self.assertTrue(
                    any("此形状不适合螺旋" in note for note in spiral.toolpath.notes),
                    spiral.toolpath.notes,
                )

    def test_the_fallback_note_gives_both_coverage_numbers(self) -> None:
        _region, outcome = _plan("u_shape", {})
        note = next(note for note in outcome.toolpath.notes if "此形状不适合螺旋" in note)
        self.assertIn("两面同时进刀", note)
        self.assertIn("%", note)
        self.assertIn("已按环切逐圈走", note)

    def test_the_fallback_is_not_a_failure(self) -> None:
        region, outcome = _plan("dumbbell", {})
        coverage = measure_coverage(outcome.toolpath, region, _tool())
        self.assertGreater(coverage.ratio, 0.95)
        self.assertGreater(outcome.toolpath.pass_count, 0)


class DirectionTests(unittest.TestCase):
    def test_a_spiral_cannot_alternate_and_says_so(self) -> None:
        _region, outcome = _plan("square", {}, parameters={"ring_direction": "alternate"})
        self.assertTrue(
            any("螺旋必须同向" in note for note in outcome.toolpath.notes),
            outcome.toolpath.notes,
        )
        self.assertFalse(
            any("螺旋必须同向" in note for note in _plan(
                "square", {}, parameters={"ring_direction": "climb"}
            )[1].toolpath.notes)
        )

    def test_climb_and_conventional_are_honoured(self) -> None:
        region, climb = _plan("square", {}, parameters={"ring_direction": "climb"})
        _same, conventional = _plan("square", {}, parameters={"ring_direction": "conventional"})
        first_climb = _cuts(climb)[0].points[:, :2]
        first_conventional = _cuts(conventional)[0].points[:, :2]
        self.assertGreater(signed_area(first_climb), 0.0)
        self.assertLess(signed_area(first_conventional), 0.0)
        # Both keep the same geometry, only the direction of travel differs.
        self.assertAlmostEqual(
            distance_to_boundary(first_climb, region.boundary()).mean(),
            distance_to_boundary(first_conventional, region.boundary()).mean(),
            places=6,
        )


class ParameterTests(unittest.TestCase):
    def test_it_shares_the_contour_parameters(self) -> None:
        entries = {entry["id"]: entry for entry in planner_catalog()}
        self.assertEqual(
            [item["key"] for item in entries["spiral"]["parameters"]],
            [item["key"] for item in entries["contour"]["parameters"]],
        )

    def test_impossible_geometry_is_reported(self) -> None:
        """A cutter wider than the region has no first offset to walk, in either contour mode."""

        region = build_region("square", {"side_mm": 20.0})
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="spiral", tool=_tool(diameter_mm=30.0), region=region,
                parameters={"stepover_mm": 6.0, "sample_step_mm": 1.0},
            )


if __name__ == "__main__":
    unittest.main()
