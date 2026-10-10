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
        """球头刀的半球刀尖与平面只接触一个点，所以足迹半径为 0。"""

        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)

    def test_bull_tool_footprint_is_radius_minus_corner(self) -> None:
        # 圆鼻刀的足迹是刀尖平面那一圈：R - Rc，而不是整个 R。
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius=3.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 3.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 2.0)

    def test_bull_footprint_grows_as_the_corner_shrinks(self) -> None:
        footprints = [
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0,
                 corner_radius=rc).footprint_radius_mm
            for rc in (0.5, 2.0, 4.0)
        ]
        self.assertEqual(footprints, sorted(footprints, reverse=True))
        self.assertAlmostEqual(footprints[0], 4.5, places=6)
        self.assertAlmostEqual(footprints[-1], 1.0, places=6)

    def test_corner_radius_at_or_below_zero_is_rejected(self) -> None:
        for value in (0.0, -1.0):
            with self.subTest(value=value):
                with self.assertRaises(ParameterError):
                    Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0,
                         corner_radius=value)

    def test_bull_corner_radius_must_be_smaller_than_the_radius(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius=5.0)

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

    def test_every_kind_is_selectable(self) -> None:
        disabled = {choice.value: choice.disabled for choice in TOOL_KINDS}
        self.assertFalse(disabled["flat"])
        self.assertFalse(disabled["ball"])
        self.assertFalse(disabled["bull"])

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        self.assertFalse(any(item["disabled"] for item in kind_spec.to_dict()["choices"]))

    def test_corner_radius_only_shows_for_bull_nose(self) -> None:
        spec = tool_parameters().spec("corner_radius_mm")
        self.assertEqual(dict(spec.to_dict()["visible_if"]), {"kind": "bull"})

    def test_bull_nose_can_be_built_from_parameters(self) -> None:
        tool = Tool.from_parameters(tool_parameters().coerce(
            {"kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0,
             "corner_radius_mm": 3.0}
        ))
        self.assertIs(tool.kind, ToolKind.BULL)
        self.assertAlmostEqual(tool.footprint_radius_mm, 2.0)

    def test_describe_exposes_the_geometry(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "flat", "diameter_mm": 10.0, "length_mm": 45.0}
        ).describe()
        self.assertEqual(payload["diameter_mm"], 10.0)
        self.assertEqual(payload["radius_mm"], 5.0)
        self.assertEqual(payload["footprint_radius_mm"], 5.0)
        self.assertEqual(payload["length_mm"], 45.0)
        self.assertIn("kind_label", payload)
        self.assertEqual(payload["corner_radius_mm"], 0.0)

    def test_out_of_range_diameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"diameter_mm": 0.1})


if __name__ == "__main__":
    unittest.main()
