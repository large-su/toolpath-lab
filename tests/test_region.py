"""区域形状：方形、圆形与模型轮廓。"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.core.errors import ParameterError, RegistryError
from toolpath_lab.core.mesh import Mesh, ModelLibrary
from toolpath_lab.core.region import (
    CIRCLE_SEGMENTS,
    REGION_SHAPES,
    build_region,
    offset_convex_polygon,
    polygon_area,
    polygon_bounds,
    region_catalog,
)
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area


class RegionCatalogTests(unittest.TestCase):
    def test_registered_shapes(self) -> None:
        self.assertEqual(sorted(REGION_SHAPES.ids()), ["circle", "model", "square"])

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
            [item["key"] for item in entries["model"]["parameters"]], ["outline", "margin_mm"]
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


def _plate_mesh(side: float = 60.0, top: float = 10.0) -> Mesh:
    """一块方板：底面 Z=0、顶面 Z=top（用来测模型轮廓与模型加工面）。"""

    half = side / 2.0
    corners = [
        (-half, -half), (half, -half), (half, half), (-half, half),
    ]
    triangles = []
    for index in range(4):
        x0, y0 = corners[index]
        x1, y1 = corners[(index + 1) % 4]
        triangles.append([(x0, y0, top), (x1, y1, top), (0.0, 0.0, top)])
        triangles.append([(x0, y0, 0.0), (0.0, 0.0, 0.0), (x1, y1, 0.0)])
    return Mesh(np.array(triangles, dtype=np.float64))


class OffsetPolygonTests(unittest.TestCase):
    def test_outward_offset_expands_a_square(self) -> None:
        polygon = np.array([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)])
        offset = offset_convex_polygon(polygon, 5.0)
        self.assertAlmostEqual(polygon_area(offset), 30.0 * 30.0, places=6)

    def test_inward_offset_shrinks_a_square(self) -> None:
        polygon = np.array([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)])
        offset = offset_convex_polygon(polygon, -4.0)
        self.assertAlmostEqual(polygon_area(offset), 12.0 * 12.0, places=6)

    def test_zero_offset_keeps_the_polygon(self) -> None:
        polygon = np.array([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
        self.assertTrue(np.allclose(offset_convex_polygon(polygon, 0.0), polygon))

    def test_excessive_inset_is_rejected(self) -> None:
        polygon = np.array([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)])
        with self.assertRaises(ParameterError):
            offset_convex_polygon(polygon, -30.0)


class ModelRegionTests(unittest.TestCase):
    def test_region_needs_a_model(self) -> None:
        with self.assertRaises(ParameterError):
            build_region("model", {})

    def test_hull_outline_follows_the_projected_part(self) -> None:
        model = ModelLibrary().add("plate.stl", b"", mesh=_plate_mesh(side=60.0))
        region = build_region("model", {"outline": "hull"}, model=model)
        polygon = ensure_ccw(region.boundary())
        self.assertAlmostEqual(polygon_area(polygon), 3600.0, places=3)
        self.assertEqual(polygon_bounds(polygon), [[-30.0, 30.0], [-30.0, 30.0]])

    def test_box_outline_matches_the_bounding_box(self) -> None:
        model = ModelLibrary().add("plate.stl", b"", mesh=_plate_mesh(side=40.0))
        region = build_region("model", {"outline": "box", "margin_mm": 5.0}, model=model)
        polygon = ensure_ccw(region.boundary())
        self.assertAlmostEqual(polygon_area(polygon), 50.0 * 50.0, places=3)

    def test_describe_publishes_the_outline_parameters(self) -> None:
        model = ModelLibrary().add("plate.stl", b"", mesh=_plate_mesh())
        described = build_region("model", {"margin_mm": 2.0}, model=model).describe()
        self.assertEqual(described["id"], "model")
        self.assertEqual(described["parameters"], {"outline": "hull", "margin_mm": 2.0})


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
