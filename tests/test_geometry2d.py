"""扫描线裁剪、多边形规范化与等距偏置——栅格与跟随周边共用的几何底座。"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.region import build_region
from toolpath_lab.planning.geometry2d import (
    bounding_box,
    distance_to_boundary,
    ensure_ccw,
    offset_polygon,
    resample_ring,
    scanline_intervals,
    signed_area,
)


def _square(side: float = 80.0) -> np.ndarray:
    return ensure_ccw(build_region("square", {"side_mm": side}).boundary())


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
        """凹形状是保留能力：新增形状不需要改裁剪代码。"""

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


class OffsetTests(unittest.TestCase):
    def test_inward_offset_of_a_square_keeps_sharp_corners(self) -> None:
        ring = offset_polygon(_square(80.0), 10.0)
        self.assertEqual(ring.shape, (4, 2))
        self.assertAlmostEqual(signed_area(ring), 60.0 ** 2, places=6)

    def test_outward_offset_of_a_square_rounds_the_corners(self) -> None:
        # 外扩 = 长方形与圆盘的闵可夫斯基和：S + 4·a·d + π·d²
        ring = offset_polygon(_square(80.0), -10.0)
        expected = 80.0 ** 2 + 4.0 * 80.0 * 10.0 + pi * 100.0
        self.assertGreater(ring.shape[0], 4)
        self.assertAlmostEqual(signed_area(ring), expected, delta=0.5)

    def test_every_offset_point_keeps_the_requested_distance(self) -> None:
        polygon = _square(80.0)
        ring = offset_polygon(polygon, 7.0)
        distances = distance_to_boundary(ring, polygon)
        self.assertGreaterEqual(float(distances.min()), 7.0 - 1e-3)
        self.assertLessEqual(float(distances.max()), 7.0 + 1e-3)

    def test_offset_beyond_the_inradius_collapses(self) -> None:
        self.assertIsNone(offset_polygon(_square(80.0), 41.0))

    def test_zero_offset_returns_the_same_polygon(self) -> None:
        polygon = _square(30.0)
        self.assertTrue(np.allclose(offset_polygon(polygon, 0.0), polygon))

    def test_circle_offset_shrinks_the_diameter(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())
        x_min, x_max, _, _ = bounding_box(offset_polygon(polygon, 5.0))
        self.assertAlmostEqual(x_max - x_min, 70.0, delta=0.1)


class ResampleTests(unittest.TestCase):
    def test_ring_is_resampled_by_arc_length(self) -> None:
        self.assertEqual(resample_ring(_square(80.0), 5.0).shape, (64, 2))

    def test_resampled_points_are_evenly_spaced(self) -> None:
        ring = resample_ring(_square(80.0), 7.0)
        closed = np.vstack([ring, ring[:1]])
        steps = np.linalg.norm(np.diff(closed, axis=0), axis=1)
        self.assertAlmostEqual(float(steps.max()), 320.0 / ring.shape[0], places=6)

    def test_first_point_is_not_repeated(self) -> None:
        ring = resample_ring(_square(80.0), 5.0)
        self.assertGreater(float(np.linalg.norm(ring[0] - ring[-1])), 1e-6)


if __name__ == "__main__":
    unittest.main()
