"""Adaptive contouring: tighten the stepover until the coverage target is met, or say honestly why not."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import (
    PLANNERS,
    PlanningContext,
    measure_coverage,
    planner_catalog,
    run_plan,
)
from toolpath_lab.planning.adaptive import AdaptiveContourPlanner


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _run(shape: str = "square", *, tool: Tool | None = None, **parameters):
    region = build_region(shape, parameters.pop("region_parameters", {}))
    return region, run_plan(
        planner_id="adaptive_contour",
        tool=tool or _tool(),
        region=region,
        parameters=parameters,
    )


class RegistrationTests(unittest.TestCase):
    def test_the_adaptive_planner_is_registered_last(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "contour", "adaptive_contour"])

    def test_the_catalog_lists_the_adaptive_parameters(self) -> None:
        entry = {item["id"]: item for item in planner_catalog()}["adaptive_contour"]
        self.assertEqual(entry["label"], "自适应环切")
        keys = [item["key"] for item in entry["parameters"]]
        # Every contour parameter is present, plus the adaptive ones
        for inherited in ("stepover_mm", "sample_step_mm", "ring_direction", "feed_mm_per_min",
                          "safe_height_mm", "rapid_feed_mm_per_min", "corner_angle_deg",
                          "corner_feed_ratio"):
            self.assertIn(inherited, keys)
        self.assertEqual(
            keys[-5:], ["coverage_target", "max_time_ratio", "max_rounds", "stepover_factor",
                        "min_stepover_mm"]
        )
        by_key = {item["key"]: item for item in entry["parameters"]}
        self.assertEqual(by_key["coverage_target"]["default"], 99.5)
        self.assertEqual(by_key["max_time_ratio"]["default"], 2.0)
        self.assertEqual(by_key["max_rounds"]["default"], 3)
        self.assertEqual(by_key["stepover_factor"]["default"], 0.7)
        self.assertEqual(by_key["min_stepover_mm"]["default"], 1.0)


class TighteningTests(unittest.TestCase):
    def test_it_tightens_until_the_target_is_met(self) -> None:
        region, outcome = _run()
        toolpath = outcome.toolpath
        # 80 square, D6: 99.31% at a 6 mm stepover, 99.86% after one round down to 4.2 mm
        self.assertAlmostEqual(toolpath.statistics()["pass_count"], 9)
        coverage = measure_coverage(toolpath, region, _tool())
        self.assertGreaterEqual(coverage.ratio, 0.995)
        self.assertIn("切宽 6 → 4.2 mm", toolpath.notes[0])
        self.assertIn("已达标", toolpath.notes[0])
        self.assertEqual(outcome.warnings, ())

    def test_a_coarser_target_needs_no_extra_round(self) -> None:
        region, outcome = _run(coverage_target=99.0)
        self.assertIn("共算 1 次", outcome.toolpath.notes[0])
        self.assertAlmostEqual(
            measure_coverage(outcome.toolpath, region, _tool()).ratio, 0.9931, delta=0.001
        )

    def test_an_already_good_stepover_is_left_alone(self) -> None:
        region, outcome = _run(stepover_mm=2.0)
        self.assertIn("共算 1 次", outcome.toolpath.notes[0])
        self.assertIn("切宽 2 → 2 mm", outcome.toolpath.notes[0])
        plain = run_plan(planner_id="contour", tool=_tool(), region=region,
                         parameters={"stepover_mm": 2.0}).toolpath
        self.assertEqual(outcome.toolpath.pass_count, plain.pass_count)

    def test_it_stops_as_soon_as_a_round_does_not_improve(self) -> None:
        """The residue on a triangle is a geometric limit: tightening cannot help, so it must not keep computing."""

        region, outcome = _run("triangle")
        toolpath = outcome.toolpath
        # 6 -> 4.2 improves clearly, 2.94 is identical to 4.2, so it stops at 4.2 (the cheaper round)
        self.assertIn("切宽 6 → 4.2 mm", toolpath.notes[0])
        self.assertIn("共算 3 次", toolpath.notes[0])
        self.assertIn("未达标", toolpath.notes[0])
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn("不再提升", outcome.warnings[0])
        self.assertIn("比刀具还窄", outcome.warnings[0])
        self.assertAlmostEqual(
            measure_coverage(toolpath, region, _tool()).ratio, 0.9919, delta=0.001
        )

    def test_it_keeps_the_best_round_even_though_coverage_is_not_monotonic(self) -> None:
        """Coverage is not monotonic in the stepover (the dumbbell is worse at 1 mm), so the best round is kept."""

        region = build_region("dumbbell", {})
        outcome = run_plan(
            planner_id="adaptive_contour", tool=_tool(), region=region,
            parameters={"stepover_mm": 2.0, "coverage_target": 99.9, "stepover_factor": 0.5},
        )
        toolpath = outcome.toolpath
        reported = measure_coverage(toolpath, region, _tool()).ratio
        smallest = run_plan(planner_id="contour", tool=_tool(), region=region,
                            parameters={"stepover_mm": 1.0}).toolpath
        self.assertGreater(reported, measure_coverage(smallest, region, _tool()).ratio)
        # Every round's coverage is recorded in the notes, so it can be traced back
        self.assertIn("各轮实测", toolpath.notes[1])

    def test_the_round_count_is_bounded(self) -> None:
        _, outcome = _run(max_rounds=1)
        self.assertIn("共算 2 次", outcome.toolpath.notes[0])

    def test_no_round_at_all_when_rounds_is_zero(self) -> None:
        _, outcome = _run(max_rounds=0)
        self.assertIn("共算 1 次", outcome.toolpath.notes[0])
        self.assertAlmostEqual(outcome.toolpath.statistics()["pass_count"], 7)

    def test_the_floor_stops_the_tightening(self) -> None:
        _, outcome = _run(min_stepover_mm=5.0)
        # Floor 5 mm: 5 is tried first (6 x 0.7 = 4.2 is raised to the floor), tightening further is pointless
        self.assertIn("各轮实测：6 mm", outcome.toolpath.notes[1])
        self.assertIn("5 mm", outcome.toolpath.notes[1])
        self.assertIn("共算 2 次", outcome.toolpath.notes[0])

    def test_the_notes_keep_the_contour_notes(self) -> None:
        _, outcome = _run()
        notes = outcome.toolpath.notes
        self.assertIn("自适应环切", notes[0])
        self.assertIn("各轮实测", notes[1])
        self.assertTrue(any("环切：共" in note for note in notes[2:]))

    def test_the_curve_reports_coverage_and_cost_per_round(self) -> None:
        """Value for money curve: every round reports stepover, coverage, ring count, cutting length and time."""

        _, outcome = _run()
        summary, curve = outcome.toolpath.notes[0], outcome.toolpath.notes[1]
        self.assertIn("工时 111.6 → 149.9 s", summary)
        self.assertIn("首轮的 1.34 倍", summary)
        self.assertIn("6 mm → 99.31%（7 环，1064 mm，111.6 s）", curve)
        self.assertIn("4.2 mm → 99.86%（9 环，1450 mm，149.9 s）", curve)

    def test_the_result_is_labelled_as_the_adaptive_planner(self) -> None:
        payload = _run()[1].toolpath.to_payload()
        self.assertEqual(payload["planner"], "adaptive_contour")
        self.assertEqual(payload["planner_label"], "自适应环切")


class TimeBudgetTests(unittest.TestCase):
    """Time limit: tightening the stepover means walking more rings, so a round that costs too much is not adopted."""

    def test_an_expensive_round_is_rejected(self) -> None:
        # 80 square: 6 mm -> 111.6 s, 4.2 mm -> 149.9 s (1.34x). With a 1.2x limit the latter is out.
        region, outcome = _run(coverage_target=100.0, max_time_ratio=1.2)
        toolpath = outcome.toolpath
        self.assertAlmostEqual(toolpath.statistics()["pass_count"], 7)  # 保留了首轮
        self.assertIn("切宽 6 → 6 mm", toolpath.notes[0])
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn("工时上限", outcome.warnings[0])
        self.assertIn("149.9 s", outcome.warnings[0])
        self.assertIn("调大", outcome.warnings[0])
        # That round was still computed and recorded on the curve, it was just not adopted
        self.assertIn("4.2 mm → 99.86%", toolpath.notes[1])
        self.assertAlmostEqual(
            measure_coverage(toolpath, region, _tool()).ratio, 0.9931, delta=0.001
        )

    def test_a_generous_budget_lets_it_tighten(self) -> None:
        region, outcome = _run(coverage_target=100.0, max_time_ratio=2.0)
        toolpath = outcome.toolpath
        # With a 2x limit both 4.2 mm (1.34x) and 2.94 mm (1.84x) fit the budget, so the best coverage wins
        self.assertAlmostEqual(toolpath.statistics()["pass_count"], 13)
        self.assertIn("切宽 6 → 2.94 mm", toolpath.notes[0])
        self.assertIn("不再提升", outcome.warnings[0])
        self.assertAlmostEqual(
            measure_coverage(toolpath, region, _tool()).ratio, 0.9988, delta=0.0005
        )

    def test_a_round_that_meets_the_target_but_costs_too_much_is_also_rejected(self) -> None:
        # Target 99.5%: 4.2 mm meets it, but 1.34x time exceeds the 1.2x limit, so 6 mm is kept
        _, outcome = _run(coverage_target=99.5, max_time_ratio=1.2)
        self.assertIn("切宽 6 → 6 mm", outcome.toolpath.notes[0])
        self.assertIn("未达标", outcome.toolpath.notes[0])
        self.assertIn("工时上限", outcome.warnings[0])

    def test_a_budget_below_the_first_round_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            _run(max_time_ratio=0.5)


class InvalidParameterTests(unittest.TestCase):
    def test_an_out_of_range_target_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            _run(coverage_target=101.0)
        with self.assertRaises(ParameterError):
            _run(coverage_target=10.0)

    def test_an_out_of_range_factor_is_rejected_by_the_parameter_layer(self) -> None:
        # The declaration already limits the factor to 0.3-0.95, so the API and UI reject it first
        for factor in (0.0, 1.0, 1.5):
            with self.subTest(factor=factor):
                with self.assertRaises(ParameterError):
                    _run(stepover_factor=factor)

    def test_the_planner_itself_rejects_a_factor_outside_zero_and_one(self) -> None:
        """Calling the domain layer directly, bypassing the parameter layer, must hold too: a factor >= 1 would widen."""

        entry = {item["id"]: item for item in planner_catalog()}["adaptive_contour"]
        defaults = {item["key"]: item["default"] for item in entry["parameters"]}
        for factor in (1.0, 1.5, -0.5):
            with self.subTest(factor=factor):
                context = PlanningContext(
                    tool=_tool(),
                    region=build_region("square", {"side_mm": 80.0}),
                    parameters={**defaults, "stepover_factor": factor},
                )
                with self.assertRaises(PlanningError):
                    AdaptiveContourPlanner().plan(context)

    def test_a_non_positive_floor_is_rejected(self) -> None:
        with self.assertRaises((ParameterError, PlanningError)):
            _run(min_stepover_mm=0.0)


if __name__ == "__main__":
    unittest.main()
