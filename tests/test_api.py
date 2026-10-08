"""HTTP 层：路由、校验、错误码与静态前端。"""

from __future__ import annotations

import base64
import json
import struct
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

    def send(self, method, path, payload=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
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
            sorted(item["id"] for item in payload["regions"]["shapes"]),
            ["circle", "model", "square"],
        )
        self.assertEqual(
            [item["id"] for item in payload["surfaces"]["list"]],
            ["flat", "slope", "wave", "model"],
        )
        self.assertEqual(payload["surfaces"]["default_id"], "flat")
        self.assertEqual(
            [item["id"] for item in payload["stocks"]["list"]], ["none", "model"]
        )
        self.assertEqual(payload["stocks"]["default_id"], "none")
        self.assertIn("models", payload)
        self.assertEqual(payload["models"]["default_id"], "")
        self.assertNotIn("presets", payload)
        self.assertEqual([item["key"] for item in payload["tool"]["parameters"]],
                         ["kind", "diameter_mm", "length_mm"])

    def test_model_capabilities_are_published(self) -> None:
        _, body, _ = self.get("/api/catalog")
        payload = json.loads(body)
        surface = {item["id"]: item for item in payload["surfaces"]["list"]}["model"]
        self.assertEqual(
            [item["key"] for item in surface["parameters"]],
            ["resolution_mm", "pick", "z_offset_mm"],
        )
        region = {item["id"]: item for item in payload["regions"]["shapes"]}["model"]
        self.assertEqual(
            [item["key"] for item in region["parameters"]], ["outline", "margin_mm"]
        )
        self.assertEqual(payload["models"]["default_id"], "")

    def test_catalog_reports_the_fixed_settings(self) -> None:
        _, body, _ = self.get("/api/catalog")
        fixed = json.loads(body)["fixed"]
        self.assertEqual(fixed["safe_height_mm"], 5.0)
        self.assertEqual(fixed["rapid_feed_mm_per_min"], 5000.0)

    def test_tool_kind_availability_is_published(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        # 平底刀与球头刀可用，圆鼻刀仍是"待拓展"。
        self.assertEqual([item["disabled"] for item in kinds], [False, False, True])

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

    def test_ball_tool_is_planned_and_published_for_display(self) -> None:
        status, payload, _ = self.plan(
            {"tool": {"kind": "ball", "diameter_mm": 6.0, "length_mm": 30.0}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["tool"]["kind"], "ball")
        self.assertEqual(payload["tool"]["kind_label"], "球头刀 Ball nose")
        # 只有刀尖接触加工面，所以不走"内缩一个半径"那套
        self.assertEqual(payload["tool"]["footprint_radius_mm"], 0.0)
        self.assertEqual([item["name"] for item in payload["tool"]["segments"]],
                         ["cutting", "shank"])
        levels = {
            round(point[1], 6)
            for move in payload["toolpath"]["moves"] if move["kind"] == "cut"
            for point in move["points"]
        }
        self.assertIn(-40.0, levels)
        self.assertIn(40.0, levels)
        self.assertTrue(any("球头刀" in note for note in payload["toolpath"]["notes"]))

    def test_ball_shorter_than_its_hemisphere_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan(
            {"tool": {"kind": "ball", "diameter_mm": 40.0, "length_mm": 10.0}}
        )
        self.assertEqual(status, 400)
        self.assertIn("半球", payload["error"])

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


class ModelApiTests(ApiTestCase):
    """导入模型：上传 → 引用 → 规划 → 删除。"""

    def setUp(self) -> None:
        self.model_ids = []

    def tearDown(self) -> None:
        for model_id in self.model_ids:
            self.send("DELETE", f"/api/models/{model_id}")

    def upload(self, mesh=None, name="plate.stl"):
        mesh = mesh if mesh is not None else plate_mesh()
        payload = {
            "name": name,
            "data_base64": base64.b64encode(mesh_to_binary_stl(mesh)).decode("ascii"),
        }
        status, body, _ = self.post("/api/models", payload)
        uploaded = json.loads(body)
        if status == 201:
            self.model_ids.append(uploaded["model"]["id"])
        return status, uploaded

    def test_upload_lists_and_deletes_a_model(self) -> None:
        status, payload = self.upload()
        self.assertEqual(status, 201)
        model = payload["model"]
        self.assertEqual(model["triangle_count"], 4)
        self.assertEqual(model["size_mm"], [60.0, 60.0, 10.0])

        _, body, _ = self.send("GET", "/api/models")
        listing = json.loads(body)
        self.assertEqual([item["id"] for item in listing["models"]], [model["id"]])
        self.assertEqual(listing["default_id"], model["id"])

        status, body, _ = self.send("GET", f"/api/models/{model['id']}")
        detail = json.loads(body)["model"]
        self.assertEqual(detail["id"], model["id"])
        self.assertEqual(len(detail["positions"]), 4 * 3 * 3)
        self.assertFalse(detail["simplified"])

        status, _, _ = self.send("DELETE", f"/api/models/{model['id']}")
        self.assertEqual(status, 200)
        self.model_ids = []
        status, _, _ = self.send("GET", f"/api/models/{model['id']}")
        self.assertEqual(status, 400)

    def test_upload_rejects_broken_payloads(self) -> None:
        status, payload, _ = self.post("/api/models", {"name": "x.stl"})
        self.assertEqual(status, 400)
        self.assertIn("data_base64", payload["error"])

        status, payload, _ = self.post(
            "/api/models", {"name": "x.stl", "data_base64": "not base64!"}
        )
        self.assertEqual(status, 400)

        status, payload, _ = self.post(
            "/api/models",
            {"name": "x.stl", "data_base64": base64.b64encode(b"hello world").decode("ascii")},
        )
        self.assertEqual(status, 400)
        self.assertIn("STL", payload["error"])

    def test_unknown_model_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"model": {"id": "m-nope"}})
        self.assertEqual(status, 400)
        self.assertIn("m-nope", payload["error"])

    def test_model_capabilities_need_a_model(self) -> None:
        status, payload, _ = self.plan({"region": {"shape": "model"}})
        self.assertEqual(status, 400)
        self.assertIn("模型", payload["error"])

        status, payload, _ = self.plan({"surface": {"kind": "model"}})
        self.assertEqual(status, 400)

        status, payload, _ = self.plan({"stock": {"kind": "model"}})
        self.assertEqual(status, 400)
        self.assertIn("模型", payload["error"])

    def test_depth_without_a_stock_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan(
            {"planner": {"parameters": {"depth_per_pass_mm": 2.0}}}
        )
        self.assertEqual(status, 400)
        self.assertIn("毛坯", payload["error"])

    def test_stock_and_depth_produce_layers(self) -> None:
        _, uploaded = self.upload()
        model_id = uploaded["model"]["id"]
        status, payload, _ = self.plan(
            {
                "model": {"id": model_id},
                "region": {"shape": "model", "parameters": {"outline": "box"}},
                "surface": {"kind": "model"},
                "stock": {"kind": "model", "parameters": {"margin_top_mm": 5.0}},
                "planner": {"parameters": {
                    "stepover_mm": 10.0, "depth_per_pass_mm": 2.0, "sample_step_mm": 2.0,
                }},
            }
        )
        self.assertEqual(status, 200)
        stock = payload["stock"]
        self.assertEqual(stock["kind"], "model")
        self.assertTrue(stock["is_set"])
        self.assertEqual(stock["bounds_mm"][2], [0.0, 15.0])
        self.assertEqual(stock["top_mm"], 15.0)
        self.assertEqual(payload["request"]["stock"]["kind"], "model")

        # 毛坯顶面 15、模型顶面 10、切深 2 → 粗加工两层（Z=13、11）+ 精加工一刀（Z=10）
        levels = sorted({
            round(point[2], 6)
            for move in payload["toolpath"]["moves"]
            if move["kind"] == "cut" and move["pass_index"] >= 0
            for point in move["points"]
        })
        self.assertEqual(levels, [10.0, 11.0, 13.0])
        # 安全平面抬到毛坯顶面之上 5 mm
        rapid_z = [
            point[2] for move in payload["toolpath"]["moves"] if move["kind"] == "rapid"
            for point in move["points"]
        ]
        self.assertAlmostEqual(max(rapid_z), 20.0, places=6)
        self.assertTrue(any("分 2 层" in note for note in payload["toolpath"]["notes"]))
        self.assertTrue(any("包容体" in note for note in payload["toolpath"]["notes"]))

    def test_planning_on_a_model_surface_follows_the_mesh(self) -> None:
        _, uploaded = self.upload()
        model_id = uploaded["model"]["id"]
        status, payload, _ = self.plan(
            {
                "model": {"id": model_id},
                "region": {"shape": "model", "parameters": {"outline": "box"}},
                "surface": {"kind": "model", "parameters": {"resolution_mm": 1.0}},
                "planner": {"parameters": {"stepover_mm": 10.0, "sample_step_mm": 2.0}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["request"]["model"]["id"], model_id)
        self.assertEqual(payload["request"]["region"]["shape"], "model")
        self.assertEqual(payload["surface"]["kind"], "model")
        self.assertFalse(payload["surface"]["is_planar"])
        self.assertEqual(payload["model"]["id"], model_id)
        # 取面方式为 top：Z-map 上每个采样点都是顶面高度。
        self.assertEqual(payload["surface"]["height_field"]["z_range_mm"], [10.0, 10.0])
        self.assertEqual(payload["region"]["parameters"]["outline"], "box")

        # 模型顶面在 Z = 10：切削点必须都在这一层上，且同一刀不只两个点。
        cut_moves = [m for m in payload["toolpath"]["moves"] if m["kind"] == "cut"]
        self.assertGreater(len(cut_moves[0]["points"]), 2)
        for move in cut_moves:
            for point in move["points"]:
                self.assertAlmostEqual(point[2], 10.0, places=6)
        # 安全平面跟着抬到模型顶面之上。
        rapid_z = [
            point[2] for move in payload["toolpath"]["moves"] if move["kind"] == "rapid"
            for point in move["points"]
        ]
        self.assertAlmostEqual(max(rapid_z), 15.0, places=6)

    def test_bottom_pick_machines_the_lower_face(self) -> None:
        _, uploaded = self.upload()
        status, payload, _ = self.plan(
            {
                "model": {"id": uploaded["model"]["id"]},
                "region": {"shape": "model"},
                "surface": {"kind": "model", "parameters": {"pick": "bottom"}},
                "planner": {"parameters": {"stepover_mm": 12.0}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["surface"]["parameters"]["pick"], "bottom"
        )
        for move in payload["toolpath"]["moves"]:
            if move["kind"] == "cut":
                for point in move["points"]:
                    self.assertAlmostEqual(point[2], 0.0, places=6)

    def test_wave_surface_is_planned_without_a_model(self) -> None:
        status, payload, _ = self.plan(
            {
                "surface": {"kind": "wave", "parameters": {"amplitude_mm": 4.0}},
                "planner": {"parameters": {"stepover_mm": 8.0, "sample_step_mm": 2.0}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["surface"]["label"], "波浪面")
        heights = [
            point[2]
            for move in payload["toolpath"]["moves"] if move["kind"] == "cut"
            for point in move["points"]
        ]
        self.assertLessEqual(max(heights), 4.0 + 1e-6)
        self.assertGreaterEqual(min(heights), -4.0 - 1e-6)
        self.assertTrue(any("波浪面" in note for note in payload["toolpath"]["notes"]))

    def test_export_uses_the_curved_toolpath(self) -> None:
        status, body, _ = self.post(
            "/api/export/gcode",
            {
                "surface": {"kind": "slope", "parameters": {"tilt_deg": 30.0}},
                "planner": {"parameters": {"stepover_mm": 10.0, "sample_step_mm": 5.0}},
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("surface: slope", body.decode("utf-8"))


def plate_mesh():
    """测试用模型：60 × 60 × 10 的方板（顶面 Z=10、底面 Z=0）。"""

    half = 30.0
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    return [[(x, y, 10.0) for x, y in corners], [(x, y, 0.0) for x, y in corners]]


def mesh_to_binary_stl(polygons) -> bytes:
    """把一组多边形（每个都指向同一条闭合回路）写成最简单的二进制 STL。"""

    triangles = []
    for polygon in polygons:
        for index in range(1, len(polygon) - 1):
            triangles.append([polygon[0], polygon[index], polygon[index + 1]])
    header = b"toolpath-lab test".ljust(80, b"\0")
    body = bytearray(struct.pack("<I", len(triangles)))
    for triangle in triangles:
        body += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vertex in triangle:
            body += struct.pack("<3f", *vertex)
        body += struct.pack("<H", 0)
    return header + bytes(body)


if __name__ == "__main__":
    unittest.main()
