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
            ["raster", "spiral", "contour"],
        )
        self.assertEqual(
            sorted(item["id"] for item in payload["regions"]["shapes"]),
            ["circle", "ellipse", "rounded_rect", "square"],
        )
        self.assertNotIn("surfaces", payload)
        self.assertNotIn("presets", payload)
        self.assertEqual(
            [item["key"] for item in payload["tool"]["parameters"]],
            ["kind", "diameter_mm", "length_mm", "corner_radius_mm"],
        )

    def test_catalog_exposes_the_editable_setup_group(self) -> None:
        status, body, _ = self.get("/api/catalog")
        setup = json.loads(body)["setup"]
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["key"] for item in setup["parameters"]],
            ["safe_height_mm", "rapid_feed_mm_per_min", "boundary_mode",
             "boundary_offset_mm"],
        )
        self.assertEqual(setup["defaults"]["safe_height_mm"], 5.0)
        self.assertEqual(setup["defaults"]["rapid_feed_mm_per_min"], 5000.0)

    def test_catalog_still_reports_the_legacy_fixed_block(self) -> None:
        _, body, _ = self.get("/api/catalog")
        fixed = json.loads(body)["fixed"]
        self.assertEqual(fixed["safe_height_mm"], 5.0)
        self.assertEqual(fixed["rapid_feed_mm_per_min"], 5000.0)

    def test_every_tool_kind_is_published(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        self.assertEqual([item["value"] for item in kinds],
                         ["flat", "ball", "bull"])
        self.assertEqual([item["disabled"] for item in kinds], [False, False, False])

    def test_setup_values_change_the_toolpath(self) -> None:
        _, base, _ = self.plan({})
        _, tall, _ = self.plan({"setup": {"safe_height_mm": 20.0}})
        heights = [
            max(point[2] for move in payload["toolpath"]["moves"]
                for point in move["points"])
            for payload in (base, tall)
        ]
        self.assertAlmostEqual(heights[0], 5.0, places=3)
        self.assertAlmostEqual(heights[1], 20.0, places=3)

    def test_boundary_mode_none_reaches_the_wall(self) -> None:
        _, payload, _ = self.plan({"setup": {"boundary_mode": "none"}})
        self.assertEqual(payload["request"]["setup"]["boundary_mode"], "none")
        widest = max(
            abs(point[0]) for move in payload["toolpath"]["moves"]
            if move["kind"] == "cut" for point in move["points"]
        )
        self.assertAlmostEqual(widest, 40.0, places=3)

    def test_invalid_setup_value_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"setup": {"safe_height_mm": -1}})
        self.assertEqual(status, 400)
        self.assertIn("safe_height_mm", payload["error"])

    def test_bull_nose_plans_over_http(self) -> None:
        status, payload, _ = self.plan(
            {"tool": {"kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0,
                      "corner_radius_mm": 3.0}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["tool"]["kind"], "bull")
        self.assertAlmostEqual(payload["tool"]["footprint_radius_mm"], 2.0, places=6)

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
        status, payload, _ = self.plan({"planner": {"id": "nope"}})
        self.assertEqual(status, 400)
        self.assertIn("nope", payload["error"])

    def test_spiral_planner_is_reachable_over_http(self) -> None:
        status, payload, _ = self.plan(
            {
                "region": {"shape": "circle", "parameters": {"diameter_mm": 80.0}},
                "planner": {"id": "spiral", "parameters": {"stepover_mm": 6.0}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["toolpath"]["planner"], "spiral")
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 1)
        # 只下一次刀、抬一次刀。
        labels = [move["label"] for move in payload["toolpath"]["moves"]
                  if move["kind"] == "rapid"]
        self.assertEqual(labels.count("下刀"), 1)
        self.assertEqual(labels.count("抬刀"), 1)

    def test_spiral_feed_ramp_is_visible_in_the_response(self) -> None:
        _, payload, _ = self.plan(
            {
                "planner": {
                    "id": "spiral",
                    "parameters": {
                        "stepover_mm": 6.0,
                        "ramp_feed": True,
                        "feed_mm_per_min": 800.0,
                        "center_feed_mm_per_min": 300.0,
                    },
                }
            }
        )
        feeds = [move["feed_mm_per_min"] for move in payload["toolpath"]["moves"]
                 if move["kind"] == "cut"]
        self.assertGreater(len(set(feeds)), 1, "勾选进给变化后每圈进给应当不同")
        self.assertAlmostEqual(max(feeds), 800.0, places=6)
        self.assertLess(min(feeds), max(feeds))

    def test_unknown_region_shape_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"region": {"shape": "hexagon"}})
        self.assertEqual(status, 400)

    def test_rounded_rectangle_can_be_planned(self) -> None:
        status, payload, _ = self.plan(
            {
                "region": {
                    "shape": "rounded_rect",
                    "parameters": {
                        "width_mm": 80.0,
                        "height_mm": 60.0,
                        "corner_radius_mm": 12.0,
                    },
                }
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["request"]["region"]["shape"], "rounded_rect")
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)
        self.assertTrue(payload["region"]["boundary"])

    def test_every_region_shape_returns_a_usable_boundary(self) -> None:
        # 毛坯是按这个 boundary 拉伸出来的，所以每个形状都必须给出非空、逆时针的轮廓。
        for shape, parameters in (
            ("square", {"side_mm": 80.0}),
            ("circle", {"diameter_mm": 80.0}),
            ("ellipse", {"semi_major_mm": 60.0, "semi_minor_mm": 40.0}),
            ("rounded_rect", {"width_mm": 80.0, "height_mm": 60.0, "corner_radius_mm": 12.0}),
        ):
            with self.subTest(shape=shape):
                status, payload, _ = self.plan(
                    {"region": {"shape": shape, "parameters": parameters}}
                )
                self.assertEqual(status, 200)
                boundary = payload["region"]["boundary"]
                self.assertGreaterEqual(len(boundary), 3)
                self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)
                signed = 0.0
                for index, (x, y, _) in enumerate(boundary):
                    nx, ny, _ = boundary[(index + 1) % len(boundary)]
                    signed += x * ny - nx * y
                self.assertGreater(signed * 0.5, 0.0, "边界必须是逆时针")

    def test_ellipse_can_be_planned(self) -> None:
        status, payload, _ = self.plan(
            {
                "region": {
                    "shape": "ellipse",
                    "parameters": {"semi_major_mm": 60.0, "semi_minor_mm": 40.0},
                }
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["request"]["region"]["shape"], "ellipse")
        [[x_min, x_max], [y_min, y_max]] = payload["region"]["bounds_mm"]
        self.assertAlmostEqual(x_max, 60.0, places=6)
        self.assertAlmostEqual(y_max, 40.0, delta=0.05)

    def test_out_of_range_corner_radius_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan(
            {"region": {"shape": "rounded_rect", "parameters": {"corner_radius_mm": 500.0}}}
        )
        self.assertEqual(status, 400)
        self.assertIn("corner_radius_mm", payload["error"])

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

    def test_csv_export_returns_a_point_table(self) -> None:
        status, body, headers = self.post("/api/export/csv", {})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertTrue(headers["Content-Disposition"].endswith('.csv"'))
        lines = body.decode("utf-8").splitlines()
        self.assertTrue(lines[0].startswith("move_index,move_kind,"))
        self.assertIn("x_mm", lines[0])
        self.assertGreater(len(lines), 2)

    def test_csv_can_exclude_rapid_moves(self) -> None:
        _, with_rapid, _ = self.post("/api/export/csv", {})
        _, without, _ = self.post("/api/export/csv", {"export": {"exclude_rapid": True}})
        self.assertLess(len(without.splitlines()), len(with_rapid.splitlines()))
        self.assertNotIn(b"rapid", without)

    def test_json_export_is_a_faithful_snapshot(self) -> None:
        status, body, headers = self.post(
            "/api/export/json",
            {"region": {"shape": "ellipse",
                        "parameters": {"semi_major_mm": 60.0, "semi_minor_mm": 40.0}},
             "planner": {"id": "spiral"}},
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body)
        self.assertEqual(payload["format"], "toolpath-lab/toolpath")
        self.assertEqual(payload["planner"]["id"], "spiral")
        self.assertEqual(payload["region"]["id"], "ellipse")
        self.assertGreater(len(payload["moves"]), 1)
        self.assertEqual(len(payload["moves"][0]["points"][0]), 3)
        self.assertIn("statistics", payload)
        self.assertIn("request", payload)

    def test_every_registered_export_format_has_a_route(self) -> None:
        # EXPORT_FORMATS 是格式登记表；这里保证登记的格式都真的能导出，
        # 免得加了格式忘了写分支却一直没人发现。
        from toolpath_lab.export import EXPORT_FORMATS

        for fmt, (name, extension) in EXPORT_FORMATS.items():
            with self.subTest(format=fmt):
                status, body, headers = self.post(f"/api/export/{fmt}", {})
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertTrue(headers["Content-Disposition"].endswith(f'{extension}"'))
                self.assertEqual(name, fmt)

    def test_unknown_export_format_is_a_404(self) -> None:
        for kind in ("step", "dxf", "nc"):
            with self.subTest(kind=kind):
                status, _, _ = self.post(f"/api/export/{kind}", {})
                self.assertEqual(status, 404)

    def test_body_is_drained_even_for_unknown_routes(self) -> None:
        # 回归测试：POST 到不存在的路由时如果不把请求体读完，HTTP/1.1 保持连接下
        # 残余字节会被下一个请求当成开头，整条连接就此错乱（表现为客户端
        # ConnectionAbortedError / ConnectionResetError）。这里在同一条连接上
        # 先打一个未知路由、再打一个正常路由，确保连接仍然可用。
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", "/api/export/step",
                               body=b'{"tool":{"diameter_mm":6}}',
                               headers={"Content-Type": "application/json"})
            first = connection.getresponse()
            first.read()
            self.assertEqual(first.status, 404)

            connection.request("POST", "/api/plan", body=b"{}",
                               headers={"Content-Type": "application/json"})
            second = connection.getresponse()
            body = json.loads(second.read())
            self.assertEqual(second.status, 200)
            self.assertTrue(body["ok"])
        finally:
            connection.close()

    def test_body_is_drained_even_for_a_method_not_allowed(self) -> None:
        # 静态路径上的非 GET 请求走 405 分支，同样必须先读完 body。
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", "/style.css", body=b'{"ignored":true}',
                               headers={"Content-Type": "application/json"})
            first = connection.getresponse()
            first.read()
            self.assertEqual(first.status, 405)

            connection.request("GET", "/api/health")
            second = connection.getresponse()
            self.assertEqual(second.status, 200)
            second.read()
        finally:
            connection.close()

    def test_oversized_body_is_refused(self) -> None:
        status, payload, _ = self.plan({"padding": "x" * (5 * 1024 * 1024)})
        self.assertEqual(status, 400)
        self.assertIn("上限", payload["error"])


if __name__ == "__main__":
    unittest.main()
