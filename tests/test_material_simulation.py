"""2.5D stock removal simulation."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.simulation import HeightField


def _square(size: float = 10.0) -> np.ndarray:
    half = size / 2
    return np.array(
        [(-half, -half), (half, -half), (half, half), (-half, half)],
        dtype=np.float64,
    )


class HeightFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stock = HeightField(
            _square(), resolution_mm=1.0, top_z_mm=0.0, bottom_z_mm=-4.0
        )

    def test_default_stock_allowance_can_be_cut_at_the_machining_plane(self) -> None:
        stock = HeightField(_square(), resolution_mm=1.0)

        removed = stock.cut_flat_tool(np.array([0.0, 0.0, 0.0]), 1.0)

        self.assertEqual(stock.top_z_mm, 2.0)
        self.assertGreater(removed, 0.0)
        self.assertEqual(stock.heights_mm[5, 5], 0.0)

    def test_flat_tool_removes_material_inside_its_footprint(self) -> None:
        initial = self.stock.heights_mm.copy()

        removed = self.stock.cut_flat_tool(np.array([0.0, 0.0, -2.0]), 1.5)

        self.assertGreater(removed, 0.0)
        self.assertEqual(self.stock.heights_mm[5, 5], -2.0)
        self.assertEqual(self.stock.heights_mm[5, 8], initial[5, 8])

    def test_cut_depth_is_clamped_to_stock_bottom(self) -> None:
        self.stock.cut_flat_tool(np.array([0.0, 0.0, -20.0]), 1.0)

        self.assertEqual(self.stock.heights_mm[5, 5], -4.0)

    def test_rapid_moves_do_not_remove_material(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(
                    MoveKind.RAPID,
                    np.array([[0.0, 0.0, -2.0], [3.0, 0.0, -2.0]]),
                    1000.0,
                ),
            )
        )

        removed = self.stock.simulate_toolpath(toolpath, radius_mm=1.0)

        self.assertEqual(removed, 0.0)
        self.assertTrue(np.all(self.stock.heights_mm[self.stock.inside] == 0.0))

    def test_cutting_move_removes_stock_and_reset_restores_it(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(
                    MoveKind.CUT,
                    np.array([[-3.0, 0.0, -1.0], [3.0, 0.0, -1.0]]),
                    500.0,
                ),
            )
        )
        initial_inside = self.stock.heights_mm[self.stock.inside].copy()

        removed = self.stock.simulate_toolpath(toolpath, radius_mm=1.0)

        self.assertGreater(removed, 0.0)
        self.assertTrue(np.any(self.stock.heights_mm[self.stock.inside] < 0.0))
        self.stock.reset()
        np.testing.assert_array_equal(self.stock.heights_mm[self.stock.inside], initial_inside)

    def test_tilted_flat_tool_cuts_the_oriented_cylinder_contact(self) -> None:
        removed = self.stock.cut_oriented_flat_tool(
            np.array([0.0, 0.0, -1.0]),
            radius_mm=1.0,
            flute_length_mm=4.0,
            rotary_axes_deg=np.array([45.0, 0.0]),
        )

        self.assertGreater(removed, 0.0)
        self.assertLess(self.stock.heights_mm[4, 5], self.stock.heights_mm[6, 5])
        self.assertEqual(self.stock.heights_mm[9, 5], self.stock.top_z_mm)

    def test_tilted_flat_tool_respects_contact_plane_instead_of_cutting_to_bottom(self) -> None:
        self.stock.cut_oriented_flat_tool(
            np.array([0.0, 0.0, -1.0]),
            radius_mm=1.0,
            flute_length_mm=4.0,
            rotary_axes_deg=np.array([45.0, 0.0]),
        )

        self.assertLess(self.stock.heights_mm[4, 5], self.stock.heights_mm[5, 5])
        self.assertLess(self.stock.heights_mm[5, 5], self.stock.heights_mm[6, 5])
        self.assertGreater(self.stock.heights_mm[5, 5], self.stock.bottom_z_mm)

    def test_tilted_tool_body_removes_material_behind_the_flute(self) -> None:
        self.stock.cut_oriented_tool(
            np.array([0.0, 0.0, -1.0]),
            radius_mm=1.0,
            flute_length_mm=1.0,
            rotary_axes_deg=np.array([45.0, 0.0]),
            tool_kind="flat",
        )

        self.assertLess(self.stock.heights_mm[4, 5], -1.5)
        self.assertLess(self.stock.heights_mm[5, 5], self.stock.top_z_mm)

    def test_toolpath_simulation_uses_per_point_rotary_axes(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(
                    MoveKind.CUT,
                    np.array([[0.0, 0.0, -1.0], [0.0, 0.0, -1.0]]),
                    500.0,
                    rotary_axes=np.array([[45.0, 0.0], [45.0, 0.0]]),
                ),
            )
        )

        self.stock.simulate_toolpath(toolpath, radius_mm=1.0, flute_length_mm=4.0)

        self.assertLess(self.stock.heights_mm[4, 5], self.stock.heights_mm[6, 5])

    def test_ball_nose_tip_has_spherical_contact_profile(self) -> None:
        self.stock.cut_oriented_tool(
            np.array([0.0, 0.0, -1.0]),
            radius_mm=1.0,
            flute_length_mm=4.0,
            rotary_axes_deg=np.array([0.0, 0.0]),
            tool_kind="ball",
        )

        self.assertAlmostEqual(self.stock.heights_mm[5, 5], -1.0)
        self.assertLess(self.stock.heights_mm[5, 5], self.stock.heights_mm[5, 6])
        self.assertEqual(self.stock.heights_mm[5, 7], self.stock.top_z_mm)

    def test_bull_nose_tip_has_flat_center_and_rounded_edge(self) -> None:
        self.stock.cut_oriented_tool(
            np.array([0.0, 0.0, -1.0]),
            radius_mm=1.0,
            flute_length_mm=4.0,
            rotary_axes_deg=np.array([0.0, 0.0]),
            tool_kind="bull",
            corner_radius_mm=0.5,
        )

        center_height = self.stock.heights_mm[5, 5]
        edge_height = self.stock.heights_mm[5, 6]
        self.assertAlmostEqual(center_height, -1.0)
        self.assertGreater(edge_height, center_height)
        self.assertLess(edge_height, self.stock.top_z_mm)
        self.assertEqual(self.stock.heights_mm[5, 7], self.stock.top_z_mm)

    def test_toolpath_simulation_dispatches_ball_and_bull_profiles(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(
                    MoveKind.CUT,
                    np.array([[0.0, 0.0, -1.0], [0.0, 0.0, -1.0]]),
                    500.0,
                    rotary_axes=np.zeros((2, 2)),
                ),
            )
        )
        results = {}
        for tool_kind in ("ball", "bull"):
            self.stock.reset()
            removed = self.stock.simulate_toolpath(
                toolpath,
                radius_mm=1.0,
                flute_length_mm=4.0,
                tool_kind=tool_kind,
                corner_radius_mm=0.5,
            )
            results[tool_kind] = self.stock.heights_mm.copy()
            self.assertGreater(removed, 0.0)
            self.assertLess(self.stock.heights_mm[5, 5], self.stock.top_z_mm)
        self.assertFalse(np.array_equal(results["ball"], results["bull"]))

    def test_invalid_stock_and_tool_inputs_are_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            HeightField(_square(), resolution_mm=0.0)
        with self.assertRaises(ParameterError):
            self.stock.cut_flat_tool(np.array([0.0, 0.0, 0.0]), radius_mm=0.0)


if __name__ == "__main__":
    unittest.main()
