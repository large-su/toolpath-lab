"""Holder collision: the tool above the flutes against the pocket walls."""

from __future__ import annotations

import unittest

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind, tool_parameters
from toolpath_lab.planning import run_plan
from toolpath_lab.planning.collision import check_holder, holder_note, holder_warnings

SQUARE = build_region("square", {})


def _tool(**overrides) -> Tool:
    values = {"kind": ToolKind.FLAT, "diameter_mm": 6.0, "length_mm": 30.0}
    values.update(overrides)
    return Tool(**values)


def _plan(tool: Tool, parameters: dict | None = None, region=None):
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0, **(parameters or {})}
    return run_plan(
        planner_id="raster", tool=tool, region=region or SQUARE, parameters=options
    )


class GeometryTests(unittest.TestCase):
    """The head/shank geometry the check and the 3D view share."""

    def test_the_defaults_are_the_geometry_the_view_has_always_drawn(self) -> None:
        tool = _tool()  # D6 L30
        self.assertAlmostEqual(tool.flute_mm, min(0.65 * 30.0, 6 * 3.0), places=6)  # 18 mm
        self.assertAlmostEqual(tool.flute_mm, 18.0, places=6)
        self.assertAlmostEqual(tool.shank_radius_mm, 1.25 * 3.0, places=6)

    def test_the_parameters_win_when_they_are_given(self) -> None:
        tool = _tool(flute_length_mm=5.0, shank_diameter_mm=12.0)
        self.assertAlmostEqual(tool.flute_mm, 5.0, places=6)
        self.assertAlmostEqual(tool.shank_radius_mm, 6.0, places=6)

    def test_a_necked_tool_is_allowed(self) -> None:
        # A shank thinner than the cutter (a necked tool) can only make the check more forgiving.
        tool = _tool(shank_diameter_mm=3.0)
        self.assertAlmostEqual(tool.shank_radius_mm, 1.5, places=6)

    def test_a_flute_longer_than_the_tool_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            _tool(flute_length_mm=40.0)

    def test_a_negative_flute_or_shank_is_rejected(self) -> None:
        for values in ({"flute_length_mm": -1.0}, {"shank_diameter_mm": -1.0}):
            with self.subTest(values=values):
                with self.assertRaises(ParameterError):
                    _tool(**values)

    def test_the_catalogue_publishes_both_parameters(self) -> None:
        keys = [item.key for item in tool_parameters()]
        self.assertIn("flute_length_mm", keys)
        self.assertIn("shank_diameter_mm", keys)
        self.assertEqual(tool_parameters().spec("flute_length_mm").default, 0.0)
        self.assertEqual(tool_parameters().spec("shank_diameter_mm").default, 0.0)

    def test_describe_exposes_the_resolved_geometry(self) -> None:
        payload = _tool(flute_length_mm=5.0, shank_diameter_mm=12.0).describe()
        self.assertEqual(payload["flute_mm"], 5.0)
        self.assertEqual(payload["shank_radius_mm"], 6.0)


class CheckTests(unittest.TestCase):
    """The two ways the tool above the flutes can be wrong."""

    def test_a_default_tool_never_puts_its_shank_in_the_pocket(self) -> None:
        # 18 mm of flutes over a 4 mm cut: nothing above the flutes ever enters the pocket.
        outcome = _plan(_tool(), {"depth_mm": 4.0, "stepdown_mm": 2.0})
        check = check_holder(outcome.toolpath, SQUARE, _tool())
        self.assertEqual(check.engaged_points, 0)
        self.assertIsNone(check.clearance_mm)
        self.assertFalse(check.collides)
        self.assertEqual(holder_warnings(check), [])
        self.assertIsNone(holder_note(check))

    def test_a_straight_shank_clears_the_pocket_the_planner_inset(self) -> None:
        """Every strategy leaves its outermost pass exactly at the cutter's own clearance."""

        tool = _tool(flute_length_mm=2.0, shank_diameter_mm=6.0)  # shank R3 = the flat cutter's R
        outcome = _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0})
        check = check_holder(outcome.toolpath, SQUARE, tool)
        self.assertGreater(check.engaged_points, 0)
        # A D6 flat tool is inset 3 mm from the 80 mm square, so the closest approach is exactly 3 mm.
        self.assertAlmostEqual(check.clearance_mm, 3.0, places=6)
        self.assertAlmostEqual(check.shortfall_mm, 0.0, places=6)
        self.assertFalse(check.collides)
        self.assertIn("余量 0.00 mm", holder_note(check))

    def test_a_fat_shank_hits_the_wall_by_an_exact_amount(self) -> None:
        tool = _tool(flute_length_mm=2.0, shank_diameter_mm=12.0)  # shank R6 in a 3 mm inset
        outcome = _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0})
        check = check_holder(outcome.toolpath, SQUARE, tool)
        self.assertAlmostEqual(check.clearance_mm, 3.0, places=6)
        self.assertAlmostEqual(check.shortfall_mm, 3.0, places=6)
        self.assertTrue(check.collides)
        message = holder_warnings(check)[0]
        self.assertIn("刀柄碰撞", message)
        self.assertIn("差 3.00 mm", message)
        # A collision is a warning, not a note: the warning already carries the message.
        self.assertIsNone(holder_note(check))

    def test_the_check_travels_with_the_plan(self) -> None:
        tool = _tool(flute_length_mm=2.0, shank_diameter_mm=12.0)
        outcome = _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0})
        self.assertIsNotNone(outcome.holder)
        self.assertTrue(outcome.holder.collides)
        self.assertTrue(any("刀柄碰撞" in warning for warning in outcome.warnings))

    def test_a_short_tool_is_reported_as_a_length_problem(self) -> None:
        # L4 with 5 mm of cut: the top of the tool (where the holder starts) is 1 mm under the face.
        # A shank of R3 exactly matches the inset, so this is only about the length.
        tool = _tool(length_mm=4.0, flute_length_mm=2.0, shank_diameter_mm=6.0)
        outcome = _plan(tool, {"depth_mm": 5.0, "stepdown_mm": 2.0})
        check = check_holder(outcome.toolpath, SQUARE, tool)
        self.assertAlmostEqual(check.deepest_cut_mm, 5.0, places=6)
        self.assertAlmostEqual(check.length_shortfall_mm, 1.0, places=6)
        self.assertAlmostEqual(check.shortfall_mm, 0.0, places=6)
        message = holder_warnings(check)[0]
        self.assertIn("刀具长度不够", message)
        self.assertIn("至少 5.00 mm", message)

    def test_entries_are_not_checked_but_links_are(self) -> None:
        """A ramp entry may leave the region on purpose; a link runs at cutting depth and counts."""

        tool = _tool(flute_length_mm=2.0, shank_diameter_mm=12.0)
        outcome = _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0, "entry_mode": "ramp",
                               "ramp_angle_deg": 10.0})
        check = check_holder(outcome.toolpath, SQUARE, tool)
        # The ramp walks ~8 mm outside the square; were it checked, the clearance would collapse to
        # roughly zero. Skipping entries keeps the number about the pocket walls.
        self.assertAlmostEqual(check.clearance_mm, 3.0, places=6)

    def test_a_deeper_inset_makes_a_fat_shank_fit(self) -> None:
        """The comparison is against the cutter's own clearance, which a stock allowance widens."""

        tool = _tool(flute_length_mm=2.0, shank_diameter_mm=12.0)
        tight = check_holder(
            _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0}).toolpath, SQUARE, tool
        )
        loose = check_holder(
            _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0, "stock_allowance_mm": 4.0}).toolpath,
            SQUARE,
            tool,
        )
        self.assertAlmostEqual(tight.clearance_mm, 3.0, places=6)
        self.assertAlmostEqual(loose.clearance_mm, 7.0, places=6)  # 3 mm tool radius + 4 mm allowance
        self.assertLess(loose.shortfall_mm, tight.shortfall_mm)

    def test_a_plan_that_never_goes_below_the_top_face_is_not_checked(self) -> None:
        outcome = _plan(_tool(flute_length_mm=2.0), {})  # no depth: a single pass at Z = 0
        check = check_holder(outcome.toolpath, SQUARE, _tool(flute_length_mm=2.0))
        self.assertAlmostEqual(check.deepest_cut_mm, 0.0, places=6)
        self.assertEqual(check.engaged_points, 0)

    def test_a_tapered_flank_is_already_accounted_for_by_the_planner(self) -> None:
        """The inset grows with the taper, so the flank itself never rubs -- the shank still can."""

        from math import radians, tan

        # A necked, tapered tool: the shank (R0.5) is narrower than the flank, so the widest thing in
        # the pocket is the flank itself -- and the planner has already made room for it.
        tool = _tool(flute_length_mm=2.0, taper_angle_deg=15.0, shank_diameter_mm=1.0)
        outcome = _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0})
        check = check_holder(outcome.toolpath, SQUARE, tool)
        inset = 3.0 + 4.0 * tan(radians(15.0))  # D6 flat with a 15 deg flank, 4 mm deep
        self.assertAlmostEqual(check.clearance_mm, inset, places=3)
        self.assertAlmostEqual(check.widest_radius_mm, 3.0 + 2.0 * tan(radians(15.0)), places=3)
        self.assertAlmostEqual(check.margin_mm, inset - check.widest_radius_mm, places=3)
        self.assertFalse(check.collides)

    def test_a_taper_with_long_flutes_leaves_nothing_to_check(self) -> None:
        """With 18 mm of flutes over a 4 mm cut, the shank never enters the pocket at all."""

        tool = _tool(flute_length_mm=18.0, taper_angle_deg=15.0)
        check = check_holder(_plan(tool, {"depth_mm": 4.0}).toolpath, SQUARE, tool)
        self.assertEqual(check.engaged_points, 0)
        self.assertIsNone(check.margin_mm)
        self.assertFalse(check.collides)

    def test_a_fat_shank_still_collides_under_a_taper(self) -> None:
        from math import radians, tan

        tool = _tool(flute_length_mm=2.0, taper_angle_deg=15.0, shank_diameter_mm=16.0)
        outcome = _plan(tool, {"depth_mm": 4.0, "stepdown_mm": 2.0})
        check = check_holder(outcome.toolpath, SQUARE, tool)
        inset = 3.0 + 4.0 * tan(radians(15.0))
        self.assertAlmostEqual(check.widest_radius_mm, 8.0, places=6)  # the shank is the widest part
        self.assertAlmostEqual(check.clearance_mm, inset, places=3)
        self.assertAlmostEqual(check.shortfall_mm, 8.0 - inset, places=3)
        self.assertTrue(check.collides)

    def test_a_steep_taper_can_make_a_pocket_infeasible(self) -> None:
        """A big taper over a deep cut is a big tool: the offset geometry has to give up somewhere."""

        from toolpath_lab.core.errors import PlanningError

        tool = _tool(taper_angle_deg=30.0)
        with self.assertRaises(PlanningError):
            _plan(tool, {"depth_mm": 4.0}, region=build_region("square", {"side_mm": 10.0}))

    def test_the_payload_is_json_friendly(self) -> None:
        tool = _tool(flute_length_mm=2.0, shank_diameter_mm=12.0)
        payload = check_holder(_plan(tool, {"depth_mm": 4.0}).toolpath, SQUARE, tool).describe()
        self.assertEqual(sorted(payload), sorted([
            "flute_mm", "shank_radius_mm", "widest_radius_mm", "tool_length_mm", "deepest_cut_mm",
            "engaged_points", "clearance_mm", "margin_mm", "shortfall_mm", "length_shortfall_mm",
            "collides",
        ]))
        self.assertTrue(payload["collides"])


if __name__ == "__main__":
    unittest.main()
