"""组合策略必须同时有自适应间距与真实变化的刀轴，并保留已有策略。"""
from __future__ import annotations
import unittest
import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import axis_angle_deg
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_polygon_region, build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import get_planner, run_plan
from toolpath_lab.simulation import build_timeline


def plan(planner="five_axis_adaptive", surface="composite", params=None, rough=False, kind=ToolKind.BALL, region=None):
    return run_plan(planner_id=planner, tool=Tool(kind, 6, 30),
                    region=region or build_region("square", {"side_mm": 40}),
                    surface=build_surface(surface, {}), parameters=params,
                    roughing={"enabled": rough})


class FiveAxisAdaptiveTests(unittest.TestCase):
    def test_combines_parameter_groups_without_fixed_stepover_or_duplicates(self):
        keys = [p.key for p in get_planner("five_axis_adaptive").parameters]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("stepover_mm", keys)
        for key in ["target_scallop_mm", "min_stepover_mm", "max_stepover_mm", "mode",
                    "lead_deg", "side_tilt_deg", "smooth_orientation", "max_angular_speed_deg_s"]:
            self.assertIn(key, keys)

    def test_original_adaptive_positions_and_spacing_are_preserved(self):
        base = plan("adaptive_scallop").toolpath
        combined = plan().toolpath
        self.assertEqual(combined.planner, "five_axis_adaptive")
        self.assertTrue(combined.is_oriented)
        self.assertEqual(combined.metadata["adaptive"], base.metadata["adaptive"])
        cuts = [m for m in combined.moves if m.kind is MoveKind.CUT]
        for a, b in zip([m for m in base.moves if m.kind is MoveKind.CUT], cuts):
            np.testing.assert_allclose(a.points, b.points, atol=1e-12)
        self.assertGreater(np.ptp(cuts[0].tool_axes, axis=0).max(), 0.1)
        profile = combined.metadata["adaptive"]["stepover_profile_mm"]
        self.assertEqual(len(profile), len(cuts))
        self.assertGreater(np.ptp(profile), 0.05)

    def test_flat_nominal_spacing_follows_ball_formula_and_pose_is_constant_per_cut(self):
        path = plan(surface="flat", params={"target_scallop_mm": 0.1}).toolpath
        expected = 2 * np.sqrt(2 * 3 * 0.1 - 0.1 ** 2)
        self.assertAlmostEqual(path.metadata["adaptive"]["nominal_stepover_mm"], expected)
        for cut in [m for m in path.moves if m.kind is MoveKind.CUT]:
            np.testing.assert_allclose(cut.tool_axes, np.repeat(cut.tool_axes[:1], len(cut.points), axis=0))

    def test_tighter_target_adds_passes_and_original_vertical_strategy_stays_vertical(self):
        coarse = plan(params={"target_scallop_mm": 0.3, "min_stepover_mm": 0.2}).toolpath
        fine = plan(params={"target_scallop_mm": 0.05, "min_stepover_mm": 0.2}).toolpath
        self.assertGreater(fine.pass_count, coarse.pass_count)
        self.assertFalse(plan("adaptive_scallop").toolpath.is_oriented)

    def test_smooth_off_keeps_variable_axes_safe_rapid_connectors_and_nominal_positions(self):
        smooth = plan().toolpath
        raw = plan(params={"smooth_orientation": False}).toolpath
        self.assertNotIn("orientation_smoothing", raw.metadata)
        self.assertNotIn(MoveKind.LINK, [m.kind for m in raw.moves])
        for a, b in zip([m for m in smooth.moves if m.kind is MoveKind.CUT],
                        [m for m in raw.moves if m.kind is MoveKind.CUT]):
            np.testing.assert_array_equal(a.points, b.points)
            self.assertTrue(b.preserve_vertices)
        self.assertTrue(any(np.ptp(m.tool_axes, axis=0).max() > 0.1 for m in raw.moves if m.kind is MoveKind.CUT))

    def test_one_way_and_zigzag_directions_are_retained(self):
        for mode in ["one_way", "zigzag"]:
            cuts = [m for m in plan(surface="freeform", params={"mode": mode}).toolpath.moves if m.kind is MoveKind.CUT]
            for index, cut in enumerate(cuts):
                sign = cut.points[-1, 0] - cut.points[0, 0] > 0
                self.assertEqual(sign, True if mode == "one_way" else index % 2 == 0)

    def test_smoothing_and_roughing_metadata_and_axes_are_kept(self):
        plain = plan(params={"max_angular_speed_deg_s": 10}).toolpath
        path = plan(params={"max_angular_speed_deg_s": 10}, rough=True).toolpath
        self.assertEqual(path.metadata["adaptive"], plain.metadata["adaptive"])
        self.assertEqual(path.metadata["orientation_smoothing"], plain.metadata["orientation_smoothing"])
        start = path.metadata["roughing"]["finish_start_move_index"]
        self.assertEqual(path.moves[start - 1].angular_speed_deg_s, 10)
        for a, b in zip(path.moves[start:], plain.moves):
            np.testing.assert_array_equal(a.points, b.points)
            np.testing.assert_array_equal(a.tool_axes, b.tool_axes)
        for a, b in zip(path.moves[:-1], path.moves[1:]):
            np.testing.assert_allclose(a.points[-1], b.points[0], atol=1e-10)
            np.testing.assert_allclose(a.tool_axes[-1], b.tool_axes[0], atol=1e-10)

    def test_timeline_keeps_boundaries_and_angular_speed_constraint(self):
        path = plan(params={"feed_mm_per_min": 2000, "max_angular_speed_deg_s": 10}).toolpath
        timeline = build_timeline(path, max_samples=2)
        self.assertAlmostEqual(timeline.duration_s, path.estimated_time_s, places=8)
        for i, dt in enumerate(np.diff(timeline.times_s)):
            if dt > 1e-8:
                self.assertLessEqual(axis_angle_deg(timeline.tool_axes[i], timeline.tool_axes[i + 1]) / dt, 10.0001)
        self.assertGreater(timeline.sample_count, path.point_count - len(path.moves))

    def test_non_ball_tool_is_rejected_with_actionable_instruction(self):
        for kind in [ToolKind.FLAT, ToolKind.BULL]:
            with self.assertRaisesRegex(PlanningError, "球头刀"):
                plan(kind=kind)

    def test_minimum_above_nominal_warns_instead_of_claiming_target_is_met(self):
        result = plan(surface="flat", params={"target_scallop_mm": 0.02, "min_stepover_mm": 2})
        self.assertTrue(any("最小步距大于" in warning for warning in result.warnings))
        self.assertTrue(result.toolpath.metadata["five_axis_adaptive"]["estimate_only"])

    def test_gcode_exports_variable_ab_with_both_stage_notes(self):
        code = toolpath_to_gcode(plan(rough=True).toolpath)
        self.assertIn("粗加工", code)
        self.assertIn("五轴", code)
        self.assertIn("simulation-only", code)
        self.assertRegex(code, r"G1 .* A-?[\d.]+ B[\d.]+")

    def test_concave_projection_profiles_match_each_split_scanline(self):
        boundary = [[-20, -20], [20, -20], [20, 20], [8, 20], [8, 0], [-8, 0], [-8, 20], [-20, 20]]
        path = plan(region=build_polygon_region(boundary)).toolpath
        cuts = [m for m in path.moves if m.kind is MoveKind.CUT]
        profile = path.metadata["adaptive"]["stepover_profile_mm"]
        by_level = {}
        for cut, step in zip(cuts, profile):
            key = round(float(cut.points[0, 1]), 8)
            if key in by_level:
                self.assertAlmostEqual(by_level[key], step)
            by_level[key] = step
        self.assertLess(len(by_level), len(cuts))


if __name__ == "__main__":
    unittest.main()
