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
        for path in ("/js/main.js", "/js/viewport.js", "/js/model.js", "/js/orientation.js", "/style.css",
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
    def test_tool_library_is_validated_and_declared(self) -> None:
        _, body, _ = self.get("/api/catalog")
        tool = json.loads(body)["tool"]
        self.assertEqual(len(tool["library"]), 8)
        self.assertEqual(tool["preset_selector"]["kind"], "choice")
        self.assertEqual(tool["preset_selector"]["choices"][0]["value"], "custom")

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
            ["raster", "crosshatch", "five_axis", "adaptive_scallop", "five_axis_adaptive", "contour", "spiral"],
        )
        self.assertEqual(
            sorted(item["id"] for item in payload["regions"]["shapes"]),
            ["circle", "ellipse", "square"],
        )
        self.assertEqual(
            [item["id"] for item in payload["surfaces"]["types"]],
            ["flat", "freeform", "saddle", "dome", "radial_ripple", "composite"],
        )
        self.assertNotIn("presets", payload)
        self.assertEqual(
            [item["key"] for item in payload["tool"]["parameters"]],
            ["kind", "diameter_mm", "length_mm", "nose_radius_mm"],
        )

    def test_catalog_reports_the_fixed_settings(self) -> None:
        _, body, _ = self.get("/api/catalog")
        fixed = json.loads(body)["fixed"]
        self.assertEqual(fixed["safe_height_mm"], 5.0)
        self.assertEqual(fixed["rapid_feed_mm_per_min"], 5000.0)

    def test_enabled_tool_kinds_are_published(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        self.assertEqual([item["disabled"] for item in kinds], [False, False, False])

    def test_unknown_endpoint(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.get("/api/nope")
        self.assertEqual(context.exception.code, 404)


class PlanTests(ApiTestCase):
    def test_contour_and_spiral_generate_timeline_and_coverage(self) -> None:
        for planner in ("contour", "spiral"):
            with self.subTest(planner=planner):
                status, data, _ = self.plan({"planner": {"id": planner}, "region": {"shape": "circle"}})
                self.assertEqual(status, 200)
                self.assertEqual(data["toolpath"]["planner"], planner)
                self.assertGreater(data["toolpath"]["metadata"]["contour"]["ring_count"], 1)
                self.assertTrue(data["timeline"]["positions"])
                self.assertTrue(data["coverage"]["available"])

    def test_new_strategies_reject_concave_selection_without_crashing(self) -> None:
        body = {"region": {"shape": "polygon", "parameters": {
            "boundary": [[0, 0], [20, 0], [20, 8], [8, 8], [8, 20], [0, 20]]}}}
        for planner in ("contour", "spiral"):
            body["planner"] = {"id": planner}
            status, data, _ = self.plan(body)
            self.assertEqual(status, 422)
            self.assertIn("凹多边形", data["error"])

    def test_plan_includes_projected_finish_coverage(self) -> None:
        status, data, _ = self.plan({"planner": {"id": "raster", "parameters": {"stepover_mm": 12}}})
        self.assertEqual(status, 200)
        coverage = data["coverage"]
        self.assertTrue(coverage["estimate_only"])
        self.assertEqual(coverage["scope"], "complete_finishing_path")
        self.assertGreater(coverage["uncovered_area_mm2"], 1000)
        grid = coverage["grid"]
        self.assertEqual(len(grid["uncovered_mask"]), grid["nx"] * grid["ny"])

    def test_optional_roughing_is_catalogued_and_returned_before_finish(self) -> None:
        _, body, _ = self.get("/api/catalog")
        settings = json.loads(body)["roughing"]
        self.assertFalse(settings["defaults"]["enabled"])
        self.assertEqual([s["key"] for s in settings["parameters"]], ["enabled", "depth_mm", "allowance_mm"])
        status, payload, _ = self.plan({"tool": {"kind": "ball"},
            "surface": {"type": "freeform"}, "roughing": {"enabled": True}})
        self.assertEqual(status, 200)
        rough = payload["toolpath"]["metadata"]["roughing"]
        self.assertEqual(rough["layer_count"], 5)
        self.assertGreater(rough["finish_start_move_index"], 0)
        self.assertTrue(payload["request"]["roughing"]["enabled"])
        self.assertTrue(payload["toolpath"]["moves"][0]["label"].startswith("粗加工"))
        self.assertEqual(payload["tool"]["cutting_length_mm"], 6)

    def test_roughing_off_and_invalid_depth(self) -> None:
        status, payload, _ = self.plan({"roughing": {"enabled": False}})
        self.assertEqual(status, 200)
        self.assertNotIn("roughing", payload["toolpath"].get("metadata", {}))
        status, payload, _ = self.plan({"roughing": {"enabled": True, "depth_mm": 0}})
        self.assertEqual(status, 400)
        self.assertIn("depth_mm", payload["error"])

    def test_roughing_gcode_export_contains_both_stages(self) -> None:
        status, body, _ = self.post("/api/export/gcode", {"roughing": {"enabled": True}})
        self.assertEqual(status, 200)
        self.assertIn("先分层粗加工", body.decode("utf-8"))

    def test_minimal_request_uses_defaults(self) -> None:
        status, payload, _ = self.plan({})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        statistics = payload["toolpath"]["statistics"]
        self.assertGreater(statistics["pass_count"], 0)
        self.assertIsNotNone(payload["timeline"])
        self.assertTrue(payload["region"]["boundary"])
        self.assertEqual(payload["surface"]["id"], "flat")

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

    def test_imported_polygon_region_is_plannable(self) -> None:
        status, payload, _ = self.plan({
            "region": {"shape": "polygon", "parameters": {
                "boundary": [[-30, -20], [30, -20], [30, 20], [-30, 20]],
            }},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["region"]["id"], "polygon")
        self.assertEqual(payload["request"]["region"]["shape"], "polygon")
        self.assertEqual(len(payload["region"]["boundary"]), 4)
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)

    def test_imported_polygon_region_rejects_degenerate_boundary(self) -> None:
        status, payload, _ = self.plan({
            "region": {"shape": "polygon", "parameters": {
                "boundary": [[0, 0], [1, 1], [2, 2]],
            }},
        })
        self.assertEqual(status, 400)
        self.assertIn("面积", payload["error"])

    def test_ellipse_and_bull_tool_are_plannable(self) -> None:
        status, payload, _ = self.plan(
            {
                "tool": {
                    "kind": "bull",
                    "diameter_mm": 10.0,
                    "length_mm": 40.0,
                    "nose_radius_mm": 2.0,
                },
                "region": {
                    "shape": "ellipse",
                    "parameters": {"major_mm": 120.0, "minor_mm": 80.0, "rotation_deg": 25.0},
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["tool"]["kind"], "bull")
        self.assertEqual(payload["tool"]["nose_radius_mm"], 2.0)
        self.assertEqual(payload["region"]["id"], "ellipse")
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)

    def test_freeform_surface_generates_sampled_3d_path(self) -> None:
        status, payload, _ = self.plan({
            "surface": {"type": "freeform", "parameters": {
                "amplitude_mm": 6.0, "wavelength_x_mm": 40.0,
                "wavelength_y_mm": 60.0,
            }},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["surface"]["id"], "freeform")
        self.assertGreater(len(payload["surface"]["mesh"]["indices"]), 0)
        cuts = [move for move in payload["toolpath"]["moves"] if move["kind"] == "cut"]
        self.assertTrue(any(len(move["points"]) > 2 for move in cuts))
        self.assertTrue(any(abs(point[2]) > 0.1 for move in cuts for point in move["points"]))
        safe_z = payload["surface"]["height_bounds_mm"][1] + 5.0
        self.assertTrue(any(
            point[2] >= safe_z for move in payload["toolpath"]["moves"]
            if move["kind"] == "rapid" for point in move["points"]
        ))

    def test_five_axis_request_returns_tool_axes(self) -> None:
        status, payload, _ = self.plan({
            "surface": {"type": "freeform", "parameters": {"amplitude_mm": 3.0}},
            "planner": {"id": "five_axis", "parameters": {
                "stepover_mm": 8.0, "lead_deg": 10.0, "side_tilt_deg": 3.0,
            }},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["toolpath"]["axis_mode"], "five_axis")
        self.assertTrue(payload["timeline"]["tool_axes"])
        oriented_moves = [move for move in payload["toolpath"]["moves"] if "tool_axes" in move]
        self.assertTrue(oriented_moves)
        axis = oriented_moves[0]["tool_axes"][0]
        self.assertAlmostEqual(sum(value * value for value in axis), 1.0, places=4)

    def test_adaptive_scallop_request_returns_curvature_aware_path(self) -> None:
        status, payload, _ = self.plan({
            "tool": {"kind": "ball", "diameter_mm": 6.0, "length_mm": 30.0},
            "surface": {"type": "freeform", "parameters": {
                "amplitude_mm": 10.0, "wavelength_y_mm": 40.0,
            }},
            "planner": {"id": "adaptive_scallop", "parameters": {
                "target_scallop_mm": 0.2, "min_stepover_mm": 0.8,
                "max_stepover_mm": 6.0,
            }},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["toolpath"]["planner"], "adaptive_scallop")
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)
        self.assertTrue(any("目标残留高度" in note for note in payload["toolpath"]["notes"]))
        adaptive = payload["toolpath"]["metadata"]["adaptive"]
        self.assertEqual(adaptive["pass_count"], payload["toolpath"]["statistics"]["pass_count"])
        self.assertGreaterEqual(adaptive["max_stepover_mm"], adaptive["min_stepover_mm"])
        self.assertEqual(len(adaptive["stepover_profile_mm"]), adaptive["pass_count"])

    def test_five_axis_adaptive_combines_metadata_and_oriented_timeline(self) -> None:
        status, payload, _ = self.plan({
            "tool": {"kind": "ball"}, "region": {"shape": "square", "parameters": {"side_mm": 30}},
            "surface": {"type": "freeform", "parameters": {"amplitude_mm": 3}},
            "planner": {"id": "five_axis_adaptive", "parameters": {
                "target_scallop_mm": 0.1, "lead_deg": 15, "side_tilt_deg": 4,
            }}, "roughing": {"enabled": True},
        })
        self.assertEqual(status, 200)
        path = payload["toolpath"]
        self.assertEqual(path["planner"], "five_axis_adaptive")
        self.assertEqual(path["axis_mode"], "five_axis")
        for group in ["adaptive", "orientation_smoothing", "roughing", "five_axis_adaptive"]:
            self.assertIn(group, path["metadata"])
        self.assertTrue(payload["timeline"]["tool_axes"])
        self.assertTrue(path["metadata"]["five_axis_adaptive"]["estimate_only"])

    def test_five_axis_adaptive_rejects_non_ball_tools_and_invalid_settings(self) -> None:
        status, payload, _ = self.plan({"planner": {"id": "five_axis_adaptive"}})
        self.assertEqual(status, 422)
        self.assertIn("球头刀", payload["error"])
        status, _, _ = self.plan({"tool": {"kind": "ball"}, "planner": {"id": "five_axis_adaptive",
                                                  "parameters": {"max_angular_speed_deg_s": 0}}})
        self.assertEqual(status, 400)

    def test_plan_returns_independent_stock_simulation_spec(self) -> None:
        status, payload, _ = self.plan({})
        self.assertEqual(status, 200)
        self.assertIn("stock", payload)
        self.assertEqual(payload["stock"]["grid_shape"], [41, 41])
        self.assertGreater(payload["stock"]["initial_top_z_mm"], 0.0)

    def test_unknown_surface_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"surface": {"type": "unknown_surface"}})
        self.assertEqual(status, 400)
        self.assertIn("unknown_surface", payload["error"])

    def test_parametric_surfaces_generate_mesh_and_3d_path(self) -> None:
        for surface_id in ("saddle", "dome", "radial_ripple", "composite"):
            with self.subTest(surface_id=surface_id):
                status, payload, _ = self.plan({
                    "surface": {"type": surface_id},
                    "planner": {"id": "raster", "parameters": {"stepover_mm": 8.0}},
                })
                self.assertEqual(status, 200)
                self.assertEqual(payload["surface"]["id"], surface_id)
                self.assertGreater(len(payload["surface"]["mesh"]["indices"]), 0)
                cuts = [move for move in payload["toolpath"]["moves"] if move["kind"] == "cut"]
                self.assertTrue(any(
                    max(point[2] for point in move["points"])
                    - min(point[2] for point in move["points"]) > 0.01
                    for move in cuts
                ))

    def test_parameters_are_echoed_back_normalised(self) -> None:
        _, payload, _ = self.plan({"planner": {"parameters": {"stepover_mm": 8}}})
        parameters = payload["request"]["planner"]["parameters"]
        self.assertEqual(parameters["stepover_mm"], 8.0)
        self.assertEqual(parameters["mode"], "zigzag")
        self.assertEqual(parameters["feed_mm_per_min"], 600.0)

    def test_unknown_planner_is_a_bad_request(self) -> None:
        status, payload, _ = self.plan({"planner": {"id": "does_not_exist"}})
        self.assertEqual(status, 400)
        self.assertIn("does_not_exist", payload["error"])

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


if __name__ == "__main__":
    unittest.main()
