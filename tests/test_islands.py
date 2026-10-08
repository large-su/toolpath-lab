"""Islands: the outward rings, the midline trimming, and what the planners do around a hole."""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.region import build_region, region_from_points
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import measure_coverage, run_plan
from toolpath_lab.planning.geometry2d import distance_to_boundary, ensure_ccw, offset_loops, resample_ring
from toolpath_lab.planning.islands import (
    connector_is_clear,
    distance_to_polygons,
    outward_ring,
    sample_line,
    split_runs,
    trim_ring,
)

TOOL = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
RADIUS = 3.0

#: The analytic island case: a round pocket 100 across with a concentric boss 30 across.
RING = build_region("ring", {"diameter_mm": 100.0, "island_diameter_mm": 30.0})
ISLAND = ensure_ccw(RING.islands()[0])


def _cut_points(toolpath) -> np.ndarray:
    """Every cutting point of a toolpath, as XY pairs."""

    points = [
        np.asarray(move.points, dtype=np.float64)[:, :2]
        for move in toolpath.moves
        if move.is_cutting
    ]
    return np.vstack(points) if points else np.zeros((0, 2))


class OutwardRingTests(unittest.TestCase):
    """The ring `distance` outside an island, which `offset_loops` cannot produce (inward only)."""

    def test_a_circular_island_gives_a_bigger_circle(self) -> None:
        """The island is a polygon, so the ring follows its edges: the *distance* is exact, the radius
        is the circle's within the polygon's own sagitta."""

        for distance in (1.0, 3.0, 9.0, 17.5):
            with self.subTest(distance=distance):
                ring = outward_ring(ISLAND, distance)
                radii = np.linalg.norm(ring, axis=1)
                self.assertAlmostEqual(float(radii.mean()), 15.0 + distance, delta=0.01)
                gaps = distance_to_boundary(ring, ISLAND)
                self.assertAlmostEqual(float(gaps.min()), distance, places=6)
                self.assertAlmostEqual(float(gaps.max()), distance, places=6)

    def test_a_convex_corner_gets_an_arc_at_the_same_distance(self) -> None:
        square = np.array([[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0]])
        for distance in (2.0, 6.0):
            with self.subTest(distance=distance):
                ring = outward_ring(square, distance)
                gaps = distance_to_boundary(ring, square)
                self.assertAlmostEqual(float(gaps.min()), distance, places=6)
                self.assertAlmostEqual(float(gaps.max()), distance, places=6)
                # The arc really is an arc: the ring has more points than the four shifted corners.
                self.assertGreater(ring.shape[0], 4)

    def test_a_reflex_corner_keeps_the_miter_at_the_same_distance(self) -> None:
        elbow = np.array(
            [[0.0, 0.0], [30.0, 0.0], [30.0, 10.0], [10.0, 10.0], [10.0, 30.0], [0.0, 30.0]]
        )
        ring = outward_ring(elbow, 3.0)
        gaps = distance_to_boundary(ring, elbow)
        self.assertAlmostEqual(float(gaps.min()), 3.0, places=6)

    def test_the_ring_stays_outside_the_island(self) -> None:
        for distance in (1.0, 8.0):
            with self.subTest(distance=distance):
                ring = outward_ring(ISLAND, distance)
                self.assertGreaterEqual(float(distance_to_boundary(ring, ISLAND).min()), distance - 1e-9)
                # Counter-clockwise like every other ring in the project.
                from toolpath_lab.planning.geometry2d import signed_area

                self.assertGreater(float(signed_area(ring)), 0.0)

    def test_no_distance_returns_the_island_itself(self) -> None:
        self.assertTrue(np.allclose(outward_ring(ISLAND, 0.0), ISLAND))


class TrimmingTests(unittest.TestCase):
    """The midline rule: every ring keeps the half of the band it is closer to."""

    def test_without_islands_the_ring_is_untouched(self) -> None:
        ring = resample_ring(offset_loops(RING.boundary(), RADIUS)[0], 1.0)
        runs = trim_ring(ring, near=[RING.boundary()], far=[], clearance_mm=RADIUS)
        self.assertEqual(len(runs), 1)
        self.assertTrue(np.allclose(runs[0], ring))

    def test_a_far_away_island_changes_nothing_each_ring_keeps_its_half(self) -> None:
        # The outer ring at d = 3 (radius 47) is far closer to the outline than to the island: kept whole.
        outer = resample_ring(offset_loops(RING.boundary(), RADIUS)[0], 1.0)
        runs = trim_ring(outer, near=[RING.boundary()], far=[ISLAND], clearance_mm=RADIUS)
        self.assertEqual(len(runs), 1)
        radii = np.linalg.norm(np.asarray(runs[0]), axis=1)
        self.assertAlmostEqual(float(radii.mean()), 47.0, delta=0.01)
        # At radius 29 (d = 21 from the outline) the island side is nearer, so nothing is left.
        inner = resample_ring(offset_loops(RING.boundary(), 21.0)[0], 1.0)
        self.assertEqual(
            trim_ring(inner, near=[RING.boundary()], far=[ISLAND], clearance_mm=RADIUS), []
        )

    def test_the_kept_points_respect_the_clearance_and_the_midline(self) -> None:
        for distance in (3.0, 9.0, 15.0):
            with self.subTest(distance=distance):
                outer = resample_ring(offset_loops(RING.boundary(), distance)[0], 1.0)
                for run in trim_ring(outer, near=[RING.boundary()], far=[ISLAND],
                                     clearance_mm=RADIUS):
                    points = np.asarray(run)
                    to_island = distance_to_polygons(points, [ISLAND])
                    to_outline = distance_to_polygons(points, [RING.boundary()])
                    self.assertGreaterEqual(float(to_island.min()), RADIUS - 1e-6)
                    self.assertLessEqual(float(to_outline.max()), float(to_island.max()) + 0.05)

    def test_a_run_crossing_the_seam_comes_back_in_one_piece(self) -> None:
        points = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0], [4.0, 0.0]])
        keep = np.array([True, False, True, True, True])
        runs = split_runs(points, keep, closed=True)
        # The run that reaches the seam continues at the front: indices 2,3,4 and 0 together.
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].shape[0], 4)
        self.assertTrue(np.allclose(runs[0][-1], points[0]))

    def test_a_connector_through_an_island_is_not_clear(self) -> None:
        self.assertTrue(connector_is_clear([-40.0, 0.0], [-20.0, 0.0], [ISLAND], RADIUS, step_mm=1.0))
        self.assertFalse(connector_is_clear([-40.0, 0.0], [40.0, 0.0], [ISLAND], RADIUS, step_mm=1.0))
        self.assertTrue(connector_is_clear([-40.0, 0.0], [40.0, 0.0], [], RADIUS, step_mm=1.0))
        self.assertFalse(connector_is_clear([-20.0, 0.0], [20.0, 0.0], [ISLAND], RADIUS, step_mm=1.0))

    def test_sampling_a_line_includes_both_ends(self) -> None:
        points = sample_line(np.array([0.0, 0.0]), np.array([10.0, 0.0]), 3.0)
        self.assertTrue(np.allclose(points[0], [0.0, 0.0]))
        self.assertTrue(np.allclose(points[-1], [10.0, 0.0]))
        self.assertEqual(points.shape[0], 5)  # 0, 2.5, 5, 7.5, 10


class RingRegionTests(unittest.TestCase):
    def test_the_band_and_the_island_are_analytic(self) -> None:
        self.assertAlmostEqual(RING.annulus_width_mm, 35.0)
        described = RING.describe()
        self.assertAlmostEqual(described["area_mm2"], pi * 50.0**2 - pi * 15.0**2, delta=2.0)
        self.assertEqual(len(described["islands"]), 1)
        self.assertEqual(described["islands"][0]["point_count"], 180)
        self.assertAlmostEqual(described["islands"][0]["area_mm2"], pi * 15.0**2, delta=0.2)

    def test_the_island_has_to_fit_inside_the_outline(self) -> None:
        for parameters in ({"island_diameter_mm": 100.0}, {"island_diameter_mm": 120.0}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    build_region("ring", parameters)

    def test_an_imported_drawing_can_bring_its_islands(self) -> None:
        outer = [[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]]
        island = [[15.0, 15.0], [25.0, 15.0], [25.0, 25.0], [15.0, 25.0]]
        region = region_from_points(outer, [island])
        self.assertEqual(region.to_params(), {"point_count": 4, "island_count": 1})
        self.assertEqual(region.header_text(), "imported - 4 points + 1 islands")
        self.assertAlmostEqual(region.describe()["area_mm2"], 1500.0, places=6)
        self.assertEqual(len(region.islands()), 1)
        self.assertGreater(region.islands()[0].shape[0], 2)

    def test_a_bad_island_list_is_reported(self) -> None:
        outer = [[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]]
        for island in ([[1.0, 1.0], [2.0, 2.0]], [[1.0, 1.0], [2.0, 2.0], [3.0]]):
            with self.subTest(island=island):
                with self.assertRaises(ParameterError):
                    region_from_point_list(outer, island)


def region_from_point_list(outer, island):
    return region_from_points(outer, [island])


class PlannerIslandTests(unittest.TestCase):
    """What every strategy must do around an island: never touch it, and still cover the band."""

    PLANNERS = ("raster", "contour", "spiral", "adaptive_contour")

    def _plan(self, planner: str, stepover: float = 6.0, region=None):
        return run_plan(
            planner_id=planner,
            tool=TOOL,
            region=region or RING,
            parameters={"stepover_mm": stepover, "sample_step_mm": 1.0,
                        "depth_mm": 1.0, "stepdown_mm": 1.0},
        ).toolpath

    def test_no_cutting_point_ever_reaches_the_island(self) -> None:
        """The safety invariant: a hole is material that stays, so the cutter keeps its radius off it."""

        for planner in self.PLANNERS:
            for stepover in (3.0, 6.0, 9.0):
                with self.subTest(planner=planner, stepover=stepover):
                    points = _cut_points(self._plan(planner, stepover))
                    closest = float(distance_to_boundary(points, ISLAND).min())
                    self.assertGreaterEqual(closest, RADIUS - 1e-6)

    def test_the_island_is_not_counted_as_uncut_material(self) -> None:
        for planner in self.PLANNERS:
            with self.subTest(planner=planner):
                coverage = measure_coverage(self._plan(planner), RING, TOOL)
                # The region is the outline minus the island, so its area leaves the boss out.
                self.assertAlmostEqual(
                    coverage.region_area_mm2, pi * 50.0**2 - pi * 15.0**2, delta=25.0
                )
                self.assertGreater(coverage.ratio, 0.95)

    def test_contouring_walks_a_ring_around_the_island(self) -> None:
        """The island ring at d = R is the circle 15 + 3 = 18, which is analytic for a round boss."""

        toolpath = self._plan("contour", 6.0)
        radii = []
        for move in toolpath.moves:
            if not move.is_cutting:
                continue
            points = np.asarray(move.points, dtype=np.float64)[:, :2]
            distances = np.linalg.norm(points, axis=1)
            if abs(float(distances.mean()) - 18.0) < 0.5:
                radii.append((float(distances.min()), float(distances.max())))
        self.assertTrue(radii)
        for low, high in radii:
            self.assertAlmostEqual(low, 18.0, delta=0.02)
            self.assertAlmostEqual(high, 18.0, delta=0.02)

    def test_a_raster_pass_is_broken_by_the_island_and_retracts(self) -> None:
        toolpath = self._plan("raster", 6.0)
        moves = toolpath.moves
        # A pass that meets the island ends there, and the next pass starts on the other side: that join
        # has to be a retract, not a link, or it would cut straight through the boss.
        straddling = 0
        for index, move in enumerate(moves):
            if move.kind.value != "cut":
                continue
            end = np.asarray(move.points, dtype=np.float64)[-1, :2]
            for following in moves[index + 1:]:
                if following.kind.value == "cut":
                    start = np.asarray(following.points, dtype=np.float64)[0, :2]
                    if np.linalg.norm(start - end) > 20.0:
                        straddling += 1
                    break
        self.assertGreater(straddling, 3)
        self.assertTrue(any("绕岛屿" in note for note in toolpath.notes))
        # The jump over the island is a rapid, so it does not cut: the island keeps its material.
        points = _cut_points(toolpath)
        self.assertGreaterEqual(float(distance_to_boundary(points, ISLAND).min()), RADIUS - 1e-6)

    def test_the_raster_no_longer_emits_two_point_passes(self) -> None:
        toolpath = self._plan("raster", 6.0)
        longest = max(
            move.points.shape[0] for move in toolpath.moves if move.is_cutting
        )
        self.assertGreater(longest, 4)

    def test_the_spiral_hands_over_to_contouring_and_says_so(self) -> None:
        toolpath = self._plan("spiral", 6.0)
        self.assertTrue(any("带岛屿" in note for note in toolpath.notes))
        # ... and it really is the contour result: same number of passes as the contour planner.
        self.assertEqual(toolpath.pass_count, self._plan("contour", 6.0).pass_count)

    def test_a_tiny_band_is_reported_instead_of_planned(self) -> None:
        tight = build_region("ring", {"diameter_mm": 20.0, "island_diameter_mm": 14.0})
        with self.assertRaises(Exception) as caught:
            self._plan("contour", 6.0, region=tight)
        self.assertIn("岛屿", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
