# -*- coding: utf-8 -*-
"""分层开粗（多层切削）策略测试。"""

from __future__ import annotations

import unittest

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import planner_catalog, run_plan


class LayeredPlannerTests(unittest.TestCase):
    def _plan(self, depth: float = 6.0, layer: float = 2.0,
              strategy: str = "raster", region_id: str = "square") -> object:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, material="aluminum")
        region = build_region(region_id, {"side_mm": 80.0} if region_id == "square"
                              else {"diameter_mm": 80.0})
        return run_plan(
            planner_id="layered",
            tool=tool,
            region=region,
            parameters={
                "strategy": strategy,
                "stepover_mm": 6.0,
                "depth_mm": depth,
                "layer_depth_mm": layer,
                "feed_mm_per_min": 600.0,
            },
        ).toolpath

    @staticmethod
    def _cut_z_values(toolpath) -> set[float]:
        return {
            round(float(point[2]), 3)
            for move in toolpath.moves
            if move.kind is not MoveKind.RAPID
            for point in move.points
        }

    def test_three_layers_with_two_mm_each(self) -> None:
        toolpath = self._plan(depth=6.0, layer=2.0)
        self.assertEqual(self._cut_z_values(toolpath), {-2.0, -4.0, -6.0})

    def test_odd_depth_rounds_up_and_clamps_last_layer(self) -> None:
        toolpath = self._plan(depth=5.0, layer=2.0)  # 3 层：-2 -4 -5
        self.assertEqual(self._cut_z_values(toolpath), {-2.0, -4.0, -5.0})

    def test_pass_count_multiplies_across_layers(self) -> None:
        toolpath = self._plan(depth=6.0, layer=2.0)
        # 80 方形单层栅格 14 刀 × 3 层 = 42 刀
        self.assertEqual(toolpath.pass_count, 42)

    def test_each_layer_has_a_plunge_and_final_retract(self) -> None:
        toolpath = self._plan(depth=6.0, layer=2.0)
        rapids = [move for move in toolpath.moves if move.kind is MoveKind.RAPID]
        plunges = [
            move for move in rapids
            if any(float(point[2]) > 4.9 for point in move.points)
            and move.points[-1][2] < 0.0
        ]
        self.assertEqual(len(plunges), 3)  # 每层一次下刀
        self.assertGreater(moves_last_z(rapids[-1]), 0.0)  # 最后抬到安全高度

    def test_contour_strategy_is_supported(self) -> None:
        toolpath = self._plan(depth=4.0, layer=2.0, strategy="contour")
        self.assertEqual(self._cut_z_values(toolpath), {-2.0, -4.0})

    def test_invalid_parameters_are_rejected(self) -> None:
        with self.assertRaises((ParameterError, PlanningError)):
            self._plan(depth=0.0)
        with self.assertRaises((ParameterError, PlanningError)):
            self._plan(layer=0.0)

    def test_gcode_contains_layered_depths_and_spindle(self) -> None:
        toolpath = self._plan(depth=6.0, layer=2.0)
        gcode = toolpath_to_gcode(toolpath, spindle_speed_rpm=8000.0)
        self.assertIn("Z-6.000", gcode)
        self.assertIn("M3 S8000.000", gcode)
        self.assertIn("M5", gcode)
        self.assertTrue(gcode.rstrip().endswith("M30"))

    def test_layered_is_registered_in_catalog(self) -> None:
        ids = {entry["id"] for entry in planner_catalog()}
        self.assertIn("layered", ids)


def moves_last_z(rapid: object) -> float:
    return float(rapid.points[-1][2])


if __name__ == "__main__":
    unittest.main()
