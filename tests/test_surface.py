"""平面与解析自由曲面的几何、采样和参数校验。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import (
    CompositeSurface,
    DomeSurface,
    FlatSurface,
    FreeformSurface,
    RadialRippleSurface,
    SaddleSurface,
    build_surface,
)


class SurfaceTests(unittest.TestCase):
    def test_flat_surface_has_constant_height(self) -> None:
        surface = FlatSurface(base_z_mm=3.0)
        heights = surface.height_at(np.array([[0, 0], [10, 20]], dtype=float))
        np.testing.assert_allclose(heights, [3.0, 3.0])
        self.assertEqual(surface.height_bounds(), (3.0, 3.0))

    def test_freeform_surface_follows_formula(self) -> None:
        surface = FreeformSurface(amplitude_mm=4.0, wavelength_x_mm=40.0,
                                  wavelength_y_mm=60.0)
        heights = surface.height_at(np.array([[0, 0], [10, 0], [20, 0]], dtype=float))
        np.testing.assert_allclose(heights, [0.0, 4.0, 0.0], atol=1e-10)
        self.assertEqual(surface.height_bounds(), (-4.0, 4.0))

    def test_freeform_mesh_has_faces_and_upward_normals(self) -> None:
        surface = FreeformSurface()
        mesh = surface.mesh_payload(build_region("square", {"side_mm": 80.0}), resolution=8)
        self.assertGreater(len(mesh["indices"]), 0)
        a, b, c = (np.array(mesh["vertices"][index]) for index in mesh["indices"][:3])
        self.assertGreater(np.cross(b - a, c - a)[2], 0)

    def test_invalid_wavelength_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_surface("freeform", {"wavelength_x_mm": 0})

    def test_freeform_rotation_preserves_height_bounds(self) -> None:
        surface = FreeformSurface(amplitude_mm=6.0, rotation_deg=45.0)
        self.assertEqual(surface.height_bounds(), (-6.0, 6.0))
        values = surface.height_at(np.array([[0, 0], [20, 10]], dtype=float))
        self.assertTrue(np.all(np.isfinite(values)))

    def test_richer_surfaces_have_bounded_heights(self) -> None:
        surfaces = (
            SaddleSurface(),
            DomeSurface(),
            RadialRippleSurface(),
            CompositeSurface(),
        )
        points = np.array([[-40, -30], [0, 0], [40, 30]], dtype=float)
        for surface in surfaces:
            with self.subTest(surface=surface.id):
                values = surface.height_at(points)
                lower, upper = surface.height_bounds()
                self.assertTrue(np.all(values >= lower - 1e-9))
                self.assertTrue(np.all(values <= upper + 1e-9))
                self.assertGreater(surface.sampling_spacing_mm, 0.0)


if __name__ == "__main__":
    unittest.main()
