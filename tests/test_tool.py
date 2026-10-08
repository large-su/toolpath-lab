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
        """球头刀将来启用时，足迹半径为 0（只有刀尖接触）。"""

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

    def test_all_tool_kinds_are_selectable(self) -> None:
        disabled = {choice.value: choice.disabled for choice in TOOL_KINDS}
        self.assertFalse(disabled["flat"])
        self.assertFalse(disabled["ball"])
        self.assertFalse(disabled["bull"])

    def test_parameter_choices_are_published_in_the_catalog(self) -> None:
        kind_spec = tool_parameters().spec("kind")
        self.assertEqual(len(kind_spec.choices), 3)
        self.assertFalse(kind_spec.to_dict()["choices"][1]["disabled"])
        self.assertFalse(kind_spec.to_dict()["choices"][2]["disabled"])

    def test_bull_tool_corner_controls_the_footprint(self) -> None:
        """圆鼻刀的名义圆角 Rc 决定底面半径，进而决定刀路偏置量。"""

        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_mm=2.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 2.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)  # 5 - 2

    def test_bull_tool_without_corner_behaves_like_flat(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_mm=0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 0.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 5.0)

    def test_corner_must_be_nonnegative_and_smaller_than_radius(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_mm=5.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_mm=-1.0)

    def test_describe_exposes_the_corner_radius(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "bull", "diameter_mm": 10.0, "length_mm": 45.0, "corner_mm": 2.0}
        ).describe()
        self.assertEqual(payload["corner_radius_mm"], 2.0)
        self.assertEqual(payload["footprint_radius_mm"], 3.0)

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

    def test_stepover_parameter_is_published_and_validated(self) -> None:
        spec = tool_parameters().spec("stepover_mm")
        self.assertEqual(spec.default, 1.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, stepover_mm=0.0)
        with self.assertRaises(ParameterError):
            tool_parameters().coerce({"stepover_mm": -0.5})


class ResidualHeightTests(unittest.TestCase):
    """残留高度：平底刀无残留，球头/圆鼻刀由刀尖几何决定。"""

    def test_flat_tool_leaves_no_residual(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, stepover_mm=1.0)
        self.assertAlmostEqual(tool.residual_height_mm(), 0.0)
        self.assertAlmostEqual(tool.residual_height_mm(5.0), 0.0)

    def test_ball_tool_residual_follows_arc_formula(self) -> None:
        # R=3，行距 s=2：h = 3 - sqrt(9 - 1) ≈ 0.1716
        tool = Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=30.0, stepover_mm=2.0)
        self.assertAlmostEqual(tool.residual_height_mm(), 3.0 - (9.0 - 1.0) ** 0.5, places=4)

    def test_ball_tool_fully_apart_returns_the_radius(self) -> None:
        # 行距 ≥ 直径：相邻刀轨不相交，残留达到半径
        tool = Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=30.0, stepover_mm=6.0)
        self.assertAlmostEqual(tool.residual_height_mm(), 3.0)

    def test_bull_tool_flat_bottom_leaves_no_residual(self) -> None:
        # D6 Rc1：平底半径 2，行距 4（=2*base）以内无残留
        tool = Tool(ToolKind.BULL, diameter_mm=6.0, length_mm=30.0,
                    corner_mm=1.0, stepover_mm=4.0)
        self.assertAlmostEqual(tool.residual_height_mm(), 0.0)

    def test_bull_tool_residual_comes_from_the_corner(self) -> None:
        # D6 Rc1：行距 5 时 half=(5-4)/2=0.5，h = 1 - sqrt(1-0.25) ≈ 0.1340
        tool = Tool(ToolKind.BULL, diameter_mm=6.0, length_mm=30.0,
                    corner_mm=1.0, stepover_mm=5.0)
        self.assertAlmostEqual(tool.residual_height_mm(), 1.0 - 0.75 ** 0.5, places=4)

    def test_bull_tool_fully_apart_returns_the_corner(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=6.0, length_mm=30.0,
                    corner_mm=1.0, stepover_mm=6.0)
        self.assertAlmostEqual(tool.residual_height_mm(), 1.0)

    def test_recommended_stepover_inverts_the_formula(self) -> None:
        # 球头刀 R3：目标残留 0.02 → 行距 = 2*sqrt(R^2-(R-h)^2)
        tool = Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=30.0)
        recommended = tool.recommended_stepover_mm(0.02)
        self.assertAlmostEqual(recommended, 2.0 * (9.0 - (3.0 - 0.02) ** 2) ** 0.5, places=4)
        # 用推荐行距算回去，残留应回到 0.02
        self.assertAlmostEqual(tool.residual_height_mm(recommended), 0.02, places=4)

    def test_recommended_stepover_for_bull_uses_flat_bottom_first(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=6.0, length_mm=30.0, corner_mm=1.0)
        # 目标残留 0 → 最多只用到平底覆盖：2*base = 4
        self.assertAlmostEqual(tool.recommended_stepover_mm(0.0), 4.0)
        # 圆鼻刀目标残留 0.02 时行距大于平底覆盖
        self.assertGreater(tool.recommended_stepover_mm(0.02), 4.0)

    def test_describe_exposes_residual_metrics(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "ball", "diameter_mm": 6.0, "length_mm": 30.0, "stepover_mm": 2.0}
        ).describe()
        self.assertAlmostEqual(payload["residual_height_mm"], 3.0 - 8.0 ** 0.5, places=4)
        self.assertGreater(payload["recommended_stepover_mm"], 0.0)
        self.assertEqual(payload["stepover_mm"], 2.0)


if __name__ == "__main__":
    unittest.main()
class CuttingParameterTests(unittest.TestCase):
    """切削参数推荐：按材料查表 + 转速/进给公式。"""

    def test_spindle_speed_uses_the_standard_formula(self) -> None:
        # D6 铝合金 Vc=250：n = 1000*250/(pi*6) ≈ 13263 rpm
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, material="aluminum")
        expected = round(1000.0 * 250.0 / (3.141592653589793 * 6.0), 0)
        self.assertEqual(tool.recommended_spindle_speed_rpm(), expected)

    def test_feed_multiplies_speed_by_flutes_and_feed_per_tooth(self) -> None:
        # D6 铝 2 刃、fz=0.05：F = n*2*0.05
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, material="aluminum")
        n = tool.recommended_spindle_speed_rpm()
        self.assertEqual(tool.recommended_feed_mm_per_min(), round(n * 2 * 0.05, 0))

    def test_harder_material_gets_slower_speed(self) -> None:
        aluminum = Tool(ToolKind.FLAT, diameter_mm=10.0, length_mm=30.0, material="aluminum")
        titanium = Tool(ToolKind.FLAT, diameter_mm=10.0, length_mm=30.0, material="titanium")
        self.assertGreater(
            aluminum.recommended_spindle_speed_rpm(),
            titanium.recommended_spindle_speed_rpm(),
        )

    def test_larger_diameter_gets_lower_speed(self) -> None:
        small = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, material="steel")
        large = Tool(ToolKind.FLAT, diameter_mm=20.0, length_mm=30.0, material="steel")
        self.assertGreater(small.recommended_spindle_speed_rpm(), large.recommended_spindle_speed_rpm())

    def test_flute_count_depends_on_kind_and_diameter(self) -> None:
        self.assertEqual(
            Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=30.0).flute_count, 2
        )
        self.assertEqual(
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0).flute_count, 2
        )
        self.assertEqual(
            Tool(ToolKind.FLAT, diameter_mm=10.0, length_mm=30.0).flute_count, 4
        )

    def test_unknown_material_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, material="wood")

    def test_describe_exposes_cutting_parameters(self) -> None:
        payload = Tool.from_parameters(
            {"kind": "flat", "diameter_mm": 6.0, "length_mm": 30.0, "material": "steel"}
        ).describe()
        self.assertEqual(payload["material"], "steel")
        self.assertEqual(payload["material_label"], "碳钢")
        self.assertIn("recommended_spindle_speed_rpm", payload)
        self.assertIn("recommended_feed_mm_per_min", payload)
        self.assertIn("cutting_speed_m_per_min", payload)
        self.assertIn("feed_per_tooth_mm", payload)
