"""Scanline clipping, polygon normalisation, and the inward offset (including split concave loops)."""

from __future__ import annotations

import unittest
from math import pi
from unittest import mock

import numpy as np

from toolpath_lab.core.region import build_region
from toolpath_lab.planning import geometry2d
from toolpath_lab.planning.geometry2d import (
    _candidate_pairs,
    _nearest_edge_distance,
    boundary_edges,
    bounding_box,
    distance_to_boundary,
    distance_to_edges,
    ensure_ccw,
    offset_loops,
    offset_polygon,
    offset_primitives,
    point_in_polygon,
    resample_ring,
    scanline_intervals,
    signed_area,
)


def _square(side: float = 80.0) -> np.ndarray:
    return ensure_ccw(build_region("square", {"side_mm": side}).boundary())


def _densify(loop: np.ndarray, step: float) -> np.ndarray:
    """Sample densely along every edge of a loop: checking only the vertices is not enough."""

    out = []
    for start, end in zip(loop, np.roll(loop, -1, axis=0)):
        count = max(2, int(np.ceil(np.linalg.norm(end - start) / step)))
        out.extend(start + t * (end - start) for t in np.linspace(0.0, 1.0, count))
    return np.array(out)


def _self_intersections(polygon: np.ndarray) -> int:
    """Count the intersections between non-adjacent edges."""

    count = polygon.shape[0]
    found = 0
    for i in range(count):
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            a, b = polygon[i], polygon[(i + 1) % count]
            c, d = polygon[j], polygon[(j + 1) % count]
            r, s = b - a, d - c
            denominator = r[0] * s[1] - r[1] * s[0]
            if abs(denominator) < 1e-12:
                continue
            qp = c - a
            t = (qp[0] * s[1] - qp[1] * s[0]) / denominator
            u = (qp[0] * r[1] - qp[1] * r[0]) / denominator
            if 1e-9 < t < 1 - 1e-9 and 1e-9 < u < 1 - 1e-9:
                found += 1
    return found


class OrientationTests(unittest.TestCase):
    def test_ensure_ccw_flips_clockwise_input(self) -> None:
        self.assertGreater(signed_area(ensure_ccw(_square()[::-1])), 0.0)

    def test_ensure_ccw_drops_a_repeated_closing_point(self) -> None:
        polygon = np.array(
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
        )
        self.assertEqual(ensure_ccw(polygon).shape[0], 4)

    def test_ensure_ccw_drops_repeated_neighbours(self) -> None:
        polygon = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 0.0], [0.0, 10.0]])
        self.assertEqual(ensure_ccw(polygon).shape[0], 3)

    def test_degenerate_polygon_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ensure_ccw(np.array([[0.0, 0.0], [1.0, 1.0]]))


class ScanlineTests(unittest.TestCase):
    def test_square_yields_one_interval_per_line(self) -> None:
        polygon = _square(80.0)
        intervals = scanline_intervals(polygon, 0.0)
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0].start, -40.0)
        self.assertAlmostEqual(intervals[0].end, 40.0)

    def test_line_outside_the_region_yields_nothing(self) -> None:
        self.assertEqual(scanline_intervals(_square(80.0), 100.0), [])

    def test_circle_chord_length_follows_the_geometry(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())
        intervals = scanline_intervals(polygon, 0.0)
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0].start, -40.0, places=3)
        self.assertAlmostEqual(intervals[0].end, 40.0, places=3)
        offset = scanline_intervals(polygon, 20.0)
        expected = (40.0 ** 2 - 20.0 ** 2) ** 0.5
        self.assertAlmostEqual(offset[0].end, expected, places=2)

    def test_a_concave_polygon_yields_several_intervals(self) -> None:
        """Concave shapes are a standing capability: a new shape needs no change to the clipping code."""

        u_shape = np.array(
            [[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [20.0, 30.0],
             [20.0, 10.0], [10.0, 10.0], [10.0, 30.0], [0.0, 30.0]]
        )
        intervals = scanline_intervals(ensure_ccw(u_shape), 20.0)
        self.assertEqual(len(intervals), 2)
        self.assertLess(intervals[0].end, intervals[1].start)

    def test_intervals_are_ordered(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())
        for level in np.linspace(-39.0, 39.0, 11):
            intervals = scanline_intervals(polygon, float(level))
            for first, second in zip(intervals, intervals[1:]):
                self.assertLess(first.end, second.start)


class HelperTests(unittest.TestCase):
    def test_bounding_box(self) -> None:
        self.assertEqual(bounding_box(_square(20.0)), (-10.0, 10.0, -10.0, 10.0))

    def test_signed_area_of_a_circle_approximation(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 40.0}).boundary())
        self.assertAlmostEqual(signed_area(polygon), pi * 400.0, delta=1.0)


class MeasureTests(unittest.TestCase):
    def test_distance_to_boundary(self) -> None:
        polygon = _square(80.0)
        points = np.array([[0.0, 0.0], [39.0, 0.0], [50.0, 0.0]])
        self.assertTrue(
            np.allclose(distance_to_boundary(points, polygon), [40.0, 1.0, 10.0])
        )

    def test_point_in_polygon(self) -> None:
        polygon = _square(80.0)
        points = np.array([[0.0, 0.0], [39.0, 39.0], [41.0, 0.0], [0.0, -41.0]])
        self.assertEqual(point_in_polygon(points, polygon).tolist(),
                         [True, True, False, False])


class OffsetLoopTests(unittest.TestCase):
    """Inward offset: it matches the analytic values and no edge crosses the boundary."""

    def test_square_offset_is_exact(self) -> None:
        for distance, side in ((3.0, 74.0), (10.0, 60.0), (21.0, 38.0), (33.0, 14.0)):
            with self.subTest(distance=distance):
                loop = offset_polygon(_square(80.0), distance)
                self.assertIsNotNone(loop)
                self.assertAlmostEqual(signed_area(loop), side * side, places=6)

    def test_rectangle_offset_is_exact(self) -> None:
        polygon = ensure_ccw(
            build_region("rectangle", {"width_mm": 100.0, "height_mm": 60.0}).boundary()
        )
        loop = offset_polygon(polygon, 10.0)
        self.assertAlmostEqual(signed_area(loop), 80.0 * 40.0, places=6)

    def test_circle_offset_keeps_the_distance(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())
        loop = offset_polygon(polygon, 10.0)
        radii = np.linalg.norm(loop, axis=1)
        self.assertAlmostEqual(float(radii.min()), 30.0, places=2)
        self.assertAlmostEqual(float(radii.max()), 30.0, places=2)

    def test_offset_past_the_inradius_is_empty(self) -> None:
        self.assertIsNone(offset_polygon(_square(80.0), 41.0))

    def test_zero_offset_returns_the_normalised_polygon(self) -> None:
        loop = offset_polygon(_square(80.0), 0.0)
        self.assertAlmostEqual(signed_area(loop), 6400.0, places=6)

    def test_outward_offset_is_rejected(self) -> None:
        # An outward offset needs arcs at convex corners, a different geometry: a negative value raises instead
        with self.assertRaises(ValueError):
            offset_loops(_square(80.0), -10.0)

    def test_tiny_loops_can_be_filtered_by_area(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        self.assertEqual(len(offset_loops(polygon, 27.0)), 2)
        self.assertEqual(offset_loops(polygon, 27.0, min_area_mm2=100.0), [])

    def test_every_loop_keeps_the_distance_along_its_edges(self) -> None:
        """The key invariant: it is not enough for the vertices to stay inside, no edge may cross the boundary.

        An early version re-connected the vertices left after the neck was eaten in their original order:
        every vertex passed, yet an edge crossed the neck, its midpoint only half the neck from the outline.
        """

        for shape_id in ("square", "circle", "rectangle", "ellipse", "u_shape", "dumbbell"):
            polygon = ensure_ccw(build_region(shape_id, {}).boundary())
            for distance in (3.0, 9.0, 15.0, 21.0):
                for loop in offset_loops(polygon, distance):
                    with self.subTest(shape=shape_id, distance=distance):
                        gaps = distance_to_boundary(_densify(loop, 0.05), polygon)
                        self.assertGreaterEqual(float(gaps.min()), distance - 0.02)

    def test_loops_are_simple_and_counter_clockwise(self) -> None:
        for shape_id, distance in (("dumbbell", 15.0), ("u_shape", 3.0), ("circle", 9.0),
                                   ("ellipse", 21.0)):
            polygon = ensure_ccw(build_region(shape_id, {}).boundary())
            for loop in offset_loops(polygon, distance):
                with self.subTest(shape=shape_id, distance=distance):
                    self.assertGreater(signed_area(loop), 0.0)
                    self.assertEqual(_self_intersections(loop), 0)

    def test_a_thin_neck_splits_the_offset_into_several_loops(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        # Neck 20 wide: it survives offsets 3 and 9 (one loop) and is eaten from 15 on, leaving one loop per pad.
        self.assertEqual(len(offset_loops(polygon, 3.0)), 1)
        self.assertEqual(len(offset_loops(polygon, 9.0)), 1)
        for distance in (15.0, 21.0, 27.0):
            with self.subTest(distance=distance):
                loops = offset_loops(polygon, distance)
                self.assertEqual(len(loops), 2)
                self.assertAlmostEqual(signed_area(loops[0]), signed_area(loops[1]), places=6)

    def test_the_split_loops_cover_the_whole_offset_region(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        loops = offset_loops(polygon, 15.0)
        # Pad 60 - 2x15 = 30, so 30x30 = 900, plus the reflex arc bulges of about 24
        self.assertAlmostEqual(
            sum(signed_area(loop) for loop in loops), 2 * 924.0, delta=2.0
        )

    def test_a_u_shape_offset_keeps_the_reflex_arc_bulges(self) -> None:
        polygon = ensure_ccw(build_region("u_shape", {}).boundary())
        loop = offset_polygon(polygon, 3.0)
        # The right-angled version is 94x74 - 56x55 = 3876, plus one arc bulge 3² - π·3²/4 per reflex corner
        bulge = 2.0 * (9.0 - pi * 9.0 / 4.0)
        self.assertAlmostEqual(signed_area(loop), 3876.0 + bulge, delta=0.5)

    def test_offset_polygon_returns_the_largest_loop(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        largest = offset_polygon(polygon, 15.0)
        self.assertAlmostEqual(signed_area(largest), signed_area(offset_loops(polygon, 15.0)[0]))


def _triangle(width: float, height: float) -> np.ndarray:
    """Isosceles triangle with base `width` and height `height` (apex pointing up)."""

    half = width / 2.0
    return ensure_ccw(np.array([[-half, 0.0], [half, 0.0], [0.0, height]]))


def _slab_with_fin() -> np.ndarray:
    """A 100 x 60 slab with a small 4 x 10 fin on top (apex angle about 22.6°).

    The fin's sides are about 10.2 long while its miter point needs 3·tan(78.7°) ≈ 15 of extension, so
    the miter falls outside the shifted segment, where the fin is only 2L·sin(11.3°) ≈ 4 < 6 = 2d wide.
    """

    return ensure_ccw(
        np.array(
            [
                [-50.0, -30.0],
                [50.0, -30.0],
                [50.0, 30.0],
                [2.0, 30.0],
                [0.0, 40.0],
                [-2.0, 30.0],
                [-50.0, 30.0],
            ]
        )
    )


class SharpCornerTests(unittest.TestCase):
    """Offsetting at sharp corners: the miter point can sit far from the vertex, which is never about length."""

    def test_a_sharp_apex_still_closes_the_ring(self) -> None:
        # With an apex angle of 18.9° the miter point sits about 6x the offset away, and it still closes
        for width, height in ((30.0, 90.0), (15.0, 120.0), (8.0, 120.0)):
            polygon = _triangle(width, height)
            with self.subTest(width=width, height=height):
                loops = offset_loops(polygon, 3.0)
                self.assertEqual(len(loops), 1)
                self.assertGreaterEqual(
                    float(distance_to_boundary(_densify(loops[0], 0.05), polygon).min()),
                    3.0 - 0.02,
                )

    def test_the_eroded_triangle_matches_the_analytic_area(self) -> None:
        """The erosion of a triangle is a similar triangle: area = original × ((r − d) / r)², r the inradius."""

        width, height = 8.0, 120.0
        polygon = _triangle(width, height)
        loop = offset_polygon(polygon, 3.0)
        area = 0.5 * width * height
        side = float(np.hypot(width / 2.0, height))
        inradius = area / ((width + 2.0 * side) / 2.0)
        self.assertAlmostEqual(
            signed_area(loop), area * ((inradius - 3.0) / inradius) ** 2, delta=0.5
        )

    def test_a_needle_that_leaves_nothing_reports_no_ring(self) -> None:
        # A needle-thin triangle with a 1.9° apex: its inradius is below the offset, so nothing is left
        self.assertEqual(offset_loops(_triangle(4.0, 120.0), 3.0), [])

    def test_a_feature_narrower_than_the_tool_just_disappears(self) -> None:
        """A fin narrower than the tool leaves no fragments and does not break the loop: it simply disappears."""

        polygon = _slab_with_fin()
        loops = offset_loops(polygon, 3.0)
        self.assertEqual(len(loops), 1)
        loop = loops[0]
        # The fin disappears completely: the offset boundary stops at the inset top face of the slab
        self.assertLess(float(loop[:, 1].max()), 28.0)
        self.assertGreater(float(loop[:, 1].min()), -28.0)
        # The remaining loop is still nowhere closer to the outline than the offset distance
        self.assertGreaterEqual(
            float(distance_to_boundary(_densify(loop, 0.05), polygon).min()), 3.0 - 0.02
        )


class ResampleTests(unittest.TestCase):
    def test_ring_is_resampled_at_even_arc_length(self) -> None:
        sampled = resample_ring(_square(80.0), 5.0)
        self.assertEqual(sampled.shape[0], 64)  # 周长 320 / 5
        closed = np.vstack([sampled, sampled[:1]])
        gaps = np.linalg.norm(np.diff(closed, axis=0), axis=1)
        self.assertTrue(bool(np.allclose(gaps, 5.0, atol=1e-9)))

    def test_resampled_ring_does_not_repeat_the_first_point(self) -> None:
        sampled = resample_ring(_square(80.0), 5.0)
        self.assertFalse(bool(np.allclose(sampled[0], sampled[-1])))

    def test_resampling_keeps_the_length(self) -> None:
        sampled = resample_ring(_square(80.0), 1.0)
        closed = np.vstack([sampled, sampled[:1]])
        self.assertAlmostEqual(
            float(np.linalg.norm(np.diff(closed, axis=0), axis=1).sum()), 320.0, places=6
        )


class PrefilterTests(unittest.TestCase):
    """The cheap-before-expensive prunings: each must agree with the full computation, or it is a wrong one."""

    def _grid(self, polygon: np.ndarray, step: float = 3.0) -> np.ndarray:
        x_min, x_max, y_min, y_max = bounding_box(polygon)
        xs = np.arange(x_min - step, x_max + step, step)
        ys = np.arange(y_min - step, y_max + step, step)
        grid_x, grid_y = np.meshgrid(xs, ys)
        return np.column_stack((grid_x.ravel(), grid_y.ravel()))

    def test_the_line_prefilter_never_loses_the_nearest_edge(self) -> None:
        """The distance to a segment is never smaller than to its supporting line, so the nearest edge always survives

        the line filter. Dropped edges can only make the minimum larger, never smaller, so for every point
        whose full distance is within target + slack must equal the full computation bit for bit.
        """

        for shape_id, parameters in (
            ("square", {}),
            ("u_shape", {}),
            ("circle", {"diameter_mm": 80.0}),
        ):
            polygon = ensure_ccw(build_region(shape_id, parameters).boundary())
            edges = boundary_edges(polygon)
            points = self._grid(polygon)
            with self.subTest(shape=shape_id):
                exact = distance_to_edges(points, edges)
                # With every edge as a candidate the result must be identical
                self.assertTrue(
                    np.array_equal(_nearest_edge_distance(points, edges, 0.0, np.inf), exact)
                )
                # With a real tolerance, points whose full distance is within target + slack must match as well
                target, slack = 3.0, 1e-3
                prefixed = _nearest_edge_distance(points, edges, target, slack)
                keep = exact <= target + slack
                self.assertGreater(int(keep.sum()), 0)
                self.assertTrue(np.array_equal(prefixed[keep], exact[keep]))

    def test_the_bucket_prunes_pairs_but_keeps_every_true_crossing(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())
        for distance in (3.0, 33.0):
            primitives = offset_primitives(polygon, distance)
            starts = np.array([item[0] for item in primitives], dtype=np.float64)
            ends = np.array([item[1] for item in primitives], dtype=np.float64)
            count = starts.shape[0]
            spans = ends - starts
            i_flat, j_flat = _candidate_pairs(starts, ends)
            candidates = set(zip(i_flat.tolist(), j_flat.tolist()))

            # All-pairs intersection, as the reference for "true crossings"
            true_pairs = set()
            for first in range(count):
                for second in range(first + 1, count):
                    r = spans[first]
                    s = spans[second]
                    denominator = r[0] * s[1] - r[1] * s[0]
                    if abs(denominator) <= 1e-12:
                        continue
                    delta = starts[second] - starts[first]
                    t = (delta[0] * s[1] - delta[1] * s[0]) / denominator
                    u = (delta[0] * r[1] - delta[1] * r[0]) / denominator
                    if 1e-12 < t < 1 - 1e-12 and 1e-12 < u < 1 - 1e-12:
                        true_pairs.add((first, second))

            with self.subTest(distance=distance):
                self.assertTrue(true_pairs, "参照集不该为空")
                self.assertTrue(true_pairs <= candidates, "候选必须覆盖所有真交点")
                all_pairs = count * (count - 1) // 2
                # At small offsets the shifted segments spread out, at large ones they shrink to the centre; both prune well
                self.assertLess(len(candidates), all_pairs * 0.5)
                self.assertGreaterEqual(len(candidates), len(true_pairs))

    def test_filtering_crossings_does_not_change_the_result(self) -> None:
        """Only crossings on the offset boundary separate valid from invalid sub-segments, so dropping the rest changes nothing."""

        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())

        def keep_everything(*_args, **_kwargs):
            return lambda points: np.ones(points.shape[0], dtype=bool)

        for distance in (3.0, 15.0, 33.0):
            with self.subTest(distance=distance):
                with mock.patch.object(geometry2d, "_crossing_filter", keep_everything):
                    unfiltered = offset_loops(polygon, distance, min_area_mm2=0.1)
                filtered = offset_loops(polygon, distance, min_area_mm2=0.1)
                self.assertEqual(len(filtered), len(unfiltered))
                for kept, reference in zip(filtered, unfiltered):
                    self.assertTrue(np.array_equal(kept, reference))


if __name__ == "__main__":
    unittest.main()
