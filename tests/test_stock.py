"""材料切除高度场仿真测试。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.simulation import StockState, stock_spec_for


class StockTests(unittest.TestCase):
    def _state(self) -> StockState:
        spec = stock_spec_for(
            build_region("square", {"side_mm": 20.0}),
            build_surface("flat", {"base_z_mm": 0.0}),
            Tool(ToolKind.FLAT, diameter_mm=4.0, length_mm=20.0),
            resolution_mm=1.0,
        )
        return StockState(spec)

    def test_stock_has_initial_height_and_region_mask(self) -> None:
        state = self._state()
        self.assertGreater(float(state.heights[state.active].min()), 0.0)
        self.assertTrue(np.all(state.heights[~state.active] == state.spec.bottom_z_mm))

    def test_tool_point_removes_local_material(self) -> None:
        state = self._state()
        before = state.remaining_volume_mm3()
        state.remove_tool_point([0.0, 0.0, 0.0])
        self.assertLess(state.remaining_volume_mm3(), before)
        self.assertAlmostEqual(float(state.heights[10, 10]), 0.0, places=6)

    def test_tool_segment_cuts_between_sampled_points(self) -> None:
        state = self._state()
        before = state.remaining_volume_mm3()
        state.remove_tool_segment([-8.0, 0.0, 0.0], [8.0, 0.0, 0.0])
        self.assertLess(state.remaining_volume_mm3(), before)
        self.assertAlmostEqual(float(state.heights[10, 5]), 0.0, places=6)
        self.assertAlmostEqual(float(state.heights[10, 15]), 0.0, places=6)

    def test_reset_restores_initial_stock(self) -> None:
        state = self._state()
        initial = state.heights.copy()
        state.remove_tool_point([0.0, 0.0, 0.0])
        state.reset()
        np.testing.assert_allclose(state.heights, initial)
        self.assertEqual(state.remaining_volume_mm3(), float(np.maximum(
            initial - state.spec.bottom_z_mm, 0.0
        ).sum() * state.spec.resolution_mm**2))


if __name__ == "__main__":
    unittest.main()
