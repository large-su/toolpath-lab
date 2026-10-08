"""刀具几何：足迹半径、球头刀半球轮廓、参数构造与校验。"""

from __future__ import annotations

import unittest
from math import sqrt

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.tool import TOOL_KINDS, Tool, ToolKind, tool_parameters


class ToolGeometryTests(unittest.TestCase):
    def test_flat_tool_footprint_is_the_radius(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        self.assertAlmostEqual(tool.radius_mm, 3.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.tip_height_mm, 0.0)

    def test_ball_tool_touches_with_its_tip(self) -> None:
        """球头刀只有一个刀尖点接触加工面，所以足迹半径为 0。"""

        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)
        self.assertAlmostEqual(tool.tip_height_mm, 4.0)

    def test_bull_tool_uses_its_flat_bottom(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 5.0)

    def test_invalid_geometry_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=0.0, length_mm=30.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=-1.0)

    def test_ball_shorter_than_its_hemisphere_is_rejected(self) -> None:
        # 半球本身就占掉半径那么高，刀长比它还短就装不下。
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BALL, diameter_mm=20.0, length_mm=5.0)


class ToolProfileTests(unittest.TestCase):
    """三维显示用的半剖回转轮廓。"""

    def test_flat_profile_is_a_capped_cylinder(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        cutting, shank = tool.segments()
        self.assertEqual(cutting.name, "cutting")
        # 刀底从轴心走到半径 3，再竖直向上：一个带底的圆柱。
        self.assertEqual(cutting.profile[0], (0.0, 0.0))
        self.assertEqual(cutting.profile[1], (3.0, 0.0))
        self.assertAlmostEqual(cutting.profile[-1][0], 3.0)
        self.assertAlmostEqual(cutting.profile[-1][1], tool.cutting_length_mm)
        self.assertEqual(shank.name, "shank")
        self.assertAlmostEqual(shank.profile[-1][1], 30.0)

    def test_ball_profile_is_a_hemisphere_plus_a_cylinder(self) -> None:
        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        cutting = tool.segments()[0]

        # 刀尖在 (0, 0)，赤道在 (R, R)：半球与圆柱共用这个端面。
        self.assertAlmostEqual(cutting.profile[0][0], 0.0)
        self.assertAlmostEqual(cutting.profile[0][1], 0.0)
        equator = [point for point in cutting.profile if abs(point[1] - 4.0) < 1e-9]
        self.assertTrue(equator)
        self.assertAlmostEqual(equator[0][0], 4.0)
        # 赤道以下每一个点都必须落在球心 (0, R)、半径 R 的球面上（轮廓取 6 位小数）。
        for radius, height in cutting.profile:
            if height <= 4.0 + 1e-9:
                self.assertAlmostEqual(radius**2 + (height - 4.0) ** 2, 16.0, delta=1e-5)
        # 赤道以上是圆柱：半径恒为 R，高度一直到切削段顶端。
        self.assertAlmostEqual(cutting.profile[-1][0], 4.0)
        self.assertAlmostEqual(cutting.profile[-1][1], tool.cutting_length_mm)
        self.assertGreaterEqual(tool.cutting_length_mm, 4.0)

    def test_the_shank_is_skipped_when_the_cutting_section_fills_the_tool(self) -> None:
        # D100 × L50 的球头刀：半球已经占满整把刀，没有刀柄可画。
        self.assertEqual(len(Tool(ToolKind.BALL, diameter_mm=100.0, length_mm=50.0).segments()), 1)

    def test_describe_publishes_the_display_profile(self) -> None:
        payload = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0).describe()
        self.assertEqual(payload["kind_label"], "球头刀 Ball nose")
        self.assertAlmostEqual(payload["tip_height_mm"], 4.0)
        self.assertEqual([segment["name"] for segment in payload["segments"]],
                         ["cutting", "shank"])
        self.assertEqual(payload["segments"][0]["profile_mm"][0], [0.0, 0.0])


class ToolResidualHeightTests(unittest.TestCase):
    """相邻两刀之间留下的球面弓高。"""

    def test_flat_tool_leaves_no_residual_on_a_plane(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        self.assertEqual(tool.residual_height_mm(4.0), 0.0)

    def test_ball_residual_height_follows_the_sphere_formula(self) -> None:
        tool = Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0)
        self.assertAlmostEqual(tool.residual_height_mm(4.0), 4.0 - sqrt(12.0), places=9)
        self.assertAlmostEqual(tool.residual_height_mm(8.0), 4.0, places=9)
        # 切宽超过直径时不会算出负数（残留最多就是一整个半径）。
        self.assertAlmostEqual(tool.residual_height_mm(30.0), 4.0, places=9)


class ToolParameterTests(unittest.TestCase):
    def test_defaults_are_a_flat_end_mill(self) -> None:
        tool = Tool.from_parameters(tool_parameters().coerce({}))
        self.assertIs(tool.kind, ToolKind.FLAT)
        self.assertEqual(tool.diameter_mm, 6.0)
        self.assertEqual(tool.length_mm, 30.0)

    def test_flat_and_ball_are_selectable_but_bull_is_not(self) -> None:
        disabled = {choice.value: choice.disabled for choice in TOOL_KINDS}
        self.assertFalse(disabled["flat"])
        self.assertFalse(disabled["ball"])
        self.assertTrue(disabled["bull"])

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        self.assertFalse(kind_spec.to_dict()["choices"][1]["disabled"])

    def test_ball_parameters_build_a_ball_tool(self) -> None:
        tool = Tool.from_parameters(
            tool_parameters().coerce({"kind": "ball", "diameter_mm": 8.0, "length_mm": 40.0})
        )
        self.assertIs(tool.kind, ToolKind.BALL)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)

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
