"""Tool geometry: footprint radius, construction from parameters, validation."""

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
        """A ball nose tool only touches the floor with its tip, so its footprint radius is 0."""

        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)

    def test_bull_tool_uses_its_flat_bottom(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 5.0)

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
        self.assertEqual(disabled, {"flat": False, "ball": False, "bull": False})

    def test_a_corner_radius_larger_than_the_tool_is_rejected(self) -> None:
        """A bull nose corner radius cannot exceed the tool radius (the API answers 400 for that)."""

        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, bull_corner_radius_mm=6.0)

    def test_a_ball_nose_touches_the_floor_in_a_point(self) -> None:
        ball = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(ball.corner_radius_mm, 4.0)
        self.assertAlmostEqual(ball.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(ball.wall_clearance_mm(0.0), 0.0)
        # 2 mm deep: the sphere's half-width there is R*sin(60 deg) = 2*sqrt(3)
        self.assertAlmostEqual(ball.wall_clearance_mm(2.0), 2.0 * (3.0 ** 0.5), places=6)
        self.assertAlmostEqual(ball.wall_clearance_mm(4.0), 4.0)
        self.assertAlmostEqual(ball.wall_clearance_mm(99.0), 4.0)

    def test_a_bull_nose_reaches_its_full_radius_at_the_corner_radius(self) -> None:
        bull = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, bull_corner_radius_mm=2.0)
        self.assertAlmostEqual(bull.corner_radius_mm, 2.0)
        self.assertAlmostEqual(bull.footprint_radius_mm, 3.0)
        self.assertAlmostEqual(bull.wall_clearance_mm(0.5), 3.0 + (4.0 - 2.25) ** 0.5, places=6)
        self.assertAlmostEqual(bull.wall_clearance_mm(2.0), 5.0)
        self.assertAlmostEqual(bull.wall_clearance_mm(10.0), 5.0)

    def test_a_flat_mill_reaches_its_radius_at_any_depth(self) -> None:
        flat = Tool(ToolKind.FLAT, diameter_mm=10.0, length_mm=40.0)
        for depth in (0.0, 0.5, 5.0, 50.0):
            with self.subTest(depth=depth):
                self.assertAlmostEqual(flat.wall_clearance_mm(depth), 5.0)

    def test_the_corner_radius_comes_from_the_parameters(self) -> None:
        tool = Tool.from_parameters(
            {"kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0, "corner_radius_mm": 2.5}
        )
        self.assertAlmostEqual(tool.corner_radius_mm, 2.5)
        self.assertAlmostEqual(tool.footprint_radius_mm, 2.5)

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        self.assertFalse(any(choice["disabled"] for choice in kind_spec.to_dict()["choices"]))

    def test_describe_exposes_the_geometry(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "flat", "diameter_mm": 10.0, "length_mm": 45.0}
        ).describe()
        self.assertEqual(payload["diameter_mm"], 10.0)
        self.assertEqual(payload["radius_mm"], 5.0)
        self.assertEqual(payload["footprint_radius_mm"], 5.0)
        self.assertEqual(payload["length_mm"], 45.0)
        self.assertIn("kind_label", payload)

    def test_out_of_range_diameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"diameter_mm": 0.1})


if __name__ == "__main__":
    unittest.main()
