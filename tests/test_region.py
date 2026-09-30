"""区域形状：方形、圆形与斜坡。"""

from __future__ import annotations

import unittest
from math import pi, radians, tan

import numpy as np

from toolpath_lab.core.errors import ParameterError, RegistryError
from toolpath_lab.core.region import (
    CIRCLE_SEGMENTS,
    RAMP_CAP_MM,
    RAMP_MAX_ANGLE_DEG,
    REGION_SHAPES,
    RegionShape,
    build_region,
    polygon_area,
    polygon_bounds,
    region_catalog,
)
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area


class RegionCatalogTests(unittest.TestCase):
    def test_three_shapes_are_registered(self) -> None:
        self.assertEqual(sorted(REGION_SHAPES.ids()), ["circle", "ramp", "square"])

    def test_catalog_publishes_labels_and_parameters(self) -> None:
        entries = {entry["id"]: entry for entry in region_catalog()}
        self.assertEqual(entries["square"]["label"], "方形")
        self.assertEqual(entries["ramp"]["label"], "斜坡")
        self.assertEqual(
            [item["key"] for item in entries["square"]["parameters"]],
            ["side_mm", "height_mm", "thickness_mm"],
        )
        self.assertEqual(
            [item["key"] for item in entries["circle"]["parameters"]],
            ["diameter_mm", "height_mm", "thickness_mm"],
        )
        self.assertEqual(
            [item["key"] for item in entries["ramp"]["parameters"]],
            ["side_mm", "angle_deg", "include_plateau", "thickness_mm"],
        )

    def test_flat_shapes_publish_a_flat_surface(self) -> None:
        for shape in ("square", "circle"):
            self.assertEqual(build_region(shape, {}).surface_payload()["kind"], "flat")

    def test_unknown_shape_raises(self) -> None:
        with self.assertRaises(RegistryError):
            build_region("hexagon", {})


class PlanarHeightTests(unittest.TestCase):
    """平面区域（方形 / 圆形）可以设置加工面高度；斜坡暂时没有这个参数。"""

    def _region(self, shape: str, height: float):
        key = "side_mm" if shape == "square" else "diameter_mm"
        return build_region(shape, {key: 80.0, "height_mm": height})

    def test_default_height_is_zero(self) -> None:
        for shape in ("square", "circle"):
            region = self._region(shape, 0.0)
            self.assertEqual(region.height_mm, 0.0)
            self.assertTrue(np.allclose(region.height_at(region.boundary()), 0.0))
            self.assertEqual(region.surface_payload()["base_z_mm"], 0.0)

    def test_height_lifts_the_whole_surface(self) -> None:
        for shape in ("square", "circle"):
            region = self._region(shape, 20.0)
            self.assertTrue(np.allclose(region.height_at(region.boundary()), 20.0))
            outline = region.boundary_3d()
            self.assertTrue(np.allclose(outline[:, 2], 20.0))
            payload = region.surface_payload()
            self.assertEqual(payload["kind"], "flat")
            self.assertEqual(payload["base_z_mm"], 20.0)
            self.assertEqual(payload["top_z_mm"], 20.0)

    def test_height_can_be_negative(self) -> None:
        region = self._region("square", -5.0)
        self.assertTrue(np.allclose(region.height_at(region.boundary()), -5.0))

    def test_height_is_published_with_its_range(self) -> None:
        entries = {entry["id"]: entry for entry in region_catalog()}
        for shape in ("square", "circle"):
            height = [item for item in entries[shape]["parameters"]
                      if item["key"] == "height_mm"][0]
            self.assertEqual(height["default"], 0.0)
            self.assertEqual(height["min"], -100.0)
            self.assertEqual(height["max"], 100.0)

    def test_height_out_of_range_is_rejected(self) -> None:
        for shape in ("square", "circle"):
            with self.assertRaises(ParameterError):
                self._region(shape, 1000.0)

    def test_ramp_does_not_expose_a_height_parameter(self) -> None:
        keys = [item["key"] for item in
                {entry["id"]: entry for entry in region_catalog()}["ramp"]["parameters"]]
        self.assertNotIn("height_mm", keys)
        self.assertFalse(hasattr(build_region("ramp", {}), "height_mm"))


class PartThicknessTests(unittest.TestCase):
    """部件厚度：加工面以下那块基体有多厚，三个区域都能设，纯几何、不参与刀路计算。"""

    def _region(self, shape: str, thickness: float):
        key = {"square": "side_mm", "circle": "diameter_mm", "ramp": "side_mm"}[shape]
        return build_region(shape, {key: 80.0, "thickness_mm": thickness})

    def test_default_thickness_is_published_for_every_shape(self) -> None:
        entries = {entry["id"]: entry for entry in region_catalog()}
        for shape in ("square", "circle", "ramp"):
            thickness = [item for item in entries[shape]["parameters"]
                         if item["key"] == "thickness_mm"][0]
            self.assertEqual(thickness["default"], 20.0)
            self.assertEqual(thickness["min"], 1.0)
            self.assertEqual(thickness["max"], 500.0)
            self.assertEqual(self._region(shape, 20.0).describe()["thickness_mm"], 20.0)

    def test_custom_thickness_reaches_the_description(self) -> None:
        for shape in ("square", "circle", "ramp"):
            region = self._region(shape, 45.0)
            self.assertEqual(region.thickness_mm, 45.0)
            self.assertEqual(region.describe()["thickness_mm"], 45.0)

    def test_thickness_does_not_touch_the_machining_surface(self) -> None:
        # 厚度只是"料有多厚"，加工面与刀路都不该受影响
        for shape in ("square", "circle", "ramp"):
            thin = self._region(shape, 5.0)
            thick = self._region(shape, 80.0)
            samples = thin.boundary()
            self.assertTrue(np.allclose(thin.height_at(samples), thick.height_at(samples)))
            self.assertEqual(thin.boundary().shape, thick.boundary().shape)

    def test_out_of_range_thickness_is_rejected(self) -> None:
        for shape in ("square", "circle", "ramp"):
            with self.assertRaises(ParameterError):
                self._region(shape, 0.5)
            with self.assertRaises(ParameterError):
                self._region(shape, 1000.0)

    def test_shape_without_the_field_falls_back_to_the_default(self) -> None:
        # 扩展路径：新形状只实现 boundary() 时，describe() 也要能用
        class Bare(RegionShape):
            id = "bare"

            def boundary(self):
                return np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])

            def height_at(self, points_xy):
                return np.zeros(np.asarray(points_xy).reshape(-1, 2).shape[0])

        self.assertEqual(Bare().describe()["thickness_mm"], 20.0)


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


class RampRegionTests(unittest.TestCase):
    """斜坡：XY 投影是 80 × 80，加工面沿 +X 抬起，最高到 80 mm 后转平顶。"""

    def _ramp(self, angle: float, side: float = 80.0):
        return build_region("ramp", {"side_mm": side, "angle_deg": angle})

    def test_projection_is_the_same_square(self) -> None:
        region = self._ramp(30.0)
        polygon = ensure_ccw(region.boundary())
        self.assertEqual(polygon.shape, (4, 2))
        self.assertAlmostEqual(signed_area(polygon), 6400.0)
        self.assertEqual(polygon_bounds(polygon), [[-40.0, 40.0], [-40.0, 40.0]])

    def test_zero_degrees_is_a_flat_square(self) -> None:
        region = self._ramp(0.0)
        self.assertTrue(np.allclose(region.height_at(region.boundary()), 0.0))
        self.assertIsNone(region.crease_x_mm)
        self.assertEqual(region.surface_payload()["patch_count"], 1)

    def test_height_follows_the_slope_from_the_low_edge(self) -> None:
        region = self._ramp(30.0)
        slope = tan(radians(30.0))
        heights = region.height_at(np.array([[-40.0, 0.0], [0.0, 0.0], [40.0, 0.0]]))
        # 低边是 +X 方向最外侧边（Z = 0），沿 −X 方向升高
        self.assertAlmostEqual(float(heights[0]), 80.0 * slope, places=9)
        self.assertAlmostEqual(float(heights[1]), 40.0 * slope, places=9)
        self.assertAlmostEqual(float(heights[2]), 0.0, places=9)

    def test_corner_heights_are_low_on_the_plus_x_side(self) -> None:
        region = self._ramp(30.0)
        heights = region.height_at(region.boundary())
        peak = 80.0 * tan(radians(30.0))
        self.assertAlmostEqual(float(heights[0]), peak, places=9)   # (-40, -40)
        self.assertAlmostEqual(float(heights[1]), 0.0, places=9)    # (+40, -40)
        self.assertAlmostEqual(float(heights[2]), 0.0, places=9)    # (+40, +40)
        self.assertAlmostEqual(float(heights[3]), peak, places=9)   # (-40, +40)

    def test_height_is_capped_at_the_ramp_cap(self) -> None:
        region = self._ramp(60.0)
        self.assertAlmostEqual(region.peak_z_mm, RAMP_CAP_MM, places=9)
        uncapped = 80.0 * tan(radians(60.0))
        self.assertGreater(uncapped, RAMP_CAP_MM)
        self.assertAlmostEqual(float(region.height_at(np.array([[-40.0, 0.0]]))[0]), RAMP_CAP_MM)

    def test_crease_appears_only_when_the_slope_reaches_the_cap(self) -> None:
        self.assertIsNone(self._ramp(30.0).crease_x_mm)
        self.assertIsNone(self._ramp(45.0).crease_x_mm)  # 45° 正好在 −X 边上到 80 mm
        expected = 40.0 - RAMP_CAP_MM / tan(radians(60.0))
        self.assertAlmostEqual(self._ramp(60.0).crease_x_mm, expected, places=9)
        self.assertLess(self._ramp(60.0).crease_x_mm, 0.0)

    def test_flat_top_sits_on_the_minus_x_side_of_the_crease(self) -> None:
        region = self._ramp(60.0)
        crease = region.crease_x_mm
        heights = region.height_at(
            np.array([[crease + 5.0, 0.0], [crease, 0.0], [crease - 5.0, 0.0]])
        )
        self.assertLess(float(heights[0]), RAMP_CAP_MM)   # 折痕靠 +X 一侧还是斜面
        self.assertAlmostEqual(float(heights[1]), RAMP_CAP_MM, places=9)
        self.assertAlmostEqual(float(heights[2]), RAMP_CAP_MM, places=9)

    def test_surface_is_split_into_ramp_and_flat_patches(self) -> None:
        self.assertEqual(len(self._ramp(30.0).surface_patches()), 1)
        crease = self._ramp(60.0).crease_x_mm
        patches = self._ramp(60.0).surface_patches()
        self.assertEqual(len(patches), 2)
        for patch in patches:
            self.assertEqual(patch.shape, (4, 3))
        # 斜段从折痕到低边（+X），平顶从 −X 边到折痕
        self.assertAlmostEqual(float(patches[0][:, 0].min()), crease, places=9)
        self.assertAlmostEqual(float(patches[0][:, 0].max()), 40.0, places=9)
        self.assertAlmostEqual(float(patches[0][:, 2].max()), RAMP_CAP_MM, places=9)
        self.assertAlmostEqual(float(patches[1][:, 0].min()), -40.0, places=9)
        self.assertAlmostEqual(float(patches[1][:, 0].max()), crease, places=9)
        self.assertAlmostEqual(float(patches[1][:, 2].min()), RAMP_CAP_MM, places=9)

    def test_surface_breaks_only_when_the_segment_crosses_the_crease(self) -> None:
        region = self._ramp(60.0)
        crease = region.crease_x_mm
        crossing = region.surface_breaks(np.array([-40.0, 0.0]), np.array([40.0, 0.0]))
        self.assertEqual(crossing.shape, (1, 2))
        self.assertAlmostEqual(float(crossing[0][0]), crease, places=9)
        self.assertAlmostEqual(float(crossing[0][1]), 0.0, places=9)
        same_side = region.surface_breaks(np.array([10.0, 0.0]), np.array([40.0, 0.0]))
        self.assertEqual(same_side.shape[0], 0)
        no_crease = self._ramp(30.0).surface_breaks(np.array([-40.0, 0.0]), np.array([40.0, 0.0]))
        self.assertEqual(no_crease.shape[0], 0)

    def test_surface_payload_describes_the_ramp(self) -> None:
        payload = self._ramp(60.0).surface_payload()
        self.assertEqual(payload["kind"], "ramp")
        self.assertEqual(payload["angle_deg"], 60.0)
        self.assertEqual(payload["cap_z_mm"], RAMP_CAP_MM)
        self.assertAlmostEqual(payload["crease_x_mm"], self._ramp(60.0).crease_x_mm, places=9)
        self.assertEqual(payload["base_z_mm"], 0.0)
        self.assertEqual(payload["top_z_mm"], RAMP_CAP_MM)
        self.assertEqual(payload["patch_count"], 2)

    def test_outline_carries_the_surface_height(self) -> None:
        outline = self._ramp(60.0).boundary_3d()
        self.assertEqual(outline.shape, (4, 3))
        self.assertEqual(sorted(float(z) for z in outline[:, 2]), [0.0, 0.0, 80.0, 80.0])

    def test_angle_is_limited_to_the_published_maximum(self) -> None:
        self.assertEqual(
            build_region("ramp", {}).angle_deg, 30.0
        )
        self._ramp(RAMP_MAX_ANGLE_DEG)  # 80° 仍然合法
        with self.assertRaises(ParameterError):
            self._ramp(RAMP_MAX_ANGLE_DEG + 1.0)
        with self.assertRaises(ParameterError):
            self._ramp(-1.0)

    def test_too_small_side_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            self._ramp(30.0, side=1.0)

    def test_plateau_is_not_machined_by_default(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0})
        self.assertFalse(region.include_plateau)
        polygon = ensure_ccw(region.machining_boundary(0.0))
        # 范围收到斜面段：靠平顶的那条边正是折痕
        self.assertAlmostEqual(polygon[:, 0].min(), region.crease_x_mm, places=9)
        self.assertAlmostEqual(polygon[:, 0].max(), 40.0, places=9)
        self.assertAlmostEqual(signed_area(polygon),
                               80.0 * (40.0 - region.crease_x_mm), places=6)

    def test_machining_boundary_leaves_a_footprint_at_the_crease(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0})
        # 内缩一个足迹半径之后刀路正好停在折痕上，所以这里先往外让出一个足迹
        self.assertAlmostEqual(
            float(region.machining_boundary(3.0)[:, 0].min()),
            region.crease_x_mm - 3.0,
            places=9,
        )

    def test_machining_boundary_is_the_whole_square_without_a_plateau(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 30.0})
        self.assertIsNone(region.crease_x_mm)
        polygon = ensure_ccw(region.machining_boundary(3.0))
        self.assertEqual(polygon.shape, (4, 2))
        self.assertAlmostEqual(signed_area(polygon), 6400.0)

    def test_machining_boundary_is_the_whole_square_when_the_plateau_is_included(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0,
                                       "include_plateau": True})
        polygon = ensure_ccw(region.machining_boundary(3.0))
        self.assertAlmostEqual(signed_area(polygon), 6400.0)
        self.assertAlmostEqual(float(polygon[:, 0].min()), -40.0, places=9)

    def test_machining_outline_carries_the_surface_height(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0})
        outline = region.machining_boundary_3d(0.0)
        self.assertTrue(
            np.allclose(outline[:, 2], region.height_at(outline[:, :2]), atol=1e-9)
        )


if __name__ == "__main__":
    unittest.main()
