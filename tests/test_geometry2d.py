"""扫描线裁剪、多边形规范化，以及向内偏置（含凹形状分裂出的多条环）。"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.region import build_region
from toolpath_lab.planning.geometry2d import (
    bounding_box,
    distance_to_boundary,
    ensure_ccw,
    offset_loops,
    offset_polygon,
    point_in_polygon,
    resample_ring,
    scanline_intervals,
    signed_area,
)


def _square(side: float = 80.0) -> np.ndarray:
    return ensure_ccw(build_region("square", {"side_mm": side}).boundary())


def _densify(loop: np.ndarray, step: float) -> np.ndarray:
    """沿环的每条边密集取样：查"边"有没有越界，光看顶点是不够的。"""

    out = []
    for start, end in zip(loop, np.roll(loop, -1, axis=0)):
        count = max(2, int(np.ceil(np.linalg.norm(end - start) / step)))
        out.extend(start + t * (end - start) for t in np.linspace(0.0, 1.0, count))
    return np.array(out)


def _self_intersections(polygon: np.ndarray) -> int:
    """数一数非相邻边之间的交点个数。"""

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
    """向内偏置：数值上对得上解析值，而且整条边都不越界。"""

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
        # 向外偏置要在凸角补圆弧，是另一套几何：负值直接报错，免得悄悄给出错的环。
        with self.assertRaises(ValueError):
            offset_loops(_square(80.0), -10.0)

    def test_tiny_loops_can_be_filtered_by_area(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        self.assertEqual(len(offset_loops(polygon, 27.0)), 2)
        self.assertEqual(offset_loops(polygon, 27.0, min_area_mm2=100.0), [])

    def test_every_loop_keeps_the_distance_along_its_edges(self) -> None:
        """关键不变式：不是只有顶点不越界，整条边都不能越界。

        早期版本把"被细颈吃掉后剩下的顶点"按原顺序接起来，顶点都合格、边却横穿细颈，
        实测边中点离轮廓只有细颈一半那么远。
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
        # 细颈宽 20：偏置 3、9 时细颈还在（一条环），15 起被吃掉，两块方头各成一条环。
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
        # 方头 60 − 2×15 = 30 → 30×30 = 900，再加上凹角圆弧鼓包 ≈ 24
        self.assertAlmostEqual(
            sum(signed_area(loop) for loop in loops), 2 * 924.0, delta=2.0
        )

    def test_a_u_shape_offset_keeps_the_reflex_arc_bulges(self) -> None:
        polygon = ensure_ccw(build_region("u_shape", {}).boundary())
        loop = offset_polygon(polygon, 3.0)
        # 直角版本是 94×74 − 56×55 = 3876，两个凹角各多一块圆弧鼓包 3² − π·3²/4
        bulge = 2.0 * (9.0 - pi * 9.0 / 4.0)
        self.assertAlmostEqual(signed_area(loop), 3876.0 + bulge, delta=0.5)

    def test_offset_polygon_returns_the_largest_loop(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        largest = offset_polygon(polygon, 15.0)
        self.assertAlmostEqual(signed_area(largest), signed_area(offset_loops(polygon, 15.0)[0]))


def _triangle(width: float, height: float) -> np.ndarray:
    """底边宽 width、高 height 的等腰三角形（顶点朝上）。"""

    half = width / 2.0
    return ensure_ccw(np.array([[-half, 0.0], [half, 0.0], [0.0, height]]))


def _slab_with_fin() -> np.ndarray:
    """一块 100 × 60 的板，上边中间长了一个 4 × 10 的小尖鳍（顶角约 22.6°）。

    尖鳍两侧边长约 10.2，而斜接点需要 3·tan(78.7°) ≈ 15 的延长——斜接点落在平移线段之外。
    按几何结论，这时鳍附近宽 2L·sin(11.3°) ≈ 4 < 6 = 2d，比刀具还窄，整只鳍早被侵蚀掉了。
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
    """尖角处的偏置：斜接点可以离顶点很远，但这从来不是"够不够长"的问题。"""

    def test_a_sharp_apex_still_closes_the_ring(self) -> None:
        # 顶角 18.9° 的斜接点离顶点约 6×偏置量，照样要闭合成环
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
        """三角形的侵蚀还是相似三角形：面积 = 原面积 × ((r − d) / r)²，r 是内切半径。"""

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
        # 顶角 1.9° 的细长三角：内切半径小于偏置量，本来就什么都不剩
        self.assertEqual(offset_loops(_triangle(4.0, 120.0), 3.0), [])

    def test_a_feature_narrower_than_the_tool_just_disappears(self) -> None:
        """比刀具还窄的尖鳍不会留下碎片，也不会把整条环弄断——它整只被侵蚀掉了。"""

        polygon = _slab_with_fin()
        loops = offset_loops(polygon, 3.0)
        self.assertEqual(len(loops), 1)
        loop = loops[0]
        # 鳍完全消失：偏置边界最高只到板的顶面内缩处
        self.assertLess(float(loop[:, 1].max()), 28.0)
        self.assertGreater(float(loop[:, 1].min()), -28.0)
        # 留下来的环离轮廓的距离仍然不小于偏置量
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


if __name__ == "__main__":
    unittest.main()
