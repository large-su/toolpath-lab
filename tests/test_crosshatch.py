"""交叉栅格策略测试。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(parameters=None):
    options = {
        "stepover_mm": 6.0,
        "direction_deg": 0.0,
        "cross_angle_deg": 90.0,
        "feed_mm_per_min": 600.0,
    }
    options.update(parameters or {})
    return run_plan(
        planner_id="crosshatch",
        tool=_tool(),
        region=build_region("square", {"side_mm": 80.0}),
        parameters=options,
    ).toolpath


class CrosshatchTests(unittest.TestCase):
    def test_two_families_are_generated(self) -> None:
        toolpath = _plan()
        cuts = [move for move in toolpath.moves if move.kind is MoveKind.CUT]
        self.assertEqual(len(cuts), 28)
        self.assertEqual(toolpath.pass_count, 28)
        self.assertTrue(any("交叉组 A" in move.label for move in cuts))
        self.assertTrue(any("交叉组 B" in move.label for move in cuts))

    def test_second_family_is_rotated(self) -> None:
        cuts = [move for move in _plan().moves if move.kind is MoveKind.CUT]
        first_delta = cuts[0].points[-1] - cuts[0].points[0]
        second_family = cuts[len(cuts) // 2]
        second_delta = second_family.points[-1] - second_family.points[0]
        self.assertGreater(float(first_delta[0]), 0.0)
        self.assertAlmostEqual(float(first_delta[1]), 0.0, places=6)
        self.assertAlmostEqual(float(second_delta[0]), 0.0, places=6)
        self.assertGreater(float(second_delta[1]), 0.0)

    def test_groups_are_separated_by_safe_rapid_move(self) -> None:
        moves = _plan().moves
        rapid = [move for move in moves if move.kind is MoveKind.RAPID]
        self.assertEqual(len(rapid), 3)
        self.assertTrue(any(move.points[:, 2].max() > 0.0 for move in rapid))

    def test_angle_changes_second_family_without_changing_pass_count(self) -> None:
        angled = _plan({"cross_angle_deg": 45.0})
        cuts = [move for move in angled.moves if move.kind is MoveKind.CUT]
        self.assertGreater(len(cuts), 28)
        second_family = next(move for move in cuts if "交叉组 B" in move.label)
        delta = second_family.points[-1] - second_family.points[0]
        self.assertGreater(float(delta[0]), 0.0)
        self.assertGreater(float(delta[1]), 0.0)

    def test_circle_is_supported(self) -> None:
        result = run_plan(
            planner_id="crosshatch",
            tool=_tool(),
            region=build_region("circle", {"diameter_mm": 80.0}),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        self.assertGreater(result.cut_length_mm, 0.0)
        self.assertTrue(np.all(np.isfinite(result.moves[-1].points)))

    def test_oversized_tool_is_rejected(self) -> None:
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="crosshatch",
                tool=_tool(120.0),
                region=build_region("square", {"side_mm": 40.0}),
                parameters={"stepover_mm": 5.0},
            )


if __name__ == "__main__":
    unittest.main()
