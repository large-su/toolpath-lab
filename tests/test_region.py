"""区域形状：方形、圆形与椭圆。"""

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
    def test_all_shapes_are_registered(self) -> None:
        self.assertEqual(sorted(REGION_SHAPES.ids()), ["circle", "ellipse", "square"])

    def test_catalog_publishes_labels_and_parameters(self) -> None:
        entries = {entry["id"]: entry for entry in region_catalog()}
        self.assertEqual(entries["square"]["label"], "方形")
        self.assertEqual(entries["circle"]["label"], "圆形")
        self.assertEqual(entries["ellipse"]["label"], "椭圆")
        self.assertEqual(
            [item["key"] for item in entries["square"]["parameters"]], ["side_mm"]
        )
        self.assertEqual(
            [item["key"] for item in entries["circle"]["parameters"]], ["diameter_mm"]
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
        self.assertEqual(polygon.shape, (CIRCLE_SEGMENTS, 2))
        self.assertAlmostEqual(polygon_area(polygon), pi * 60.0 * 40.0, delta=5.0)

    def test_boundary_bounds_follow_the_two_semi_axes(self) -> None:
        polygon = ensure_ccw(
            build_region("ellipse", {"semi_major_mm": 50.0, "semi_minor_mm": 25.0}).boundary()
        )
        self.assertEqual(polygon_bounds(polygon), [[-50.0, 50.0], [-25.0, 25.0]])

    def test_boundary_is_counter_clockwise(self) -> None:
        polygon = build_region("ellipse", {}).boundary()
        self.assertGreater(signed_area(polygon), 0.0)

    def test_a_round_ellipse_is_a_circle(self) -> None:
        ellipse = build_region("ellipse", {"semi_major_mm": 30.0, "semi_minor_mm": 30.0})
        circle = build_region("circle", {"diameter_mm": 60.0})
        self.assertAlmostEqual(polygon_area(ellipse.boundary()),
                               polygon_area(circle.boundary()), places=6)

    def test_describe_reports_the_two_axes(self) -> None:
        described = build_region(
            "ellipse", {"semi_major_mm": 70.0, "semi_minor_mm": 30.0}
        ).describe()
        self.assertEqual(
            described["parameters"], {"semi_major_mm": 70.0, "semi_minor_mm": 30.0}
        )
        self.assertAlmostEqual(described["area_mm2"], pi * 70.0 * 30.0, delta=5.0)

    def test_too_small_axis_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_region("ellipse", {"semi_major_mm": 1.0})
        with self.assertRaises(ParameterError):
            build_region("ellipse", {"semi_minor_mm": 1.0})


if __name__ == "__main__":
    unittest.main()
