"""HTTP 层：路由、校验、错误码与静态前端。"""

from __future__ import annotations

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
            ["raster", "surface_finish"],
        )
        self.assertEqual(
            sorted(item["id"] for item in payload["regions"]["shapes"]), ["circle", "square"]
        )
        self.assertEqual(
            [item["id"] for item in payload["surfaces"]["list"]],
            ["flat", "incline", "cylinder", "dome"],
        )
        self.assertNotIn("presets", payload)
        self.assertEqual([item["key"] for item in payload["tool"]["parameters"]],
                         ["kind", "diameter_mm", "corner_radius_mm", "length_mm"])

    def test_catalog_reports_the_fixed_settings(self) -> None:
        _, body, _ = self.get("/api/catalog")
        fixed = json.loads(body)["fixed"]
        self.assertEqual(fixed["safe_height_mm"], 5.0)
        self.assertEqual(fixed["rapid_feed_mm_per_min"], 5000.0)

    def test_every_tool_kind_is_published_as_selectable(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        self.assertEqual([item["value"] for item in kinds], ["flat", "ball", "bull"])
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


class SurfacePlanTests(ApiTestCase):
    def test_default_request_reports_the_reference_plane(self) -> None:
        status, payload, _ = self.plan({})
        self.assertEqual(status, 200)
        self.assertEqual(payload["surface"]["id"], "flat")
        self.assertEqual(payload["request"]["surface"]["type"], "flat")
        # 平面加工面不需要额外的网格：三维视图直接用工件实体表示。
        self.assertNotIn("mesh", payload["surface"])
        self.assertEqual(
            [point[2] for point in payload["region"]["boundary"]],
            [0.0] * len(payload["region"]["boundary"]),
        )

    def test_curved_surface_is_planned_and_meshed(self) -> None:
        status, payload, _ = self.plan(
            {
                "tool": {"kind": "ball", "diameter_mm": 8.0, "length_mm": 40.0},
                "region": {"shape": "square", "parameters": {"side_mm": 60.0}},
                "surface": {"type": "dome",
                            "parameters": {"radius_mm": 100.0, "crown_mm": 10.0}},
                "planner": {"id": "surface_finish",
                            "parameters": {"scallop_mm": 0.05, "max_stepover_mm": 4.0}},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["surface"]["id"], "dome")
        self.assertFalse(payload["surface"]["is_planar"])
        mesh = payload["surface"]["mesh"]
        self.assertEqual(mesh["resolution"], 64)
        self.assertEqual(len(mesh["z"]), 64)
        self.assertEqual(len(mesh["z"][0]), 64)
        # 区域轮廓被抬到曲面上，于是三维视图里的轮廓线贴着加工面。
        heights = [point[2] for point in payload["region"]["boundary"]]
        self.assertGreater(min(heights), 0.0)
        # 刀尖母线随响应发出，前端据此旋成球头刀实体。
        self.assertGreaterEqual(len(payload["tool"]["profile"]), 2)
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)

    def test_incline_surface_only_needs_two_points_per_pass(self) -> None:
        status, payload, _ = self.plan(
            {
                "tool": {"kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0,
                         "corner_radius_mm": 2.0},
                "region": {"shape": "square", "parameters": {"side_mm": 40.0}},
                "surface": {"type": "incline", "parameters": {"angle_deg": 15.0}},
            }
        )
        self.assertEqual(status, 200)
        cuts = [move for move in payload["toolpath"]["moves"] if move["kind"] == "cut"]
        self.assertTrue(cuts)
        self.assertTrue(all(len(move["points"]) == 2 for move in cuts))
        self.assertEqual(payload["tool"]["corner_radius_mm"], 2.0)

    def test_unknown_surface_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"surface": {"type": "torus"}})
        self.assertEqual(status, 400)
        self.assertIn("torus", payload["error"])

    def test_surface_smaller_than_the_region_is_unprocessable(self) -> None:
        status, payload, _ = self.plan(
            {
                "region": {"shape": "square", "parameters": {"side_mm": 120.0}},
                "surface": {"type": "dome",
                            "parameters": {"radius_mm": 50.0, "crown_mm": 5.0}},
            }
        )
        self.assertEqual(status, 422)
        self.assertTrue(payload["error"])


if __name__ == "__main__":
    unittest.main()
