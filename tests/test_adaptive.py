"""自适应环切：切宽自动收紧到覆盖率达标，到不了就如实说明。"""

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
        # 环切的参数一个不少，再加四项目适应专属
        for inherited in ("stepover_mm", "sample_step_mm", "ring_direction", "feed_mm_per_min",
                          "safe_height_mm", "rapid_feed_mm_per_min"):
            self.assertIn(inherited, keys)
        self.assertEqual(
            keys[-4:], ["coverage_target", "max_rounds", "stepover_factor", "min_stepover_mm"]
        )
        by_key = {item["key"]: item for item in entry["parameters"]}
        self.assertEqual(by_key["coverage_target"]["default"], 99.5)
        self.assertEqual(by_key["max_rounds"]["default"], 3)
        self.assertEqual(by_key["stepover_factor"]["default"], 0.7)
        self.assertEqual(by_key["min_stepover_mm"]["default"], 1.0)


class TighteningTests(unittest.TestCase):
    def test_it_tightens_until_the_target_is_met(self) -> None:
        region, outcome = _run()
        toolpath = outcome.toolpath
        # 方形 80、D6：切宽 6 mm 时 99.31%，收紧一轮到 4.2 mm 就是 99.86%
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
        """三角形的残留是几何极限：再收窄也不会更好，就不该继续白算。"""

        region, outcome = _run("triangle")
        toolpath = outcome.toolpath
        # 6 → 4.2 提升明显，2.94 与 4.2 完全一样，于是停在 4.2（环数更少的那次）
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
        """覆盖率并非切宽越小越高（哑铃在 1 mm 时反而更差），所以必须保留最好的一次。"""

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
        # 每一轮的覆盖率都记在 notes 里，便于回溯
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
        # 下限 5 mm：先试 5（6 × 0.7 = 4.2 被抬到下限），再收就没有意义了
        self.assertIn("各轮实测：6 mm", outcome.toolpath.notes[1])
        self.assertIn("5 mm", outcome.toolpath.notes[1])
        self.assertIn("共算 2 次", outcome.toolpath.notes[0])

    def test_the_notes_keep_the_contour_notes(self) -> None:
        _, outcome = _run()
        notes = outcome.toolpath.notes
        self.assertIn("自适应环切", notes[0])
        self.assertIn("各轮实测", notes[1])
        self.assertTrue(any("环切：共" in note for note in notes[2:]))

    def test_the_result_is_labelled_as_the_adaptive_planner(self) -> None:
        payload = _run()[1].toolpath.to_payload()
        self.assertEqual(payload["planner"], "adaptive_contour")
        self.assertEqual(payload["planner_label"], "自适应环切")


class InvalidParameterTests(unittest.TestCase):
    def test_an_out_of_range_target_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            _run(coverage_target=101.0)
        with self.assertRaises(ParameterError):
            _run(coverage_target=10.0)

    def test_an_out_of_range_factor_is_rejected_by_the_parameter_layer(self) -> None:
        # 参数声明里已经限定 0.3–0.95，接口与界面都会先拦下来
        for factor in (0.0, 1.0, 1.5):
            with self.subTest(factor=factor):
                with self.assertRaises(ParameterError):
                    _run(stepover_factor=factor)

    def test_the_planner_itself_rejects_a_factor_outside_zero_and_one(self) -> None:
        """绕过参数层直接调用域层时也得守住：系数 ≥ 1 就是"越收越宽"。"""

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
