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

    def test_flat_tool_ignores_corner_radius(self) -> None:
        """平底刀没有刀尖圆角，构造时强制归零。"""

        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, corner_radius_mm=2.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)

    def test_ball_tool_touches_with_its_tip(self) -> None:
        """球头刀足迹半径为 0（只有刀尖接触），圆角半径自动取半径。"""

        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)

    def test_bull_tool_uses_its_flat_bottom(self) -> None:
        """圆鼻刀默认无圆角时等同平底；带圆角时足迹半径 = R - Rc。"""

        bare = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0)
        self.assertAlmostEqual(bare.corner_radius_mm, 0.0)
        self.assertAlmostEqual(bare.footprint_radius_mm, 5.0)

        rounded = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=2.0)
        self.assertAlmostEqual(rounded.corner_radius_mm, 2.0)
        self.assertAlmostEqual(rounded.footprint_radius_mm, 3.0)

    def test_bull_corner_radius_is_clamped_to_the_radius(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=9.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 5.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)

    def test_invalid_geometry_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=0.0, length_mm=30.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=-1.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=6.0, length_mm=30.0, corner_radius_mm=-1.0)


class ToolParameterTests(unittest.TestCase):
    def test_defaults_are_a_flat_end_mill(self) -> None:
        tool = Tool.from_parameters(tool_parameters().coerce({}))
        self.assertIs(tool.kind, ToolKind.FLAT)
        self.assertEqual(tool.diameter_mm, 6.0)
        self.assertEqual(tool.length_mm, 30.0)
        self.assertEqual(tool.corner_radius_mm, 0.0)

    def test_all_three_kinds_are_selectable(self) -> None:
        for choice in TOOL_KINDS:
            self.assertFalse(choice.disabled, f"{choice.value} 应可选")

    def test_corner_radius_parameter_is_published_and_gated(self) -> None:
        """圆角半径参数存在，且仅在类型为圆鼻时显示。"""

        corner = tool_parameters().spec("corner_radius_mm")
        self.assertEqual(dict(corner.visible_if), {"kind": "bull"})
        self.assertEqual(corner.to_dict()["visible_if"], {"kind": "bull"})

    def test_ball_kind_from_parameters_forces_corner_to_radius(self) -> None:
        tool = Tool.from_parameters(
            {"kind": "ball", "diameter_mm": 8.0, "length_mm": 40.0, "corner_radius_mm": 1.0}
        )
        self.assertIs(tool.kind, ToolKind.BALL)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)

    def test_bull_kind_from_parameters_uses_corner(self) -> None:
        tool = Tool.from_parameters(
            {"kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0, "corner_radius_mm": 2.5}
        )
        self.assertAlmostEqual(tool.corner_radius_mm, 2.5)
        self.assertAlmostEqual(tool.footprint_radius_mm, 2.5)

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        for choice in kind_spec.to_dict()["choices"]:
            self.assertFalse(choice.get("disabled", False))

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

    def test_describe_exposes_bull_corner_radius(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "bull", "diameter_mm": 10.0, "length_mm": 45.0, "corner_radius_mm": 2.0}
        ).describe()
        self.assertEqual(payload["corner_radius_mm"], 2.0)
        self.assertEqual(payload["footprint_radius_mm"], 3.0)

    def test_out_of_range_diameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"diameter_mm": 0.1})


if __name__ == "__main__":
    unittest.main()
