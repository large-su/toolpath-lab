"""环切策略：几何、注册与 API 集成。"""

from __future__ import annotations

import json
import unittest

import numpy as np

from toolpath_lab.planning.contour import offset_polygon, resample_ring
from tests.test_api import ApiTestCase


class ContourGeometryTests(unittest.TestCase):
    """多边形等距偏置与环重采样。"""

    def test_offset_polygon_insets_a_square(self) -> None:
        square = np.array([[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0]])
        inner = offset_polygon(square, 2.0)
        self.assertIsNotNone(inner)
        # 内缩 2 mm 后边长 16 mm
        self.assertAlmostEqual(float(np.ptp(inner[:, 0])), 16.0, places=6)

    def test_offset_polygon_returns_none_when_too_large(self) -> None:
        square = np.array([[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0]])
        # 偏置量超过内切半径，环退化
        self.assertIsNone(offset_polygon(square, 15.0))

    def test_resample_ring_produces_smooth_point_cloud(self) -> None:
        square = np.array([[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0]])
        sampled = resample_ring(square, 1.0)
        self.assertGreater(sampled.shape[0], 20)


class ContourPlannerApiTests(ApiTestCase):
    """环切策略在接口层的注册与规划。"""

    def test_contour_is_published_in_catalog(self) -> None:
        status, body, _ = self.get("/api/catalog")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        ids = [item["id"] for item in payload["planners"]["list"]]
        self.assertIn("contour", ids)
        entry = next(item for item in payload["planners"]["list"] if item["id"] == "contour")
        self.assertEqual(entry["label"], "环切刀路")

    def _plan(self, tool, region, planner_parameters=None):
        return self.plan({
            "tool": tool,
            "region": region,
            "planner": {
                "id": "contour",
                "parameters": planner_parameters or {
                    "stepover_mm": 6.0, "sample_step_mm": 1.0, "feed_mm_per_min": 600.0,
                },
            },
        })

    def test_contour_plans_rings_on_circle(self) -> None:
        status, payload, _ = self._plan(
            {"kind": "flat", "diameter_mm": 6.0, "length_mm": 30.0},
            {"shape": "circle", "parameters": {"diameter_mm": 80.0}},
        )
        self.assertEqual(status, 200)
        moves = payload["toolpath"]["moves"]
        cut = [move for move in moves if move["kind"] == "cut"]
        self.assertGreaterEqual(len(cut), 2)  # 至少两环
        # 第一环贴近边界：点到圆心距离 ≈ 半径 - 足迹半径
        first = cut[0]
        x, y = first["points"][0][0], first["points"][0][1]
        self.assertAlmostEqual((x * x + y * y) ** 0.5, 37.0, delta=0.5)

    def test_contour_plans_on_square(self) -> None:
        status, payload, _ = self._plan(
            {"kind": "ball", "diameter_mm": 6.0, "length_mm": 30.0},
            {"shape": "square", "parameters": {"side_mm": 80.0}},
        )
        self.assertEqual(status, 200)
        cut = [move for move in payload["toolpath"]["moves"] if move["kind"] == "cut"]
        self.assertGreaterEqual(len(cut), 2)

    def test_contour_rejects_oversized_tool(self) -> None:
        # D80 足迹半径 40 = 方形内切半径，首环即退化 → 无环 → 规划失败（422）
        status, payload, _ = self._plan(
            {"kind": "flat", "diameter_mm": 80.0, "length_mm": 30.0},
            {"shape": "square", "parameters": {"side_mm": 80.0}},
        )
        self.assertEqual(status, 422)
        self.assertIn("环切未生成任何刀轨", payload["error"])


if __name__ == "__main__":
    unittest.main()
