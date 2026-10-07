"""区域形状：方形、矩形、圆形、椭圆，以及所有形状共同遵守的边界契约。"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.errors import ParameterError, RegistryError
from toolpath_lab.core.region import (
    CURVE_SEGMENTS,
    REGION_SHAPES,
    DumbbellRegion,
    EllipseRegion,
    RectangleRegion,
    UShapeRegion,
    build_region,
    polygon_area,
    polygon_bounds,
    region_catalog,
)
from toolpath_lab.planning.geometry2d import ensure_ccw, scanline_intervals, signed_area


class RegionCatalogTests(unittest.TestCase):
    def test_registered_shapes(self) -> None:
        self.assertEqual(
            sorted(REGION_SHAPES.ids()),
            ["circle", "dumbbell", "ellipse", "rectangle", "square", "u_shape"],
        )

    def test_catalog_publishes_labels_and_parameters(self) -> None:
        entries = {entry["id"]: entry for entry in region_catalog()}
        self.assertEqual(entries["square"]["label"], "方形")
        self.assertEqual(
            [item["key"] for item in entries["square"]["parameters"]], ["side_mm"]
        )
        self.assertEqual(
            [item["key"] for item in entries["circle"]["parameters"]], ["diameter_mm"]
        )
        self.assertEqual(entries["rectangle"]["label"], "矩形")
        self.assertEqual(
            [item["key"] for item in entries["rectangle"]["parameters"]],
            ["width_mm", "height_mm"],
        )
        self.assertEqual(entries["ellipse"]["label"], "椭圆")
        self.assertEqual(
            [item["key"] for item in entries["ellipse"]["parameters"]],
            ["semi_major_mm", "semi_minor_mm"],
        )
        self.assertEqual(entries["u_shape"]["label"], "U 形")
        self.assertEqual(
            [item["key"] for item in entries["u_shape"]["parameters"]],
            ["width_mm", "height_mm", "wall_mm"],
        )
        self.assertEqual(entries["dumbbell"]["label"], "哑铃形")
        self.assertEqual(
            [item["key"] for item in entries["dumbbell"]["parameters"]],
            ["width_mm", "pad_mm", "neck_mm"],
        )

    def test_unknown_shape_raises(self) -> None:
        with self.assertRaises(RegistryError):
            build_region("hexagon", {})

    def test_every_shape_builds_from_its_catalog_defaults(self) -> None:
        for entry in region_catalog():
            with self.subTest(shape=entry["id"]):
                region = build_region(entry["id"], {})
                self.assertEqual(region.id, entry["id"])
                self.assertGreater(region.describe()["area_mm2"], 0.0)


class ShapeContractTests(unittest.TestCase):
    """新增形状时必须满足的契约：逆时针、不重复首点、有限、至少三个点、以原点为中心。"""

    def test_every_boundary_is_a_ccw_polygon_without_repeats(self) -> None:
        for shape_id in REGION_SHAPES.ids():
            boundary = build_region(shape_id, {}).boundary()
            with self.subTest(shape=shape_id):
                self.assertEqual(boundary.ndim, 2)
                self.assertGreaterEqual(boundary.shape[0], 3)
                self.assertTrue(bool(np.all(np.isfinite(boundary))))
                self.assertGreater(signed_area(boundary), 0.0)
                self.assertGreater(
                    float(np.linalg.norm(boundary[0] - boundary[-1])), 1e-9
                )

    def test_every_shape_is_centred_on_the_origin(self) -> None:
        # 契约只要求包围盒对称：凹形状（U 形）的材料重心并不在原点，
        # 而三维取景与工件厚度都是按包围盒算的。
        for shape_id in REGION_SHAPES.ids():
            polygon = ensure_ccw(build_region(shape_id, {}).boundary())
            [(x_min, x_max), (y_min, y_max)] = polygon_bounds(polygon)
            with self.subTest(shape=shape_id):
                self.assertAlmostEqual(x_min, -x_max, places=6)
                self.assertAlmostEqual(y_min, -y_max, places=6)


class SquareRegionTests(unittest.TestCase):
    def test_boundary_is_a_counter_clockwise_square(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape, (4, 2))
        self.assertAlmostEqual(signed_area(polygon), 6400.0)
        self.assertEqual(polygon_bounds(polygon), [[-40.0, 40.0], [-40.0, 40.0]])

    def test_describe_reports_area(self) -> None:
        described = build_region("square", {"side_mm": 50.0}).describe()
        self.assertAlmostEqual(described["area_mm2"], 2500.0)
        self.assertEqual(described["parameters"]["side_mm"], 50.0)

    def test_too_small_side_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_region("square", {"side_mm": 1.0})


class RectangleRegionTests(unittest.TestCase):
    def test_boundary_is_a_counter_clockwise_rectangle(self) -> None:
        region = build_region("rectangle", {"width_mm": 100.0, "height_mm": 60.0})
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape, (4, 2))
        self.assertAlmostEqual(signed_area(polygon), 6000.0)
        self.assertEqual(polygon_bounds(polygon), [[-50.0, 50.0], [-30.0, 30.0]])

    def test_defaults_are_a_hundred_by_sixty_rectangle(self) -> None:
        described = build_region("rectangle", {}).describe()
        self.assertAlmostEqual(described["area_mm2"], 6000.0)
        self.assertEqual(described["parameters"], {"width_mm": 100.0, "height_mm": 60.0})

    def test_too_small_sides_are_rejected(self) -> None:
        for parameters in ({"width_mm": 1.0}, {"height_mm": 1.0}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    build_region("rectangle", parameters)

    def test_the_class_itself_keeps_the_invariant(self) -> None:
        with self.assertRaises(ParameterError):
            RectangleRegion(width_mm=0.0, height_mm=60.0)


class CircleRegionTests(unittest.TestCase):
    def test_boundary_area_matches_the_analytic_value(self) -> None:
        region = build_region("circle", {"diameter_mm": 60.0})
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape[0], CURVE_SEGMENTS)
        self.assertAlmostEqual(polygon_area(polygon), pi * 900.0, delta=1.0)

    def test_boundary_stays_inside_the_nominal_radius(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 60.0}).boundary())
        radii = np.linalg.norm(polygon, axis=1)
        self.assertAlmostEqual(float(radii.min()), 30.0, places=6)
        self.assertAlmostEqual(float(radii.max()), 30.0, places=6)


class EllipseRegionTests(unittest.TestCase):
    def test_boundary_samples_the_ellipse(self) -> None:
        major, minor = 60.0, 40.0
        polygon = ensure_ccw(
            build_region(
                "ellipse", {"semi_major_mm": major, "semi_minor_mm": minor}
            ).boundary()
        )
        self.assertEqual(polygon.shape[0], CURVE_SEGMENTS)
        # 每个点都落在椭圆上：(x/a)² + (y/b)² == 1
        self.assertTrue(
            bool(
                np.allclose(
                    (polygon[:, 0] / major) ** 2 + (polygon[:, 1] / minor) ** 2,
                    1.0,
                    atol=1e-9,
                )
            )
        )
        self.assertEqual(polygon_bounds(polygon), [[-major, major], [-minor, minor]])

    def test_area_matches_the_analytic_value(self) -> None:
        polygon = build_region("ellipse", {}).boundary()
        self.assertAlmostEqual(polygon_area(polygon), pi * 60.0 * 40.0, delta=5.0)

    def test_swapping_the_axes_just_rotates_the_shape(self) -> None:
        wide = build_region("ellipse", {"semi_major_mm": 60.0, "semi_minor_mm": 40.0}).boundary()
        tall = build_region("ellipse", {"semi_major_mm": 40.0, "semi_minor_mm": 60.0}).boundary()
        self.assertAlmostEqual(polygon_area(wide), polygon_area(tall), places=6)
        self.assertEqual(polygon_bounds(wide)[0], polygon_bounds(tall)[1])

    def test_invalid_axes_are_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_region("ellipse", {"semi_major_mm": 0.5})
        with self.assertRaises(ParameterError):
            EllipseRegion(semi_major_mm=60.0, semi_minor_mm=0.0)


class UShapeRegionTests(unittest.TestCase):
    """凹多边形：一条扫描线会切出两段，刀路与三维显示都不需要特判。"""

    def test_boundary_is_an_eight_vertex_concave_polygon(self) -> None:
        region = build_region(
            "u_shape", {"width_mm": 100.0, "height_mm": 80.0, "wall_mm": 25.0}
        )
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape, (8, 2))
        # 100 × 80 减去 50 × 55 的槽
        self.assertAlmostEqual(signed_area(polygon), 8000.0 - 2750.0, places=6)
        self.assertEqual(polygon_bounds(polygon), [[-50.0, 50.0], [-40.0, 40.0]])

    def test_a_scanline_across_the_arms_yields_two_intervals(self) -> None:
        polygon = ensure_ccw(build_region("u_shape", {}).boundary())
        arms = scanline_intervals(polygon, 0.0)
        self.assertEqual(len(arms), 2)
        self.assertAlmostEqual(arms[0].start, -50.0, places=6)
        self.assertAlmostEqual(arms[0].end, -25.0, places=6)
        self.assertAlmostEqual(arms[1].start, 25.0, places=6)
        self.assertAlmostEqual(arms[1].end, 50.0, places=6)

    def test_a_scanline_below_the_channel_yields_one_interval(self) -> None:
        polygon = ensure_ccw(build_region("u_shape", {}).boundary())
        base = scanline_intervals(polygon, -30.0)
        self.assertEqual(len(base), 1)
        self.assertAlmostEqual(base[0].start, -50.0, places=6)
        self.assertAlmostEqual(base[0].end, 50.0, places=6)

    def test_a_wall_that_would_close_the_channel_is_rejected(self) -> None:
        # 壁厚 >= 外宽的一半时两条臂会贴在一起，形状就不再是 U 形了。
        with self.assertRaises(ParameterError):
            build_region("u_shape", {"width_mm": 100.0, "wall_mm": 50.0})
        with self.assertRaises(ParameterError):
            build_region("u_shape", {"wall_mm": 80.0, "height_mm": 80.0})

    def test_the_class_itself_keeps_the_invariant(self) -> None:
        with self.assertRaises(ParameterError):
            UShapeRegion(width_mm=100.0, height_mm=80.0, wall_mm=60.0)


class DumbbellRegionTests(unittest.TestCase):
    """哑铃形：细颈被偏置吃掉后，环切一层会分裂成两条环。"""

    def test_boundary_is_a_twelve_vertex_concave_polygon(self) -> None:
        region = build_region("dumbbell", {})
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape, (12, 2))
        # 160 × 60 的两个方头 + 40 × 20 的细颈
        self.assertAlmostEqual(signed_area(polygon), 2 * 60 * 60 + 40 * 20, places=6)
        self.assertEqual(polygon_bounds(polygon), [[-80.0, 80.0], [-30.0, 30.0]])

    def test_a_scanline_across_the_neck_yields_one_interval(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        self.assertEqual(len(scanline_intervals(polygon, 0.0)), 1)
        self.assertAlmostEqual(scanline_intervals(polygon, 0.0)[0].start, -80.0, places=6)

    def test_a_scanline_across_a_pad_yields_one_interval(self) -> None:
        polygon = ensure_ccw(build_region("dumbbell", {}).boundary())
        interval = scanline_intervals(polygon, 20.0)
        self.assertEqual(len(interval), 2)  # 左右两个方头各一段
        self.assertLess(interval[0].end, interval[1].start)

    def test_a_neck_that_is_not_thinner_than_the_pad_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_region("dumbbell", {"neck_mm": 60.0})
        with self.assertRaises(ParameterError):
            build_region("dumbbell", {"width_mm": 100.0})  # 两个 60 的方头放不下

    def test_the_class_itself_keeps_the_invariant(self) -> None:
        with self.assertRaises(ParameterError):
            DumbbellRegion(width_mm=160.0, pad_mm=60.0, neck_mm=0.0)


if __name__ == "__main__":
    unittest.main()
