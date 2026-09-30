"""HTTP 层：路由、校验、错误码与静态前端。"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
import urllib.error
import urllib.request

from toolpath_lab.server.app import ToolpathLabHandler, create_server


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
        self.assertEqual(
            [item["id"] for item in payload["planners"]["list"]],
            ["raster", "follow_periphery"],
        )
        self.assertEqual(
            sorted(item["id"] for item in payload["regions"]["shapes"]),
            ["circle", "ramp", "square"],
        )
        self.assertNotIn("surfaces", payload)
        self.assertNotIn("presets", payload)
        self.assertEqual([item["key"] for item in payload["tool"]["parameters"]],
                         ["kind", "diameter_mm", "length_mm", "corner_radius_mm"])

    def test_catalog_reports_the_fixed_settings(self) -> None:
        _, body, _ = self.get("/api/catalog")
        fixed = json.loads(body)["fixed"]
        self.assertEqual(fixed["safe_height_mm"], 5.0)
        self.assertEqual(fixed["rapid_feed_mm_per_min"], 5000.0)

    def test_every_tool_kind_is_selectable(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        self.assertEqual([item["disabled"] for item in kinds], [False, False, False])
        self.assertEqual([item["value"] for item in kinds], ["flat", "ball", "bull"])

    def test_corner_radius_is_only_shown_for_the_bull_kind(self) -> None:
        _, body, _ = self.get("/api/catalog")
        parameters = {item["key"]: item for item in json.loads(body)["tool"]["parameters"]}
        self.assertEqual(parameters["corner_radius_mm"]["visible_if"], {"kind": "bull"})
        self.assertEqual(parameters["corner_radius_mm"]["default"], 1.0)

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

    def test_follow_periphery_request_is_accepted(self) -> None:
        status, payload, _ = self.plan(
            {
                "region": {"shape": "square", "parameters": {"side_mm": 60.0}},
                "planner": {
                    "id": "follow_periphery",
                    "parameters": {"direction": "outward", "winding": "cw", "stepover_mm": 5.0},
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["toolpath"]["planner"], "follow_periphery")
        self.assertEqual(payload["toolpath"]["planner_label"], "跟随周边")
        self.assertEqual(payload["request"]["planner"]["parameters"]["direction"], "outward")
        self.assertEqual(payload["request"]["planner"]["parameters"]["winding"], "cw")
        self.assertTrue(any("顺时针" in note for note in payload["toolpath"]["notes"]))
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 1)

    def test_ball_tool_request_is_accepted(self) -> None:
        status, payload, _ = self.plan(
            {"tool": {"kind": "ball", "diameter_mm": 8.0, "length_mm": 40.0}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["tool"]["kind"], "ball")
        self.assertEqual(payload["tool"]["footprint_radius_mm"], 0.0)
        self.assertEqual(payload["tool"]["radius_mm"], 4.0)
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)

    def test_ramp_region_reports_its_surface(self) -> None:
        status, payload, _ = self.plan(
            {
                "region": {"shape": "ramp", "parameters": {"side_mm": 80.0, "angle_deg": 60.0}},
                "planner": {"id": "raster", "parameters": {"stepover_mm": 6.0}},
            }
        )
        self.assertEqual(status, 200)
        region = payload["region"]
        self.assertEqual(region["id"], "ramp")
        self.assertEqual(region["surface"]["kind"], "ramp")
        self.assertEqual(region["surface"]["cap_z_mm"], 80.0)
        self.assertAlmostEqual(region["surface"]["crease_x_mm"], -6.188, places=2)

        # 轮廓带上加工面高度：低边（+X）0、高边（−X）80
        self.assertEqual(sorted(point[2] for point in region["boundary"]), [0.0, 0.0, 80.0, 80.0])
        self.assertEqual(region["boundary"][1][0], 40.0)
        self.assertEqual(region["boundary"][1][2], 0.0)
        # 顶面分片供前端拼实体：斜段（靠 +X） + 平顶（靠 −X）
        self.assertEqual(len(region["top_patches"]), 2)
        self.assertTrue(all(len(patch) == 4 for patch in region["top_patches"]))

        # 只加工斜面段：刀路范围收窄到斜面段（分界处留一个足迹半径），工件轮廓仍是整个斜坡
        machining = region["machining_boundary"]
        self.assertEqual(len(machining), 4)
        self.assertAlmostEqual(min(point[0] for point in machining), -6.188, places=2)
        self.assertAlmostEqual(min(point[0] for point in region["boundary"]), -40.0, places=4)
        self.assertEqual(region["parameters"]["include_plateau"], False)

        # 下刀沿加工面切入：第一段切削从料外（x = 42）引入，刀路本身由低往高
        lead_in = [move for move in payload["toolpath"]["moves"]
                   if move["kind"] == "cut" and move["label"] == "沿面切入"]
        self.assertEqual(len(lead_in), 1)
        self.assertAlmostEqual(lead_in[0]["points"][0][0], 42.0, places=4)
        passes = [move for move in payload["toolpath"]["moves"]
                  if move["kind"] == "cut" and move["label"] != "沿面切入"]
        self.assertGreater(passes[0]["points"][-1][2], passes[0]["points"][0][2])

    def test_flat_region_reports_a_flat_surface(self) -> None:
        _, payload, _ = self.plan({})
        self.assertEqual(payload["region"]["surface"]["kind"], "flat")
        self.assertTrue(all(point[2] == 0.0 for point in payload["region"]["boundary"]))

    def test_bull_tool_request_is_accepted(self) -> None:
        status, payload, _ = self.plan(
            {"tool": {"kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0,
                      "corner_radius_mm": 1.5}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["tool"]["kind"], "bull")
        self.assertEqual(payload["tool"]["corner_radius_mm"], 1.5)
        self.assertEqual(payload["tool"]["footprint_radius_mm"], 3.5)
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)

    def test_corner_radius_larger_than_the_radius_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan(
            {"tool": {"kind": "bull", "diameter_mm": 6.0, "length_mm": 30.0,
                      "corner_radius_mm": 4.0}}
        )
        self.assertEqual(status, 400)
        self.assertIn("圆角", payload["error"])

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


class KeepAliveTests(ApiTestCase):
    """HTTP/1.1 长连接：没被读走的请求体不能污染下一个请求。

    未知接口不读请求体就直接回应，残留字节会被当成"下一个请求"解析；在 Windows 上
    带着未读数据关闭套接字还会发 RST，客户端读到 ConnectionAbortedError 而不是响应。
    """

    def _connection(self) -> http.client.HTTPConnection:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        self.addCleanup(connection.close)
        return connection

    def test_unread_body_does_not_leak_into_the_next_request(self) -> None:
        connection = self._connection()
        body = json.dumps({"tool": {"diameter_mm": 6.0}})
        connection.request("POST", "/api/export/json", body=body,
                           headers={"Content-Type": "application/json"})
        first = connection.getresponse()
        self.assertEqual(first.status, 404)
        first.read()

        connection.request("GET", "/api/health")
        second = connection.getresponse()
        self.assertEqual(second.status, 200)
        self.assertTrue(json.loads(second.read())["ok"])

    def test_unread_body_after_a_bad_method_is_drained_too(self) -> None:
        connection = self._connection()
        connection.request("POST", "/index.html", body=json.dumps({"x": 1}),
                           headers={"Content-Type": "application/json"})
        first = connection.getresponse()
        self.assertEqual(first.status, 405)
        first.read()

        connection.request("GET", "/api/health")
        self.assertEqual(connection.getresponse().status, 200)


if __name__ == "__main__":
    unittest.main()
