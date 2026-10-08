"""区域形状：方形与圆形。"""

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
    def test_only_square_and_circle_are_registered(self) -> None:
        self.assertEqual(sorted(REGION_SHAPES.ids()), ["circle", "rounded_rectangle", "square"])

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
            [item["key"] for item in entries["rounded_rectangle"]["parameters"]],
            ["width_mm", "height_mm", "corner_radius_mm"],
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




class RoundedRectangleRegionTests(unittest.TestCase):
    """圆角矩形：几何、面积与校验。"""

    def test_boundary_is_ccw_and_in_bounds(self) -> None:
        region = build_region(
            "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 10.0}
        )
        polygon = ensure_ccw(region.boundary())
        self.assertGreater(polygon.shape[0], 60)  # 4 弧 × 16 段 + 直线
        self.assertGreater(signed_area(polygon), 0.0)
        self.assertEqual(polygon_bounds(polygon), [[-40.0, 40.0], [-30.0, 30.0]])

    def test_area_matches_analytic_value(self) -> None:
        # W×H - 4r² + πr²：矩形减去四角再加圆角圆弧
        region = build_region(
            "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 10.0}
        )
        expected = 80.0 * 60.0 - 4.0 * 100.0 + pi * 100.0
        self.assertAlmostEqual(polygon_area(ensure_ccw(region.boundary())), expected, delta=5.0)

    def test_zero_corner_degrades_to_square(self) -> None:
        region = build_region(
            "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 0.0}
        )
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape, (4, 2))
        self.assertEqual(polygon_bounds(polygon), [[-40.0, 40.0], [-30.0, 30.0]])

    def test_corner_cannot_exceed_half_the_short_side(self) -> None:
        with self.assertRaises(ParameterError):
            build_region(
                "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 31.0}
            )
        with self.assertRaises(ParameterError):
            build_region(
                "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": -1.0}
            )

    def test_raster_plans_on_rounded_rectangle(self) -> None:
        from toolpath_lab.planning import run_plan
        from toolpath_lab.core.tool import Tool, ToolKind

        outcome = run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
            region=build_region(
                "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 10.0}
            ),
            parameters={"mode": "zigzag", "stepover_mm": 6.0},
        )
        self.assertGreater(outcome.toolpath.pass_count, 3)

    def test_contour_plans_on_rounded_rectangle(self) -> None:
        from toolpath_lab.planning import run_plan
        from toolpath_lab.core.tool import Tool, ToolKind

        outcome = run_plan(
            planner_id="contour",
            tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
            region=build_region(
                "rounded_rectangle", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 10.0}
            ),
            parameters={"stepover_mm": 6.0, "sample_step_mm": 1.0},
        )
        cut = [m for m in outcome.toolpath.moves if m.kind.value == "cut"]
        self.assertGreaterEqual(len(cut), 2)

if __name__ == "__main__":

    unittest.main()
