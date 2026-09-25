"""五轴刀轴姿态规划、时间轴和 G-code 导出测试。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan
from toolpath_lab.simulation import build_timeline


class FiveAxisTests(unittest.TestCase):
    def _plan(self, **parameters):
        options = {"stepover_mm": 8.0, "lead_deg": 12.0, "side_tilt_deg": 4.0}
        options.update(parameters)
        return run_plan(
            planner_id="five_axis",
            tool=Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=40.0),
            region=build_region("square", {"side_mm": 80.0}),
            surface=build_surface("freeform", {"amplitude_mm": 4.0}),
            parameters=options,
        ).toolpath

    def test_cut_moves_carry_normalized_axes(self) -> None:
        toolpath = self._plan()
        cuts = [move for move in toolpath.moves if move.kind is MoveKind.CUT]
        self.assertTrue(cuts)
        self.assertTrue(toolpath.is_oriented)
        self.assertTrue(any(abs(float(axis[0])) > 1e-4 for axis in cuts[0].tool_axes))
        for move in cuts:
            np.testing.assert_allclose(np.linalg.norm(move.tool_axes, axis=1), 1.0, atol=1e-8)

    def test_timeline_interpolates_tool_axes(self) -> None:
        timeline = build_timeline(self._plan())
        self.assertIsNotNone(timeline.tool_axes)
        state = timeline.state_at(timeline.duration_s * 0.5)
        self.assertAlmostEqual(float(np.linalg.norm(state.tool_axis)), 1.0, places=6)

    def test_freeform_recomputes_axis_at_dense_points(self) -> None:
        cuts = [move for move in self._plan().moves if move.kind is MoveKind.CUT]
        self.assertGreater(cuts[0].points.shape[0], 2)
        self.assertGreater(float(np.ptp(cuts[0].tool_axes, axis=0).max()), 1e-3)

    def test_tilted_flat_tool_gets_contact_clearance(self) -> None:
        toolpath = run_plan(
            planner_id="five_axis",
            tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=40.0),
            region=build_region("square", {"side_mm": 80.0}),
            surface=build_surface("flat", {"base_z_mm": 0.0}),
            parameters={
                "stepover_mm": 8.0,
                "lead_deg": 20.0,
                "side_tilt_deg": 0.0,
                "feed_mm_per_min": 500.0,
            },
        ).toolpath
        cut = next(move for move in toolpath.moves if move.kind is MoveKind.CUT)
        expected = 3.0 * np.sin(np.deg2rad(20.0))
        self.assertTrue(np.all(cut.points[:, 2] >= expected - 1e-6))

    def test_gcode_contains_ab_axes(self) -> None:
        gcode = toolpath_to_gcode(self._plan())
        self.assertIn("(five-axis: A=azimuth deg, B=tilt-from-Z deg)", gcode)
        self.assertRegex(gcode, r"G1 .* A-?\d+\.\d+ B\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
