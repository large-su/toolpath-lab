"""新增功能的测试：刀具形态、螺旋刀路、材料切除仿真、刀路评价。

对应分支 feat/removal-sim-and-spiral：
- 刀具：球头/圆鼻刀启用 + 圆角半径参数 + 轴向切深下的咬入半径；
- 刀路：spiral 螺旋策略（圆形解析螺线 / 非圆等距环）；
- 仿真：Z-map 材料切除与残余统计；
- 评价：多策略评分与排序。
"""

from __future__ import annotations

import math
import unittest

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.region import (
    REGION_SHAPES,
    CircleRegion,
    RoundedRectangleRegion,
    build_region,
)
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.evaluation import (
    ComparisonReport,
    default_candidates,
    evaluate_strategies,
    format_table,
    suggest_stepover,
)
from toolpath_lab.planning import PLANNERS, run_plan
from toolpath_lab.simulation import simulate_removal


def flat_tool(diameter: float = 10.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=40.0)


class ToolShapeTests(unittest.TestCase):
    """刀具形态扩展。"""

    def test_ball_tool_cutting_footprint_grows_with_depth(self) -> None:
        tool = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=40.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 5.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.cutting_footprint_radius_mm(0.5),
                               math.sqrt(2 * 5 * 0.5 - 0.25), places=9)
        # ap 达到球半径后咬入半径封顶为 R
        self.assertAlmostEqual(tool.cutting_footprint_radius_mm(5.0), 5.0, places=9)
        self.assertAlmostEqual(tool.cutting_footprint_radius_mm(9.0), 5.0, places=9)

    def test_bull_tool_splits_bottom_and_corner(self) -> None:
        tool = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=2.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)
        # ap = 1 mm < Rc：底面还没接触，只有圆角部分吃刀
        expected = 3.0 + math.sqrt(2 * 2 * 1 - 1)
        self.assertAlmostEqual(tool.cutting_footprint_radius_mm(1.0), expected, places=9)
        # ap ≥ Rc 后整个底面参与切削
        self.assertAlmostEqual(tool.cutting_footprint_radius_mm(2.0), 5.0, places=9)

    def test_zero_depth_cuts_nothing(self) -> None:
        for kind in (ToolKind.FLAT, ToolKind.BALL, ToolKind.BULL):
            tool = Tool(kind, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=1.0)
            self.assertEqual(tool.cutting_footprint_radius_mm(0.0), 0.0)

    def test_corner_radius_must_be_smaller_than_radius(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, corner_radius_mm=5.0)

    def test_profile_describes_the_tip(self) -> None:
        flat = flat_tool(10.0).profile_mm()
        self.assertEqual(flat[0], [0.0, 0.0])
        self.assertEqual(flat[1], [5.0, 0.0])

        ball = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=40.0).profile_mm()
        self.assertAlmostEqual(ball[0][0], 0.0)
        self.assertTrue(any(abs(point[1] - 5.0) < 1e-6 for point in ball))

        bull = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0,
                    corner_radius_mm=2.0).profile_mm()
        self.assertAlmostEqual(bull[1][0], 3.0)


class RoundedRectangleRegionTests(unittest.TestCase):
    """新增区域形状。"""

    def test_registered_and_has_expected_area(self) -> None:
        self.assertIn("rounded_rect", REGION_SHAPES)
        region = RoundedRectangleRegion(side_x_mm=80.0, side_y_mm=60.0, corner_radius_mm=10.0)
        from toolpath_lab.core.region import polygon_area

        expected = 80.0 * 60.0 - (4.0 - math.pi) * 10.0 * 10.0
        # 圆弧用折线逼近，面积略小于解析值：允许 0.5% 的离散误差。
        self.assertAlmostEqual(polygon_area(region.boundary()), expected, delta=expected * 0.005)

    def test_zero_radius_is_a_plain_rectangle(self) -> None:
        region = build_region("rounded_rect", {"side_x_mm": 40.0, "side_y_mm": 20.0,
                                               "corner_radius_mm": 0.0})
        self.assertEqual(region.boundary().shape, (4, 2))

    def test_radius_larger_than_half_of_the_short_side_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            RoundedRectangleRegion(side_x_mm=40.0, side_y_mm=20.0, corner_radius_mm=11.0)

    def test_stadium_shape_is_allowed(self) -> None:
        region = RoundedRectangleRegion(side_x_mm=60.0, side_y_mm=20.0, corner_radius_mm=10.0)
        self.assertGreater(region.boundary().shape[0], 4)


class SpiralPlannerTests(unittest.TestCase):
    """新增刀路策略。"""

    def test_registered_in_the_catalog(self) -> None:
        self.assertIn("spiral", PLANNERS)
        entry = [item for item in PLANNERS.catalog() if item["id"] == "spiral"][0]
        self.assertEqual(entry["label"], "螺旋刀路")
        self.assertIn("stepover_mm", [spec["key"] for spec in entry["parameters"]])

    def test_circle_spiral_is_one_continuous_cut(self) -> None:
        outcome = run_plan(
            planner_id="spiral",
            tool=flat_tool(10.0),
            region=CircleRegion(diameter_mm=80.0),
            parameters={"stepover_mm": 6.0, "sample_step_mm": 1.5},
        )
        toolpath = outcome.toolpath
        kinds = [move.kind.value for move in toolpath.moves]
        self.assertEqual(kinds.count("cut"), 1)
        self.assertEqual(kinds.count("rapid"), 2)  # 只有下刀与抬刀
        self.assertNotIn("link", kinds)
        # 半径不超过 区域半径 - 刀具足迹半径
        radius = max(float(point[0] ** 2 + point[1] ** 2) ** 0.5 for move in toolpath.moves
                     for point in move.points) if False else None
        planar = toolpath.moves[1].points
        self.assertLessEqual(
            float((planar[:, 0] ** 2 + planar[:, 1] ** 2).max()) ** 0.5, 40.0 - 5.0 + 1e-6
        )

    def test_circle_spiral_stepover_is_capped(self) -> None:
        outcome = run_plan(
            planner_id="spiral",
            tool=flat_tool(10.0),
            region=CircleRegion(diameter_mm=60.0),
            parameters={"stepover_mm": 7.0, "sample_step_mm": 2.0},
        )
        notes = " ".join(outcome.toolpath.notes)
        self.assertIn("实际切宽", notes)
        pitch = float(notes.split("实际切宽")[1].split("mm")[0])
        self.assertLessEqual(pitch, 7.0 + 1e-9)

    def test_square_region_uses_the_ring_branch(self) -> None:
        outcome = run_plan(
            planner_id="spiral",
            tool=flat_tool(8.0),
            region=build_region("square", {"side_mm": 60.0}),
            parameters={"stepover_mm": 6.0, "sample_step_mm": 2.0},
        )
        kinds = [move.kind.value for move in outcome.toolpath.moves]
        self.assertGreater(kinds.count("cut"), 1)
        self.assertEqual(kinds.count("rapid"), 2)
        self.assertIn("等距环", " ".join(outcome.toolpath.notes))

    def test_tiny_region_is_reported_as_a_planning_error(self) -> None:
        from toolpath_lab.core.errors import PlanningError

        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="spiral",
                tool=flat_tool(40.0),
                region=CircleRegion(diameter_mm=20.0),
                parameters={"stepover_mm": 5.0},
            )


class RemovalSimulationTests(unittest.TestCase):
    """材料切除仿真。"""

    def _toolpath(self, planner_id: str, parameters: dict, *, diameter: float = 10.0):
        return run_plan(
            planner_id=planner_id,
            tool=flat_tool(diameter),
            region=build_region("square", {"side_mm": 60.0}),
            parameters=parameters,
        ).toolpath

    def test_generous_stepover_cleans_the_face(self) -> None:
        toolpath = self._toolpath("raster", {"mode": "zigzag", "stepover_mm": 4.0})
        report = simulate_removal(toolpath, flat_tool(10.0),
                                  build_region("square", {"side_mm": 60.0}),
                                  resolution_mm=1.0, axial_depth_mm=1.0)
        self.assertGreater(report.metrics["coverage_ratio"], 0.99)
        self.assertLess(report.metrics["residual_volume_mm3"], 60.0 * 60.0 * 0.01)
        self.assertAlmostEqual(report.metrics["max_residual_mm"], 1.0, places=6)

    def test_large_stepover_leaves_residual_ridges(self) -> None:
        toolpath = self._toolpath("raster", {"mode": "zigzag", "stepover_mm": 14.0})
        report = simulate_removal(toolpath, flat_tool(10.0),
                                  build_region("square", {"side_mm": 60.0}),
                                  resolution_mm=1.0, axial_depth_mm=1.0)
        self.assertLess(report.metrics["coverage_ratio"], 0.95)
        self.assertGreater(report.metrics["residual_volume_mm3"], 0.0)
        self.assertGreater(report.metrics["max_residual_mm"], 0.9)

    def test_ball_tool_coverage_drops_with_shallow_depth(self) -> None:
        """球头刀在平面上的咬入半径随 ap 迅速变小，同样切宽下覆盖率显著低于平底刀。"""

        region = build_region("square", {"side_mm": 60.0})
        parameters = {"mode": "zigzag", "stepover_mm": 6.0}
        ball_tool = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=40.0)
        flat = run_plan(planner_id="raster", tool=flat_tool(10.0), region=region,
                        parameters=parameters).toolpath
        ball = run_plan(planner_id="raster", tool=ball_tool, region=region,
                        parameters=parameters).toolpath
        # ap = 0.2 mm：球头刀咬入半径仅 sqrt(2·5·0.2−0.04) ≈ 1.39 mm，远小于 6 mm 切宽
        report_flat = simulate_removal(flat, flat_tool(10.0), region,
                                       resolution_mm=1.0, axial_depth_mm=0.2)
        report_ball = simulate_removal(ball, ball_tool, region,
                                       resolution_mm=1.0, axial_depth_mm=0.2)
        self.assertGreater(report_flat.metrics["coverage_ratio"],
                           report_ball.metrics["coverage_ratio"] + 0.3)

    def test_raster_keeps_a_flat_tool_inside_the_boundary(self) -> None:
        """平底刀按足迹半径内缩，刀路不会切到区域外——这是"无过切"的正向验证。"""

        region = build_region("square", {"side_mm": 40.0})
        toolpath = run_plan(planner_id="raster", tool=flat_tool(10.0), region=region,
                            parameters={"mode": "one_way", "stepover_mm": 4.0}).toolpath
        report = simulate_removal(toolpath, flat_tool(10.0), region,
                                  resolution_mm=1.0, axial_depth_mm=1.0)
        self.assertEqual(report.metrics["overcut_outside_area_mm2"], 0.0)

    def test_ball_tool_path_can_cut_outside_the_boundary(self) -> None:
        """球头刀足迹半径为 0（刀路走到边界上），实际咬入半径却大于 0，于是边界外被切到。"""

        region = build_region("square", {"side_mm": 40.0})
        ball_tool = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=40.0)
        toolpath = run_plan(planner_id="raster", tool=ball_tool, region=region,
                            parameters={"mode": "one_way", "stepover_mm": 4.0}).toolpath
        report = simulate_removal(toolpath, ball_tool, region,
                                  resolution_mm=1.0, axial_depth_mm=1.0)
        self.assertGreater(report.metrics["overcut_outside_area_mm2"], 0.0)

    def test_payload_is_json_friendly_and_downsampled(self) -> None:
        toolpath = self._toolpath("spiral", {"stepover_mm": 5.0})
        report = simulate_removal(toolpath, flat_tool(10.0),
                                  build_region("square", {"side_mm": 60.0}),
                                  resolution_mm=1.0, axial_depth_mm=1.0)
        payload = report.to_payload(max_cells=16)
        self.assertLessEqual(len(payload["heights"]), 16)
        self.assertEqual(len(payload["heights"][0]), len(payload["inside"][0]))
        self.assertIn("coverage_ratio", payload["metrics"])

    def test_invalid_parameters_are_rejected(self) -> None:
        toolpath = self._toolpath("raster", {"mode": "zigzag"})
        with self.assertRaises(ParameterError):
            simulate_removal(toolpath, flat_tool(10.0),
                             build_region("square", {"side_mm": 60.0}),
                             resolution_mm=0.0, axial_depth_mm=1.0)


class EvaluationTests(unittest.TestCase):
    """刀路评价与多策略对比。"""

    def test_default_candidates_cover_the_registered_planners(self) -> None:
        ids = {planner_id for planner_id, _ in default_candidates()}
        self.assertIn("raster", ids)
        self.assertIn("spiral", ids)

    def test_evaluation_ranks_by_total_score(self) -> None:
        report = evaluate_strategies(
            tool=flat_tool(10.0),
            region=build_region("square", {"side_mm": 60.0}),
            candidates=[("raster", {"mode": "zigzag", "stepover_mm": 5.0}),
                        ("spiral", {"stepover_mm": 5.0})],
            resolution_mm=1.0,
            axial_depth_mm=1.0,
        )
        self.assertIsInstance(report, ComparisonReport)
        scores = [entry.total_score for entry in report.entries]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(report.best.planner_id, report.entries[0].planner_id)

    def test_weights_must_sum_to_one(self) -> None:
        with self.assertRaises(ParameterError):
            evaluate_strategies(
                tool=flat_tool(10.0),
                region=build_region("square", {"side_mm": 60.0}),
                candidates=[("raster", {})],
                weights=(0.5, 0.5, 0.5),
            )

    def test_scores_are_bounded_and_table_renders(self) -> None:
        report = evaluate_strategies(
            tool=flat_tool(10.0),
            region=CircleRegion(diameter_mm=60.0),
            candidates=[("raster", {"mode": "zigzag", "stepover_mm": 5.0}),
                        ("spiral", {"stepover_mm": 5.0})],
            resolution_mm=1.0,
        )
        for entry in report.entries:
            self.assertGreaterEqual(entry.total_score, 0.0)
            self.assertLessEqual(entry.total_score, 100.0)
            self.assertGreater(entry.removal_metrics["coverage_ratio"], 0.5)
        table = format_table(report)
        self.assertIn("总分", table)
        self.assertGreaterEqual(len(table.splitlines()), 3)

    def test_repeated_evaluation_is_deterministic(self) -> None:
        kwargs = dict(
            tool=flat_tool(10.0),
            region=build_region("square", {"side_mm": 40.0}),
            candidates=[("raster", {"mode": "one_way", "stepover_mm": 5.0})],
            resolution_mm=1.0,
        )
        first = evaluate_strategies(**kwargs).entries[0].total_score
        second = evaluate_strategies(**kwargs).entries[0].total_score
        self.assertAlmostEqual(first, second, places=9)

    def test_suggest_stepover_shrinks_for_ball_tools(self) -> None:
        flat = suggest_stepover(flat_tool(10.0), 1.0)
        ball = suggest_stepover(Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=40.0), 1.0)
        self.assertGreater(flat, ball)


class EvaluateApiTests(unittest.TestCase):
    """新增接口 /api/evaluate。"""

    @classmethod
    def setUpClass(cls) -> None:
        import threading

        from toolpath_lab.server.app import create_server

        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        import json
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_evaluate_returns_a_ranked_table(self) -> None:
        status, payload = self.post("/api/evaluate", {
            "tool": {"kind": "flat", "diameter_mm": 10.0},
            "region": {"shape": "square", "parameters": {"side_mm": 60.0}},
            "simulation": {"resolution_mm": 1.5, "axial_depth_mm": 1.0},
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(len(payload["entries"]), 2)
        scores = [entry["scores"]["total"] for entry in payload["entries"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(payload["best_planner_id"], payload["entries"][0]["planner_id"])
        self.assertIn("coverage_ratio", payload["entries"][0]["removal"])
        self.assertEqual(payload["weighting"], {"quality": 0.5, "efficiency": 0.3, "air": 0.2})

    def test_unknown_candidate_is_reported(self) -> None:
        status, payload = self.post("/api/evaluate", {
            "candidates": [{"id": "trochoidal"}],
        })
        self.assertEqual(status, 400)
        self.assertIn("trochoidal", payload["error"])

    def test_bad_weights_are_rejected(self) -> None:
        status, payload = self.post("/api/evaluate", {
            "candidates": [{"id": "raster"}],
            "weights": [0.5, 0.5, 0.5],
        })
        self.assertEqual(status, 400)
        self.assertIn("weights", payload["error"])


if __name__ == "__main__":
    unittest.main()
