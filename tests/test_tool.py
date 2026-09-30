"""刀具几何：足迹半径、参数构造与校验。"""

from __future__ import annotations

import unittest

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.tool import TOOL_KINDS, Tool, ToolKind, tool_parameters


class ToolGeometryTests(unittest.TestCase):
    def test_flat_tool_footprint_is_the_radius(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        self.assertAlmostEqual(tool.radius_mm, 3.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)

    def test_ball_tool_touches_with_its_tip(self) -> None:
        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)

    def test_bull_tool_defaults_to_a_flat_bottom(self) -> None:
        # 不给圆角时 Rc = 0，等价于平底刀：足迹 = R。
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 5.0)

    def test_bull_tool_footprint_is_radius_minus_corner_radius(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=2.0)
        self.assertAlmostEqual(tool.radius_mm, 5.0)
        self.assertAlmostEqual(tool.flat_radius_mm, 3.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)

    def test_bull_with_corner_equal_to_radius_behaves_like_a_ball(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=5.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)

    def test_bull_corner_radius_beyond_the_tool_radius_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=7.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=-1.0)

    def test_flat_and_ball_normalise_the_corner_radius(self) -> None:
        # 界面上那一格在非圆鼻刀下是隐藏的，但请求里可能残留旧值：应当被忽略而不是报错。
        self.assertAlmostEqual(
            Tool(ToolKind.FLAT, 6.0, 30.0, corner_radius_mm=2.0).corner_radius_mm, 0.0
        )
        self.assertAlmostEqual(
            Tool(ToolKind.BALL, 8.0, 30.0, corner_radius_mm=1.0).corner_radius_mm, 4.0
        )

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
        self.assertEqual(tool.corner_radius_mm, 0.0)

    def test_every_kind_is_selectable(self) -> None:
        disabled = {choice.value: choice.disabled for choice in TOOL_KINDS}
        self.assertEqual(sorted(disabled), ["ball", "bull", "flat"])
        self.assertFalse(any(disabled.values()))

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        self.assertEqual([item.disabled for item in kind_spec.choices], [False, False, False])
        self.assertEqual(
            [item.value for item in kind_spec.choices], ["flat", "ball", "bull"]
        )

    def test_corner_radius_only_shows_up_for_the_bull_nose(self) -> None:
        corner = tool_parameters().spec("corner_radius_mm")
        self.assertEqual(corner.to_dict()["visible_if"], {"kind": "bull"})
        self.assertEqual(corner.default, 0.0)

    def test_bull_nose_parameters_reach_the_tool(self) -> None:
        values = tool_parameters().coerce(
            {"kind": "bull", "diameter_mm": 12.0, "length_mm": 40.0, "corner_radius_mm": 2.0}
        )
        tool = Tool.from_parameters(values)
        self.assertIs(tool.kind, ToolKind.BULL)
        self.assertAlmostEqual(tool.footprint_radius_mm, 4.0)

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

    def test_describe_of_a_ball_tool_reports_a_zero_footprint(self) -> None:
        payload = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0).describe()
        self.assertEqual(payload["kind_label"], "球头刀 Ball nose")
        self.assertEqual(payload["footprint_radius_mm"], 0.0)
        self.assertEqual(payload["corner_radius_mm"], 4.0)

    def test_out_of_range_diameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"diameter_mm": 0.1})


if __name__ == "__main__":
    unittest.main()
