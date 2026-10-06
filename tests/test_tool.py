"""刀具几何：足迹半径、圆角半径、参数构造与校验。"""

from __future__ import annotations

import unittest

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.tool import TOOL_KINDS, Tool, ToolKind, tool_parameters


class ToolGeometryTests(unittest.TestCase):
    def test_flat_tool_footprint_is_the_radius(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        self.assertAlmostEqual(tool.radius_mm, 3.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)
        self.assertAlmostEqual(tool.effective_corner_radius_mm, 0.0)

    def test_ball_tool_touches_with_its_tip(self) -> None:
        """球头刀的足迹半径为 0（只有刀尖接触），圆角半径等于刀具半径。"""

        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.effective_corner_radius_mm, 4.0)

    def test_bull_tool_sits_between_flat_and_ball(self) -> None:
        """圆鼻刀：底面半径 = R − Rc，足迹半径也是 R − Rc。"""

        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=2.0)
        self.assertAlmostEqual(tool.radius_mm, 5.0)
        self.assertAlmostEqual(tool.effective_corner_radius_mm, 2.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)

    def test_bull_corner_must_be_smaller_than_the_radius(self) -> None:
        # 圆角等于半径就成了球头刀，必须拒绝。
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=5.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=4.8)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=0.0)
        # 合法值不应抛异常。
        Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=4.5)

    def test_flat_and_ball_ignore_the_corner_parameter(self) -> None:
        """圆角只对圆鼻刀有意义，其余形态由几何唯一确定。"""

        flat = Tool.from_parameters(
            {"kind": "flat", "diameter_mm": 10.0, "length_mm": 30.0, "corner_radius_mm": 3.0}
        )
        ball = Tool.from_parameters(
            {"kind": "ball", "diameter_mm": 10.0, "length_mm": 30.0, "corner_radius_mm": 3.0}
        )
        self.assertAlmostEqual(flat.effective_corner_radius_mm, 0.0)
        self.assertAlmostEqual(ball.effective_corner_radius_mm, 5.0)

    def test_invalid_geometry_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=0.0, length_mm=30.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=-1.0)


class ToolParameterTests(unittest.TestCase):
    def test_defaults_are_a_flat_end_mill(self) -> None:
        tool = Tool.from_parameters(tool_parameters().coerce({}))
        self.assertIs(tool.kind, ToolKind.FLAT)
        self.assertEqual(tool.diameter_mm, 6.0)
        self.assertEqual(tool.length_mm, 30.0)

    def test_all_three_kinds_are_selectable(self) -> None:
        disabled = {choice.value: choice.disabled for choice in TOOL_KINDS}
        self.assertFalse(disabled["flat"])
        self.assertFalse(disabled["ball"])
        self.assertFalse(disabled["bull"])

    def test_bull_tool_is_built_from_parameters(self) -> None:
        tool = Tool.from_parameters(
            tool_parameters().coerce(
                {"kind": "bull", "diameter_mm": 10.0, "corner_radius_mm": 3.0}
            )
        )
        self.assertIs(tool.kind, ToolKind.BULL)
        self.assertAlmostEqual(tool.effective_corner_radius_mm, 3.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 2.0)

    def test_ball_tool_geometry_differs_from_flat(self) -> None:
        ball = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=30.0)
        flat = Tool(ToolKind.FLAT, diameter_mm=10.0, length_mm=30.0)
        # 球头刀的足迹半径是 0（不参与区域偏置），平底刀是整个半径。
        self.assertAlmostEqual(ball.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(flat.footprint_radius_mm, 5.0)
        self.assertAlmostEqual(ball.radius_mm - ball.footprint_radius_mm, 5.0)

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        for choice in kind_spec.to_dict()["choices"]:
            self.assertFalse(choice["disabled"])

    def test_describe_exposes_the_geometry(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "flat", "diameter_mm": 10.0, "length_mm": 45.0}
        ).describe()
        self.assertEqual(payload["diameter_mm"], 10.0)
        self.assertEqual(payload["radius_mm"], 5.0)
        self.assertEqual(payload["footprint_radius_mm"], 5.0)
        self.assertEqual(payload["length_mm"], 45.0)
        self.assertEqual(payload["corner_radius_mm"], 0.0)
        self.assertIn("kind_label", payload)

    def test_describe_reports_the_bull_corner(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "bull", "diameter_mm": 12.0, "length_mm": 45.0, "corner_radius_mm": 2.0}
        ).describe()
        self.assertEqual(payload["radius_mm"], 6.0)
        self.assertEqual(payload["corner_radius_mm"], 2.0)
        self.assertEqual(payload["footprint_radius_mm"], 4.0)

    def test_out_of_range_diameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"diameter_mm": 0.1})

    def test_out_of_range_corner_is_rejected_by_coercion(self) -> None:
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"corner_radius_mm": 0.1})


if __name__ == "__main__":
    unittest.main()
