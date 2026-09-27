"""2.5D 轮廓层（pyclipper）测试。

pyclipper 是新增的第三方依赖；没装时整组跳过，这样原来"零第三方依赖"的环境
跑全量测试也不会红。
"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

try:
    import pyclipper  # noqa: F401
    HAS_CLIPPER = True
except ImportError:  # pragma: no cover - 依赖缺失时跳过
    HAS_CLIPPER = False

from toolpath_lab.contour2d import ContourClipper, Polygon2D, Region2D, ring_passes
from toolpath_lab.contour2d.polygon import Polygon2D as Polygon2DRef


@unittest.skipUnless(HAS_CLIPPER, "需要 pyclipper")
class PolygonTests(unittest.TestCase):
    def test_area_sign_follows_winding(self) -> None:
        ccw = Polygon2D.rectangle(0, 0, 10, 20)
        self.assertAlmostEqual(ccw.area, 200.0, places=9)
        self.assertTrue(ccw.is_ccw)
        self.assertAlmostEqual(ccw.reversed().area, -200.0, places=9)

    def test_closing_point_is_dropped(self) -> None:
        polygon = Polygon2D(np.array([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], dtype=float))
        self.assertEqual(len(polygon.points), 4)

    def test_region_normalises_ring_directions(self) -> None:
        # 故意把孔给成 CCW，Region2D 要把它掰成 CW
        hole = Polygon2D.rectangle(2, 2, 4, 4)
        region = Region2D(Polygon2D.rectangle(0, 0, 10, 10), (hole,))
        self.assertTrue(region.outer.is_ccw)
        self.assertFalse(region.holes[0].is_ccw)
        self.assertAlmostEqual(region.area, 100.0 - 4.0, places=9)

    def test_contains(self) -> None:
        square = Polygon2D.rectangle(0, 0, 10, 10)
        self.assertTrue(square.contains((5, 5)))
        self.assertFalse(square.contains((15, 5)))

    def test_circle_area_approaches_pi_r_squared(self) -> None:
        circle = Polygon2D.circle(0, 0, 10, segments=512)
        self.assertAlmostEqual(abs(circle.area), pi * 100.0, delta=1.0)


@unittest.skipUnless(HAS_CLIPPER, "需要 pyclipper")
class ClipperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clipper = ContourClipper()
        self.region = Region2D.rectangle(
            -50, -40, 50, 40, holes=[Polygon2D.rectangle(-15, -10, 15, 10)]
        )

    def test_inward_offset_moves_outer_and_hole_oppositely(self) -> None:
        """刀具半径补偿的核心：外环内缩、孔环外扩，方向必须相反。"""

        core = self.clipper.offset(self.region, -5.0)
        self.assertEqual(len(core), 1)
        self.assertEqual([round(v, 6) for v in core[0].outer.bounds()], [-45, -35, 45, 35])
        self.assertEqual([round(v, 6) for v in core[0].holes[0].bounds()], [-20, -15, 20, 15])

    def test_outward_offset_is_mirror_image(self) -> None:
        grown = self.clipper.offset(self.region, 3.0)
        self.assertEqual([round(v, 6) for v in grown[0].outer.bounds()], [-53, -43, 53, 43])
        self.assertEqual([round(v, 6) for v in grown[0].holes[0].bounds()], [-12, -7, 12, 7])

    def test_offset_can_vanish(self) -> None:
        self.assertEqual(self.clipper.offset(self.region, -100.0), ())

    def test_boolean_difference_removes_exact_area(self) -> None:
        circle = Region2D(Polygon2D.circle(35, 25, 8, segments=512))
        result = self.clipper.boolean(self.region, circle, op="difference")
        self.assertAlmostEqual(self.clipper.total_area(result),
                               self.region.area - pi * 64, delta=0.5)

    def test_boolean_keeps_holes(self) -> None:
        """挖掉一个材料内的圆之后，原来的孔和这个圆都应当作为孔保留。"""

        circle = Region2D(Polygon2D.circle(35, 25, 8, segments=512))
        result = self.clipper.boolean(self.region, circle, op="difference")
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].holes), 2)
        self.assertEqual([round(v, 6) for v in result[0].outer.bounds()], [-50, -40, 50, 40])

    def test_nested_island_becomes_its_own_region(self) -> None:
        """孔里再放一个岛：应当变成两个区域，而不是把岛当孔丢掉。"""

        isolated = Region2D(Polygon2D.circle(0, 0, 6, segments=256))
        nested = self.clipper.union_all([self.region, isolated])
        self.assertEqual(len(nested), 2)
        areas = sorted(abs(item.outer.area) for item in nested)
        self.assertAlmostEqual(areas[0], pi * 36, delta=1.0)

    def test_self_intersecting_input_is_cleaned(self) -> None:
        """自交的"8 字形"环清理后不应炸出碎环。"""

        bowtie = Polygon2D(np.array([[0, 0], [10, 10], [10, 0], [0, 10]], dtype=float))
        cleaned = self.clipper.cleanup(Region2D(bowtie))
        self.assertLessEqual(len(cleaned), 2)


@unittest.skipUnless(HAS_CLIPPER, "需要 pyclipper")
class PocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clipper = ContourClipper()
        # 100 x 80 的框，中间 30 x 20 的岛
        self.region = Region2D.rectangle(
            -50, -40, 50, 40, holes=[Polygon2D.rectangle(-15, -10, 15, 10)]
        )

    def test_rings_shrink_by_exactly_one_stepover(self) -> None:
        result = ring_passes(self.region, tool_radius=5.0, stepover_mm=4.0,
                             clipper=self.clipper)
        outside = [p for p in result.passes if p.level < 3 and abs(p.polygon.area) > 1000
                   and p.polygon.is_ccw]
        by_level = {p.level: abs(p.polygon.area) for p in outside}
        self.assertAlmostEqual(by_level[0], 90 * 70, places=6)
        self.assertAlmostEqual(by_level[1], 82 * 62, places=6)
        self.assertAlmostEqual(by_level[2], 74 * 54, places=6)

    def test_tool_radius_compensation_is_built_in(self) -> None:
        """刀心可达区 = 区域内缩一个刀半径。

        用 ``miter`` 拐角才能对面积做精确断言；``round`` 会在孔的四个角上补圆弧，
        面积比理论值大 (4-π)r²/4 量级 —— 那是**正确**的圆角补偿，不是误差。
        """

        result = ring_passes(self.region, tool_radius=5.0, stepover_mm=4.0,
                             clipper=self.clipper)
        # ring_passes 内部用的就是默认的 round 偏置，两者必须完全一致
        expected_core = self.clipper.total_area(self.clipper.offset(self.region, -5.0))
        self.assertAlmostEqual(self.clipper.total_area(result.core), expected_core, places=6)
        mitred = self.clipper.offset(self.region, -5.0, join="miter")
        self.assertAlmostEqual(self.clipper.total_area(mitred), 90 * 70 - 40 * 30, places=6)
        # 圆角补偿的量级：(4-π)/4 * r² * 4 个角 ≈ 0.215 * 25 * 4 = 21.5mm²
        rounded = self.clipper.offset(self.region, -5.0, join="round")
        self.assertAlmostEqual(self.clipper.total_area(rounded)
                               - self.clipper.total_area(mitred), 21.46, delta=0.5)

    def test_too_large_tool_yields_no_pass(self) -> None:
        result = ring_passes(self.region, tool_radius=200.0, stepover_mm=4.0,
                             clipper=self.clipper)
        self.assertEqual(result.passes, ())

    def test_stepover_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            ring_passes(self.region, tool_radius=5.0, stepover_mm=0.0)

    def test_inside_out_order_reverses(self) -> None:
        outside_in = ring_passes(self.region, 5.0, 4.0, clipper=self.clipper)
        inside_out = ring_passes(self.region, 5.0, 4.0, order="inside_out",
                                 clipper=self.clipper)
        self.assertEqual([p.inset_mm for p in outside_in.passes],
                         list(reversed([p.inset_mm for p in inside_out.passes])))

    def test_links_are_shorter_than_naive(self) -> None:
        """最近点连接应当比"各环起点直连"短。"""

        nearest = ring_passes(self.region, 5.0, 4.0, clipper=self.clipper)
        self.assertGreater(nearest.cut_length_mm, 0.0)
        self.assertLess(nearest.link_length_mm, nearest.cut_length_mm)

    def test_leftover_is_reported(self) -> None:
        result = ring_passes(self.region, 5.0, 4.0, clipper=self.clipper)
        # 框形区域的中央始终切不到，必须如实报出来
        self.assertGreater(result.leftover_area_mm2, 0.0)


if __name__ == "__main__":
    unittest.main()
