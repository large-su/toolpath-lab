"""Regression checks for spiral coverage and sampling-independent timing."""

import unittest

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan
from toolpath_lab.simulation import build_timeline


def spiral(shape="square", kind=ToolKind.FLAT, size=80.0, step=1.0):
    key = "side_mm" if shape == "square" else "diameter_mm"
    return run_plan(
        planner_id="spiral", tool=Tool(kind, diameter_mm=6.0, length_mm=30.0),
        region=build_region(shape, {key: size}),
        parameters={"sample_step_mm": step},
    ).toolpath


class SpiralRegressionTests(unittest.TestCase):
    def test_square_footprint_stays_inside_every_edge(self):
        for kind in (ToolKind.FLAT, ToolKind.BALL, ToolKind.BULL):
            with self.subTest(kind=kind):
                points = spiral(kind=kind).moves[1].points[:, :2]
                # Check the actual footprint, including the default bull radius.
                actual = Tool(kind, diameter_mm=6.0, length_mm=30.0).footprint_radius_mm
                self.assertLessEqual(float(np.abs(points).max()), 40.0 - actual + 1e-8)
                self.assertTrue(np.allclose(points[0], 0.0))

    def test_finishing_pass_visits_all_four_inset_corners(self):
        points = spiral(step=20.0).moves[1].points[:, :2]
        for x in (-37.0, 37.0):
            for y in (-37.0, 37.0):
                self.assertLess(float(np.linalg.norm(points - [x, y], axis=1).min()), 1e-8)

    def test_default_square_covers_a_grid_of_reachable_centres(self):
        points = spiral().moves[1].points[:, :2]
        starts, delta = points[:-1], np.diff(points, axis=0)
        squared = np.maximum(np.sum(delta * delta, axis=1), 1e-20)
        for x in np.linspace(-37.0, 37.0, 31):
            grid = np.column_stack((np.full(31, x), np.linspace(-37.0, 37.0, 31)))
            offset = grid[:, None, :] - starts
            ratios = np.clip(np.sum(offset * delta, axis=2) / squared, 0.0, 1.0)
            distance = np.linalg.norm(offset - ratios[..., None] * delta, axis=2).min(axis=1)
            self.assertLessEqual(float(distance.max()), 3.0 + 1e-6)

    def test_circle_is_inside_the_inset_polygon(self):
        region = build_region("circle")
        boundary = region.boundary()
        edges = np.roll(boundary, -1, axis=0) - boundary
        normals = np.column_stack((edges[:, 1], -edges[:, 0]))
        normals /= np.linalg.norm(normals, axis=1)[:, None]
        limits = np.sum(normals * boundary, axis=1) - 3.0
        points = spiral("circle").moves[1].points[:, :2]
        self.assertTrue(np.all(points @ normals.T <= limits + 1e-8))

    def test_sample_step_is_an_upper_bound(self):
        for shape in ("square", "circle"):
            for step in (0.1, 1.0, 20.0):
                with self.subTest(shape=shape, step=step):
                    points = spiral(shape, step=step).moves[1].points
                    self.assertLessEqual(float(np.linalg.norm(np.diff(points, axis=0), axis=1).max()), step + 1e-8)

    def test_small_but_feasible_region_is_supported(self):
        path = spiral(size=7.0)
        self.assertGreater(path.moves[1].length_mm, 0.0)
        self.assertLessEqual(float(np.abs(path.moves[1].points[:, :2]).max()), 0.5 + 1e-8)

    def test_excessive_sampling_is_rejected_before_allocation(self):
        with self.assertRaisesRegex(PlanningError, "采样点过多"):
            run_plan(planner_id="spiral",
                     tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
                     region=build_region("square", {"side_mm": 1000.0}),
                     parameters={"stepover_mm": 0.5, "sample_step_mm": 0.1})

    def test_oversized_tool_raises_planning_error_for_both_strategies(self):
        for planner in ("spiral", "contour"):
            with self.subTest(planner=planner), self.assertRaises(PlanningError):
                run_plan(planner_id=planner,
                         tool=Tool(ToolKind.FLAT, diameter_mm=20.0, length_mm=30.0),
                         region=build_region("square", {"side_mm": 10.0}))


class TimelineRegressionTests(unittest.TestCase):
    def test_spiral_duration_does_not_depend_on_sampling_budget(self):
        path = spiral()
        for budget in (1, 6, 10, 50, 4000):
            with self.subTest(budget=budget):
                timeline = build_timeline(path, max_samples=budget)
                self.assertAlmostEqual(timeline.duration_s, path.estimated_time_s, places=9)
                self.assertLessEqual(timeline.sample_count, max(budget, 2 * len(path.moves)))

    def test_sharp_turn_time_uses_original_length(self):
        move = Move(MoveKind.RAPID, np.array([[0, 0, 0], [0, 0, 5],
                                            [10, 0, 5], [10, 0, 0]]), 600.0)
        timeline = build_timeline(Toolpath(moves=(move,)), max_samples=2)
        self.assertAlmostEqual(timeline.duration_s, 2.0)

    def test_shared_timestamp_belongs_to_the_new_move(self):
        path = spiral()
        timeline = build_timeline(path, max_samples=10)
        boundary = path.moves[0].duration_s
        for time in (boundary, boundary + 1e-6):
            state = timeline.state_at(time)
            self.assertEqual(state.kind, "cut")
            self.assertEqual(state.move_index, 1)
        self.assertEqual(timeline.state_at(boundary - 1e-6).kind, "rapid")
        self.assertEqual(timeline.state_at(timeline.duration_s).move_index, 2)

    def test_zero_length_moves_do_not_break_interpolation(self):
        zero = Move(MoveKind.LINK, np.zeros((2, 3)), 600.0)
        cut = Move(MoveKind.CUT, np.array([[0, 0, 0], [10, 0, 0]]), 600.0)
        timeline = build_timeline(Toolpath(moves=(zero, cut, zero)), max_samples=6)
        self.assertTrue(np.all(np.isfinite(timeline.state_at(0.5).position)))
        self.assertTrue(np.allclose(timeline.state_at(0.5).position, [5.0, 0.0, 0.0]))
        self.assertEqual(timeline.state_at(0.0).kind, "cut")


if __name__ == "__main__":
    unittest.main()
