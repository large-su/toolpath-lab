"""加工面：平面、斜面、波浪面与导入模型。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError, RegistryError
from toolpath_lab.core.mesh import Mesh, ModelLibrary
from toolpath_lab.core.surface import (
    SURFACES,
    FlatSurface,
    ModelSurface,
    SlopeSurface,
    WaveSurface,
    build_surface,
    probe_points,
    surface_catalog,
)

SQUARE = np.array([(-30.0, -30.0), (30.0, -30.0), (30.0, 30.0), (-30.0, 30.0)])


def plate(height: float = 10.0, side: float = 60.0) -> Mesh:
    """一块平板：顶面在 Z = height，底面在 Z = 0。"""

    half = side / 2.0

    def face(z: float):
        return [
            (-half, -half, z), (half, -half, z), (half, half, z), (-half, half, z),
        ]

    points = face(height) + face(0.0)
    triangles = [
        [points[0], points[1], points[2]],
        [points[0], points[2], points[3]],
        [points[4], points[5], points[6]],
        [points[4], points[6], points[7]],
    ]
    return Mesh(np.array(triangles, dtype=np.float64))


class SurfaceCatalogTests(unittest.TestCase):
    def test_registered_surfaces(self) -> None:
        self.assertEqual(SURFACES.ids(), ["flat", "slope", "wave", "model"])

    def test_catalog_publishes_labels_and_parameters(self) -> None:
        entries = {entry["id"]: entry for entry in surface_catalog()}
        self.assertEqual(entries["flat"]["label"], "平面")
        self.assertEqual([item["key"] for item in entries["flat"]["parameters"]], ["z_offset_mm"])
        self.assertEqual(
            [item["key"] for item in entries["slope"]["parameters"]],
            ["tilt_deg", "direction_deg", "z_offset_mm"],
        )
        self.assertEqual(
            [item["key"] for item in entries["wave"]["parameters"]],
            ["amplitude_mm", "wavelength_mm", "direction_deg", "z_offset_mm"],
        )

    def test_unknown_surface_raises(self) -> None:
        with self.assertRaises(RegistryError):
            build_surface("saddle", {})

    def test_build_coerces_the_parameters(self) -> None:
        surface = build_surface("wave", {"amplitude_mm": "3", "wavelength_mm": 8})
        self.assertEqual(surface.amplitude_mm, 3.0)
        self.assertEqual(surface.wavelength_mm, 8.0)
        self.assertEqual(surface.direction_deg, 0.0)


class FlatSurfaceTests(unittest.TestCase):
    def test_heights_are_constant(self) -> None:
        surface = FlatSurface(z_offset_mm=2.5)
        points = np.array([[-10.0, 0.0], [0.0, 20.0], [7.0, -3.0]])
        self.assertTrue(np.allclose(surface.heights(points), 2.5))

    def test_flat_surface_is_planar(self) -> None:
        surface = build_surface("flat", {})
        self.assertTrue(surface.is_planar)
        self.assertEqual(surface.sample_extremes(SQUARE), (0.0, 0.0))
        self.assertIn("平面", surface.note())

    def test_default_flat_surface_has_no_offset(self) -> None:
        self.assertEqual(build_surface("flat", {}).to_params(), {"z_offset_mm": 0.0})


class SlopeSurfaceTests(unittest.TestCase):
    def test_heights_follow_the_tilt(self) -> None:
        surface = SlopeSurface(tilt_deg=45.0, direction_deg=0.0)
        heights = surface.heights(np.array([[-10.0, 0.0], [0.0, 0.0], [10.0, 0.0]]))
        self.assertTrue(np.allclose(heights, [-10.0, 0.0, 10.0], atol=1e-9))

    def test_direction_rotates_the_slope(self) -> None:
        surface = SlopeSurface(tilt_deg=45.0, direction_deg=90.0)
        heights = surface.heights(np.array([[0.0, 0.0], [0.0, 10.0], [10.0, 0.0]]))
        self.assertTrue(np.allclose(heights, [0.0, 10.0, 0.0], atol=1e-9))

    def test_offset_shifts_everything(self) -> None:
        surface = SlopeSurface(tilt_deg=0.0, z_offset_mm=-3.0)
        self.assertTrue(np.allclose(surface.heights(np.array([[5.0, 5.0]])), -3.0))

    def test_extremes_cover_the_region(self) -> None:
        surface = SlopeSurface(tilt_deg=45.0)
        low, high = surface.sample_extremes(SQUARE)
        self.assertAlmostEqual(low, -30.0, places=6)
        self.assertAlmostEqual(high, 30.0, places=6)
        self.assertFalse(surface.is_planar)

    def test_note_mentions_the_angles(self) -> None:
        note = SlopeSurface(tilt_deg=20.0, direction_deg=45.0).note()
        self.assertIn("20", note)
        self.assertIn("45", note)


class WaveSurfaceTests(unittest.TestCase):
    def test_heights_follow_a_sine(self) -> None:
        surface = WaveSurface(amplitude_mm=4.0, wavelength_mm=40.0)
        heights = surface.heights(np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]]))
        self.assertAlmostEqual(float(heights[0]), 0.0, places=9)
        self.assertAlmostEqual(float(heights[1]), 4.0, places=9)
        self.assertAlmostEqual(float(heights[2]), 0.0, places=6)

    def test_extremes_are_analytic(self) -> None:
        surface = WaveSurface(amplitude_mm=4.0, z_offset_mm=1.0)
        self.assertEqual(surface.sample_extremes(SQUARE), (-3.0, 5.0))

    def test_zero_amplitude_is_flat_but_not_marked_planar(self) -> None:
        surface = WaveSurface(amplitude_mm=0.0)
        self.assertTrue(np.allclose(surface.heights(np.array([[3.0, 4.0]])), 0.0))
        self.assertFalse(surface.is_planar)


class ModelSurfaceTests(unittest.TestCase):
    def _model(self, height: float = 10.0):
        return ModelLibrary().add("plate.stl", b"", mesh=plate(height))

    def test_surface_needs_a_model(self) -> None:
        with self.assertRaises(ParameterError):
            build_surface("model", {})
        with self.assertRaises(ParameterError):
            ModelSurface()

    def test_heights_come_from_the_mesh(self) -> None:
        surface = build_surface("model", {"resolution_mm": 1.0}, model=self._model(10.0))
        self.assertFalse(surface.is_planar)
        heights = surface.heights(np.array([[-20.0, -20.0], [0.0, 0.0], [25.0, 25.0]]))
        self.assertTrue(np.allclose(heights, 10.0, atol=1e-6))

    def test_bottom_pick_follows_the_lower_face(self) -> None:
        surface = build_surface(
            "model", {"pick": "bottom"}, model=self._model(10.0)
        )
        self.assertTrue(np.allclose(surface.heights(np.array([[0.0, 0.0]])), 0.0))

    def test_offset_shifts_the_surface(self) -> None:
        surface = build_surface(
            "model", {"z_offset_mm": -0.5}, model=self._model(10.0)
        )
        self.assertTrue(np.allclose(surface.heights(np.array([[0.0, 0.0]])), 9.5))

    def test_extremes_are_limited_to_the_region(self) -> None:
        surface = build_surface("model", {}, model=self._model(10.0))
        smaller = np.array([(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)])
        self.assertEqual(surface.sample_extremes(smaller), (10.0, 10.0))

    def test_describe_publishes_the_model_and_the_field(self) -> None:
        model = self._model(4.0)
        described = build_surface("model", {}, model=model).describe()
        self.assertEqual(described["kind"], "model")
        self.assertEqual(described["model"]["id"], model.id)
        self.assertEqual(described["height_field"]["z_range_mm"], [4.0, 4.0])
        self.assertEqual(described["warnings"], [])

    def test_note_mentions_the_model_name(self) -> None:
        surface = build_surface("model", {}, model=self._model())
        self.assertIn("plate.stl", surface.note())


class ProbePointTests(unittest.TestCase):
    def test_probes_cover_the_region(self) -> None:
        probes = probe_points(SQUARE, nodes=4)
        self.assertGreater(probes.shape[0], 4)
        self.assertAlmostEqual(float(probes[:, 0].min()), -30.0)
        self.assertAlmostEqual(float(probes[:, 1].max()), 30.0)

    def test_empty_polygon_yields_no_probes(self) -> None:
        self.assertEqual(probe_points(np.zeros((0, 2))).shape, (0, 2))


if __name__ == "__main__":
    unittest.main()
