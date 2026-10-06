"""HTTP 层：路由、校验、错误码与静态前端。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from toolpath_lab.server.app import ToolpathLabHandler, create_server
from toolpath_lab.simulation import DEFAULT_FRAME_BUDGET


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
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

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def plan(self, payload):
        status, body, headers = self.post("/api/plan", payload)
        return status, json.loads(body), headers


class StaticTests(ApiTestCase):
    def test_index_is_served(self) -> None:
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"ToolpathLab", body)

    def test_modules_and_vendor_files_are_served(self) -> None:
        for path in ("/js/main.js", "/js/viewport.js", "/style.css",
                     "/vendor/three.module.js", "/vendor/RoomEnvironment.js"):
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_application_icon_is_served(self) -> None:
        status, body, headers = self.get("/icon.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_favicon_route_returns_the_icon(self) -> None:
        # 浏览器会直接请求 /favicon.ico，这里返回同一张 PNG，而不是空响应。
        status, body, headers = self.get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_missing_file_is_a_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.get("/js/nope.js")
        self.assertEqual(context.exception.code, 404)

    def test_path_traversal_is_refused(self) -> None:
        self.assertIsNone(ToolpathLabHandler._resolve_static("../requirements.txt"))
        self.assertIsNone(ToolpathLabHandler._resolve_static("../../etc/passwd"))
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.get("/%2e%2e/requirements.txt")
        self.assertEqual(context.exception.code, 404)


class CatalogTests(ApiTestCase):
    def test_health(self) -> None:
        status, body, _ = self.get("/api/health")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("version", payload)

    def test_catalog_exposes_the_simplified_scope(self) -> None:
        status, body, _ = self.get("/api/catalog")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["planners"]["list"]], ["raster"])
        self.assertEqual(
            sorted(item["id"] for item in payload["regions"]["shapes"]), ["circle", "square"]
        )
        self.assertNotIn("surfaces", payload)
        self.assertNotIn("presets", payload)
        self.assertEqual([item["key"] for item in payload["tool"]["parameters"]],
                         ["kind", "diameter_mm", "corner_radius_mm", "length_mm"])

    def test_catalog_reports_the_fixed_settings(self) -> None:
        _, body, _ = self.get("/api/catalog")
        fixed = json.loads(body)["fixed"]
        self.assertEqual(fixed["safe_height_mm"], 5.0)
        self.assertEqual(fixed["rapid_feed_mm_per_min"], 5000.0)

    def test_catalog_publishes_the_stock_group(self) -> None:
        _, body, _ = self.get("/api/catalog")
        stock = json.loads(body)["stock"]
        self.assertEqual(
            [item["key"] for item in stock["parameters"]],
            ["depth_mm", "top_mm", "cut_depth_mm", "margin_mm", "resolution_mm", "frame_budget"],
        )
        self.assertEqual(stock["defaults"]["frame_budget"], DEFAULT_FRAME_BUDGET)
        self.assertGreater(stock["defaults"]["cut_depth_mm"], 0)
        self.assertGreater(stock["simulation"]["max_grid_cells"], 0)

    def test_tool_kind_availability_is_published(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        # 平底刀、球头刀、圆鼻刀三种形态都已可选。
        self.assertEqual([item["disabled"] for item in kinds], [False, False, False])

    def test_unknown_endpoint(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.get("/api/nope")
        self.assertEqual(context.exception.code, 404)


class PlanTests(ApiTestCase):
    def test_minimal_request_uses_defaults(self) -> None:
        status, payload, _ = self.plan({})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        statistics = payload["toolpath"]["statistics"]
        self.assertGreater(statistics["pass_count"], 0)
        self.assertIsNotNone(payload["timeline"])
        self.assertTrue(payload["region"]["boundary"])

    def test_response_carries_everything_the_viewer_needs(self) -> None:
        status, payload, _ = self.plan(
            {
                "tool": {"diameter_mm": 8.0, "length_mm": 40.0},
                "region": {"shape": "circle", "parameters": {"diameter_mm": 60.0}},
                "planner": {"id": "raster", "parameters": {"mode": "one_way", "stepover_mm": 4.0}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["request"]["region"]["shape"], "circle")
        self.assertEqual(payload["tool"]["diameter_mm"], 8.0)
        self.assertEqual(payload["tool"]["length_mm"], 40.0)
        self.assertEqual(len(payload["region"]["boundary"][0]), 3)
        timeline = payload["timeline"]
        self.assertEqual(len(timeline["times"]), timeline["sample_count"])
        self.assertEqual(len(timeline["positions"]), timeline["sample_count"])
        for move in payload["toolpath"]["moves"]:
            self.assertIn(move["kind"], {"cut", "link", "rapid"})
            self.assertGreaterEqual(len(move["points"]), 2)

    def test_parameters_are_echoed_back_normalised(self) -> None:
        _, payload, _ = self.plan({"planner": {"parameters": {"stepover_mm": 8}}})
        parameters = payload["request"]["planner"]["parameters"]
        self.assertEqual(parameters["stepover_mm"], 8.0)
        self.assertEqual(parameters["mode"], "zigzag")
        self.assertEqual(parameters["feed_mm_per_min"], 600.0)

    def test_unknown_planner_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"planner": {"id": "spiral"}})
        self.assertEqual(status, 400)
        self.assertIn("spiral", payload["error"])

    def test_unknown_region_shape_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"region": {"shape": "hexagon"}})
        self.assertEqual(status, 400)

    def test_invalid_value_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"tool": {"diameter_mm": -3}})
        self.assertEqual(status, 400)
        self.assertIn("diameter_mm", payload["error"])

    def test_impossible_geometry_is_unprocessable(self) -> None:
        # 参数本身合法（D60 在允许范围内），但足迹半径超过区域宽度：这是几何不可行，不是参数错误。
        status, payload, _ = self.plan(
            {"tool": {"diameter_mm": 60.0}, "region": {"shape": "square", "parameters": {"side_mm": 40.0}}}
        )
        self.assertEqual(status, 422)
        self.assertTrue(payload["error"])

    def test_warnings_are_returned(self) -> None:
        _, payload, _ = self.plan(
            {"tool": {"diameter_mm": 6.0}, "planner": {"parameters": {"stepover_mm": 40.0}}}
        )
        self.assertTrue(payload["warnings"])

    def test_malformed_body_is_a_bad_request(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/plan", data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=30)
        self.assertEqual(context.exception.code, 400)


class StockSimulationTests(ApiTestCase):
    """材料切除仿真在接口层的表现。"""

    def test_plan_carries_the_carved_stock(self) -> None:
        status, payload, _ = self.plan({})
        self.assertEqual(status, 200)
        stock = payload["stock"]
        self.assertIsNotNone(stock)
        self.assertTrue(stock["enabled"])
        self.assertEqual(stock["columns"], stock["rows"])  # 默认方形区域
        self.assertGreater(stock["statistics"]["cut_cells"], 0)
        self.assertEqual(len(stock["times"]), len(stock["frames"]))
        # 初始帧是完整毛坯，没有增量；随后的帧才带数据。
        self.assertEqual(stock["frames"][0], [])
        self.assertTrue(any(runs for runs in stock["frames"][1:]))

    def test_stock_parameters_are_echoed_back(self) -> None:
        _, payload, _ = self.plan(
            {"stock": {"depth_mm": 8.0, "resolution_mm": 0.5, "frame_budget": 8}}
        )
        self.assertEqual(payload["request"]["stock"]["depth_mm"], 8.0)
        self.assertEqual(payload["request"]["stock"]["resolution_mm"], 0.5)
        self.assertEqual(payload["stock"]["statistics"]["frame_count"] - 1, 8)

    def test_invalid_stock_parameter_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"stock": {"depth_mm": -5.0}})
        self.assertEqual(status, 400)
        self.assertIn("depth_mm", payload["error"])

    def test_more_frames_never_lose_the_final_shape(self) -> None:
        # 帧数不同只改变动画的中间状态，终态（最后一帧）必须一样。
        _, sparse, _ = self.plan({"stock": {"frame_budget": 4, "resolution_mm": 1.0}})
        _, dense, _ = self.plan({"stock": {"frame_budget": 16, "resolution_mm": 1.0}})
        self.assertEqual(
            sparse["stock"]["statistics"]["cut_cells"],
            dense["stock"]["statistics"]["cut_cells"],
        )
        self.assertEqual(
            sparse["stock"]["statistics"]["removed_volume_mm3"],
            dense["stock"]["statistics"]["removed_volume_mm3"],
        )
        self.assertNotEqual(sparse["stock"]["times"], dense["stock"]["times"])

    def test_export_does_not_waste_time_on_the_simulation(self) -> None:
        status, body, _ = self.post("/api/export/gcode", {})
        self.assertEqual(status, 200)
        self.assertIn(b"M30", body)


class ExportTests(ApiTestCase):
    def test_gcode_download(self) -> None:
        status, body, headers = self.post("/api/export/gcode", {})
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(headers["Content-Disposition"].endswith('.nc"'))
        text = body.decode("utf-8")
        self.assertIn("G21", text)
        self.assertIn("M30", text)

    def test_other_formats_are_gone(self) -> None:
        for kind in ("csv", "json", "step"):
            with self.subTest(kind=kind):
                status, _, _ = self.post(f"/api/export/{kind}", {})
                self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
