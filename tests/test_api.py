"""HTTP layer: routing, validation, status codes and the static front end."""

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
        # Browsers request /favicon.ico directly; answer with the same PNG instead of an empty reply.
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
            ["raster", "contour", "adaptive_contour"],
        )
        self.assertEqual(
            sorted(item["id"] for item in payload["regions"]["shapes"]),
            ["circle", "dumbbell", "ellipse", "rectangle", "square", "triangle", "u_shape"],
        )
        self.assertNotIn("surfaces", payload)
        self.assertNotIn("presets", payload)
        self.assertEqual([item["key"] for item in payload["tool"]["parameters"]],
                         ["kind", "diameter_mm", "length_mm"])

    def test_catalog_reports_the_motion_parameters_instead_of_fixed_values(self) -> None:
        # Safe height and rapid feed used to sit in the catalogue's "fixed" section for display only;
        # they are strategy parameters now.
        _, body, _ = self.get("/api/catalog")
        payload = json.loads(body)
        self.assertNotIn("fixed", payload)
        defaults = payload["planners"]["defaults"]
        self.assertEqual(defaults["safe_height_mm"], 5.0)
        self.assertEqual(defaults["rapid_feed_mm_per_min"], 5000.0)
        raster = payload["planners"]["list"][0]["parameters"]
        keys = [item["key"] for item in raster]
        self.assertIn("safe_height_mm", keys)
        self.assertIn("rapid_feed_mm_per_min", keys)
        self.assertIn("boundary_mode", keys)

    def test_disabled_tool_kinds_are_published(self) -> None:
        _, body, _ = self.get("/api/catalog")
        kinds = json.loads(body)["tool"]["parameters"][0]["choices"]
        self.assertEqual([item["disabled"] for item in kinds], [False, True, True])

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
        # The parameters themselves are legal (D60 is within range) but the footprint radius exceeds
        # the width of the region: that is infeasible geometry, not a parameter error.
        status, payload, _ = self.plan(
            {"tool": {"diameter_mm": 60.0}, "region": {"shape": "square", "parameters": {"side_mm": 40.0}}}
        )
        self.assertEqual(status, 422)
        self.assertTrue(payload["error"])

    def test_a_pending_tool_kind_is_a_bad_request(self) -> None:
        # Tool kinds marked "to be extended" must be refused by the API as well, never quietly
        # computed as if they were flat mills.
        for kind in ("ball", "bull"):
            with self.subTest(kind=kind):
                status, payload, _ = self.plan({"tool": {"kind": kind}})
                self.assertEqual(status, 400)
                self.assertIn("待拓展", payload["error"])

    def test_the_plan_response_carries_the_coverage_analysis(self) -> None:
        _, payload, _ = self.plan({})
        coverage = payload["coverage"]
        self.assertIsNotNone(coverage)
        self.assertAlmostEqual(coverage["ratio"], 1.0, delta=0.05)
        self.assertIn("uncut_area_mm2", coverage)
        self.assertIn("patches", coverage)
        # Rectangles for the 3D overlay: a default request leaves little uncut, but something at the
        # boundary does remain.
        self.assertTrue(coverage["uncut_rects"])
        self.assertIsInstance(coverage["uncut_rects_truncated"], bool)
        self.assertEqual(len(coverage["uncut_rects"][0]), 4)

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

    def test_csv_download(self) -> None:
        status, body, headers = self.post("/api/export/csv", {})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertTrue(headers["Content-Disposition"].endswith('.csv"'))
        lines = body.decode("utf-8").splitlines()
        self.assertEqual(
            lines[0], "move_index,pass_index,kind,feed_mm_per_min,point_index,x_mm,y_mm,z_mm"
        )
        # Rows = tool points + header; a default request (80 square, D6, stepover 6) has 58 points.
        self.assertEqual(len(lines), 59)
        self.assertTrue(all(len(line.split(",")) == 8 for line in lines))

    def test_csv_and_gcode_describe_the_same_toolpath(self) -> None:
        _, plan_body, _ = self.post("/api/plan", {})
        point_count = json.loads(plan_body)["toolpath"]["statistics"]["point_count"]
        _, csv_body, _ = self.post("/api/export/csv", {})
        self.assertEqual(len(csv_body.decode("utf-8").splitlines()) - 1, point_count)

    def test_other_formats_are_gone(self) -> None:
        for kind in ("json", "step", "dxf"):
            with self.subTest(kind=kind):
                status, _, _ = self.post(f"/api/export/{kind}", {})
                self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
