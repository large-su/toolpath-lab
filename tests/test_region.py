"""区域形状：方形、圆形与圆角矩形。"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.errors import ParameterError, RegistryError
from toolpath_lab.core.region import (
    CIRCLE_SEGMENTS,
    REGION_SHAPES,
    build_region,
    polygon_area,
    polygon_bounds,
    region_catalog,
)
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area


class RegionCatalogTests(unittest.TestCase):
    def test_the_three_shapes_are_registered(self) -> None:
        self.assertEqual(
            sorted(REGION_SHAPES.ids()),
            ["circle", "ellipse", "rounded_rect", "square"],
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
        self.assertEqual(
            [item["key"] for item in entries["rounded_rect"]["parameters"]],
            ["width_mm", "height_mm", "corner_radius_mm"],
        )
        self.assertEqual(
            [item["key"] for item in entries["ellipse"]["parameters"]],
            ["semi_major_mm", "semi_minor_mm"],
        )

    def test_unknown_shape_raises(self) -> None:
        with self.assertRaises(RegistryError):
            build_region("hexagon", {})


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


class CircleRegionTests(unittest.TestCase):
    def test_boundary_area_matches_the_analytic_value(self) -> None:
        region = build_region("circle", {"diameter_mm": 60.0})
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape[0], CIRCLE_SEGMENTS)
        self.assertAlmostEqual(polygon_area(polygon), pi * 900.0, delta=1.0)

    def test_boundary_stays_inside_the_nominal_radius(self) -> None:
        polygon = ensure_ccw(build_region("circle", {"diameter_mm": 60.0}).boundary())
        radii = np.linalg.norm(polygon, axis=1)
        self.assertAlmostEqual(float(radii.min()), 30.0, places=6)
        self.assertAlmostEqual(float(radii.max()), 30.0, places=6)


class EllipseRegionTests(unittest.TestCase):
    def test_boundary_area_matches_the_analytic_value(self) -> None:
        region = build_region("ellipse", {"semi_major_mm": 60.0, "semi_minor_mm": 40.0})
        polygon = ensure_ccw(region.boundary())
        self.assertGreater(signed_area(polygon), 0.0)
        self.assertAlmostEqual(polygon_area(polygon), pi * 60.0 * 40.0, delta=1.0)

    def test_every_point_lies_on_the_ellipse(self) -> None:
        # 曾经的 bug：按极角反解参数会让上下半平面的点重合，面积完全失真。
        a, b = 60.0, 40.0
        polygon = ensure_ccw(build_region(
            "ellipse", {"semi_major_mm": a, "semi_minor_mm": b}
        ).boundary())
        residual = (polygon[:, 0] / a) ** 2 + (polygon[:, 1] / b) ** 2
        np.testing.assert_allclose(residual, 1.0, atol=1e-9)

    def test_no_duplicate_points(self) -> None:
        for major, minor in ((60.0, 40.0), (40.0, 40.0), (200.0, 20.0)):
            with self.subTest(major=major, minor=minor):
                polygon = ensure_ccw(build_region(
                    "ellipse", {"semi_major_mm": major, "semi_minor_mm": minor}
                ).boundary())
                gaps = np.linalg.norm(
                    np.diff(np.vstack([polygon, polygon[:1]]), axis=0), axis=1
                )
                self.assertGreater(float(gaps.min()), 1e-6)

    def test_equal_axes_match_the_circle_area(self) -> None:
        ellipse = polygon_area(build_region(
            "ellipse", {"semi_major_mm": 30.0, "semi_minor_mm": 30.0}
        ).boundary())
        circle = polygon_area(build_region("circle", {"diameter_mm": 60.0}).boundary())
        self.assertAlmostEqual(ellipse, circle, delta=2.0)

    def test_bounds_follow_the_semi_axes(self) -> None:
        # 离散多边形不会正好落在 ±b 上（除非 4 | steps），所以短轴方向略小于名义值。
        described = build_region(
            "ellipse", {"semi_major_mm": 60.0, "semi_minor_mm": 40.0}
        ).describe()
        [[x_min, x_max], [y_min, y_max]] = described["bounds_mm"]
        self.assertAlmostEqual(x_min, -60.0, places=6)
        self.assertAlmostEqual(x_max, 60.0, places=6)
        self.assertAlmostEqual(y_min, -40.0, delta=0.05)
        self.assertAlmostEqual(y_max, 40.0, delta=0.05)

    def test_non_positive_semi_axes_are_rejected(self) -> None:
        for parameters in ({"semi_major_mm": 0.0}, {"semi_minor_mm": -5.0}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    build_region("ellipse", parameters)

    def test_elongated_ellipse_keeps_segments_short(self) -> None:
        # 200 x 20 的扁椭圆也要保证相邻点足够密，否则边界会明显"棱角化"。
        polygon = ensure_ccw(build_region(
            "ellipse", {"semi_major_mm": 200.0, "semi_minor_mm": 20.0}
        ).boundary())
        gaps = np.linalg.norm(
            np.diff(np.vstack([polygon, polygon[:1]]), axis=0), axis=1
        )
        self.assertLess(float(gaps.max()), 5.0)


class RoundedRectRegionTests(unittest.TestCase):
    def test_boundary_is_counter_clockwise_and_within_the_outer_box(self) -> None:
        region = build_region(
            "rounded_rect", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 12.0}
        )
        polygon = ensure_ccw(region.boundary())
        self.assertGreaterEqual(polygon.shape[0], 4)
        self.assertGreater(signed_area(polygon), 0.0)
        self.assertEqual(
            polygon_bounds(polygon), [[-40.0, 40.0], [-30.0, 30.0]]
        )

    def test_area_matches_the_analytic_value(self) -> None:
        # 面积 = W*H - (4 - pi) * R^2
        region = build_region(
            "rounded_rect", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 10.0}
        )
        expected = 80.0 * 60.0 - (4.0 - pi) * 100.0
        self.assertAlmostEqual(polygon_area(region.boundary()), expected, delta=1.0)

    def test_full_radius_on_the_short_side_is_a_stadium(self) -> None:
        # R = H/2 时两端是半圆，边界最宽处仍然等于 W。
        region = build_region(
            "rounded_rect", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 30.0}
        )
        polygon = ensure_ccw(region.boundary())
        self.assertAlmostEqual(float(np.abs(polygon[:, 0]).max()), 40.0, places=6)
        self.assertAlmostEqual(float(np.abs(polygon[:, 1]).max()), 30.0, places=6)

    def test_radius_equal_to_half_the_side_degenerates_into_a_circle(self) -> None:
        # W == H 且 R == W/2 时应当等价于同直径的圆形区域。
        square_region = build_region(
            "rounded_rect", {"width_mm": 60.0, "height_mm": 60.0, "corner_radius_mm": 30.0}
        )
        polygon = ensure_ccw(square_region.boundary())
        radii = np.linalg.norm(polygon, axis=1)
        self.assertAlmostEqual(float(radii.min()), 30.0, places=4)
        self.assertAlmostEqual(float(radii.max()), 30.0, places=4)

    def test_describe_reports_bounds_and_parameters(self) -> None:
        described = build_region(
            "rounded_rect", {"width_mm": 70.0, "height_mm": 50.0, "corner_radius_mm": 8.0}
        ).describe()
        self.assertEqual(described["id"], "rounded_rect")
        self.assertEqual(
            described["bounds_mm"], [[-35.0, 35.0], [-25.0, 25.0]]
        )
        self.assertEqual(described["parameters"]["corner_radius_mm"], 8.0)

    def test_radius_beyond_half_the_short_side_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_region(
                "rounded_rect",
                {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 31.0},
            )

    def test_non_positive_dimensions_are_rejected(self) -> None:
        for parameters in (
            {"width_mm": 0.0},
            {"height_mm": -10.0},
            {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 0.0},
        ):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    build_region("rounded_rect", parameters)


if __name__ == "__main__":
    unittest.main()
