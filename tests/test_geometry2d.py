"""扫描线裁剪与多边形规范化——栅格刀路的几何底座。"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.region import build_region
from toolpath_lab.planning.geometry2d import (
    bounding_box,
    ensure_ccw,
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


if __name__ == "__main__":
    unittest.main()
