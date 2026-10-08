"""HTTP layer: routing, validation, status codes and the static front end."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from toolpath_lab.server.app import ToolpathLabHandler, create_server

#: A 60 x 40 outline as a drawing hands it over: counter-clockwise, with the first point repeated at
#: the end, so the region layer has to drop that closing point. Used by the imported-outline tests.
IMPORTED_POINTS = [[0.0, 0.0], [60.0, 0.0], [60.0, 40.0], [0.0, 40.0], [0.0, 0.0]]


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
                         ["kind", "diameter_mm", "length_mm", "corner_radius_mm"])

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

    def test_every_tool_kind_is_published_and_selectable(self) -> None:
        """Ball nose and bull nose tools are real tools now, so nothing is flagged as pending."""

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

    def test_an_imported_outline_is_planned_from_the_request_points(self) -> None:
        """`shape: "imported"` turns the point list from /api/import/dxf into a normal plan."""

        status, payload, _ = self.plan(
            {"region": {"shape": "imported", "points": IMPORTED_POINTS}}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["request"]["region"]["shape"], "imported")
        # The points are echoed back normalised (the closing point of the drawing is dropped), so the
        # UI can resend exactly the request it made.
        self.assertEqual(
            payload["request"]["region"]["parameters"]["points"],
            [[0.0, 0.0], [60.0, 0.0], [60.0, 40.0], [0.0, 40.0]],
        )
        self.assertEqual(payload["region"]["id"], "imported")
        self.assertEqual(payload["region"]["parameters"]["point_count"], 4)
        self.assertEqual(len(payload["region"]["boundary"]), 4)
        self.assertIsNotNone(payload["coverage"])
        self.assertIsNotNone(payload["removal"])
        self.assertGreater(payload["toolpath"]["statistics"]["pass_count"], 0)

    def test_an_imported_outline_plans_like_the_same_shape_built_from_parameters(self) -> None:
        """Cross check: the 60 x 40 outline gives the same path as the 60 x 40 rectangle region."""

        _, imported, _ = self.plan(
            {
                "region": {"shape": "imported", "points": IMPORTED_POINTS},
                "planner": {"id": "raster", "parameters": {"stepover_mm": 6.0}},
            }
        )
        _, rectangle, _ = self.plan(
            {
                "region": {"shape": "rectangle", "parameters": {"width_mm": 60.0, "height_mm": 40.0}},
                "planner": {"id": "raster", "parameters": {"stepover_mm": 6.0}},
            }
        )
        self.assertEqual(
            imported["toolpath"]["statistics"]["point_count"],
            rectangle["toolpath"]["statistics"]["point_count"],
        )
        self.assertAlmostEqual(
            imported["toolpath"]["statistics"]["cut_length_mm"],
            rectangle["toolpath"]["statistics"]["cut_length_mm"],
            places=6,
        )

    def test_the_export_header_summarises_an_imported_outline(self) -> None:
        status, body, _ = self.post(
            "/api/export/gcode", {"region": {"shape": "imported", "points": IMPORTED_POINTS}}
        )
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("region: imported - 4 points", text)
        # A drawing can hold thousands of points; the header states the count instead of the list.
        self.assertNotIn("points=", text)

    def test_an_imported_outline_needs_its_points(self) -> None:
        status, payload, _ = self.plan({"region": {"shape": "imported"}})
        self.assertEqual(status, 400)
        self.assertIn("points", payload["error"])

    def test_the_nested_parameter_form_is_accepted_too(self) -> None:
        # The UI sends {"shape": "imported", "parameters": {"points": [...]}}; the flat form and the
        # nested one both have to work, because everything else in the payload is nested.
        status, payload, _ = self.plan(
            {"region": {"shape": "imported", "parameters": {"points": IMPORTED_POINTS}}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["region"]["parameters"]["point_count"], 4)

    def test_a_degenerate_outline_is_not_a_server_error(self) -> None:
        # Three collinear points are a legal point list but have no area to machine: infeasible
        # geometry (422) is a decision, an internal error would be a bug.
        status, _, _ = self.plan(
            {"region": {"shape": "imported", "points": [[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]]}}
        )
        self.assertEqual(status, 422)

    def test_a_short_or_malformed_point_list_is_a_bad_request(self) -> None:
        cases = (
            [[0.0, 0.0], [40.0, 0.0]],  # fewer than three points
            [[0.0, 0.0], [40.0, 0.0], [20.0]],  # a point without two coordinates
            [[0.0, 0.0], [60.0, 0.0], [60.0, 40.0, 0.0]],  # the response's own [x, y, z] boundary
            [[0.0, 0.0], [40.0, 0.0], [20.0, float("nan")]],  # JSON carries NaN, a plan must not
            "0,0 40,0 20,30",  # a string is not a point list
        )
        for points in cases:
            with self.subTest(points=points):
                status, payload, _ = self.plan(
                    {"region": {"shape": "imported", "points": points}}
                )
                self.assertEqual(status, 400)
                self.assertTrue(payload["error"])

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

    def test_a_corner_radius_larger_than_the_tool_is_a_bad_request(self) -> None:
        # The tool geometry is validated in the parameter layer: a bull nose corner radius cannot
        # exceed the tool radius, so the API answers 400 instead of cutting with a nonsense tool.
        status, payload, _ = self.plan(
            {"tool": {"kind": "bull", "diameter_mm": 10.0, "corner_radius_mm": 6.0}}
        )
        self.assertEqual(status, 400)
        self.assertTrue(payload["error"])

    def test_the_plan_response_carries_the_material_removal(self) -> None:
        """The height map is part of the response: how deep the toolpath actually got."""

        _, payload, _ = self.plan({"planner": {"parameters": {"depth_mm": 2.0}}})
        removal = payload["removal"]
        self.assertIsNotNone(removal)
        self.assertAlmostEqual(removal["floor_mm"], -2.0, places=4)
        self.assertGreater(removal["removed_volume_mm3"], 0.0)
        self.assertGreaterEqual(removal["floor_ratio"], 0.9)
        self.assertLess(removal["remaining_volume_mm3"], removal["region_area_mm2"] * 2.0 * 0.1)

    def test_the_plan_response_carries_the_coverage_analysis(self) -> None:
        _, payload, _ = self.plan({})
        coverage = payload["coverage"]
        self.assertIsNotNone(coverage)
        self.assertAlmostEqual(coverage["ratio"], 1.0, delta=0.05)
        self.assertIn("uncut_area_mm2", coverage)
        self.assertIsNotNone(payload["removal"])
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
        comments = [line for line in lines if line.startswith("#")]
        rows = [line for line in lines if not line.startswith("#")]
        # The download explains itself: a comment block with the summary, the request and coverage.
        self.assertTrue(comments)
        self.assertTrue(any("coverage" in line for line in comments))
        self.assertEqual(
            rows[0], "move_index,pass_index,kind,feed_mm_per_min,point_index,x_mm,y_mm,z_mm"
        )
        # Data rows = tool points + header; a default request (80 square, D6, stepover 6) has 58.
        self.assertEqual(len(rows), 59)
        self.assertTrue(all(len(line.split(",")) == 8 for line in rows))

    def test_csv_and_gcode_describe_the_same_toolpath(self) -> None:
        _, plan_body, _ = self.post("/api/plan", {})
        point_count = json.loads(plan_body)["toolpath"]["statistics"]["point_count"]
        _, csv_body, _ = self.post("/api/export/csv", {})
        rows = [
            line for line in csv_body.decode("utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(len(rows) - 1, point_count)

    def test_dxf_import_returns_the_outline(self) -> None:
        """The import endpoint parses a drawing and hands the outlines back (nothing is stored)."""

        dxf = "\n".join([
            "0", "SECTION", "2", "ENTITIES", "0", "LWPOLYLINE", "8", "cut", "70", "1",
            "10", "0", "20", "0", "10", "40", "20", "0",
            "10", "40", "20", "40", "10", "0", "20", "40",
            "0", "CIRCLE", "10", "20", "20", "20", "40", "5",
            "0", "ENDSEC", "0", "EOF", "",
        ])
        status, body, _ = self.post("/api/import/dxf", {"text": dxf})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["outlines"]), 1)
        outline = payload["outlines"][0]
        self.assertTrue(outline["closed"])
        self.assertEqual(outline["point_count"], 4)
        self.assertEqual(outline["layer"], "cut")
        self.assertEqual(payload["skipped"], ["CIRCLE"])
        self.assertTrue(payload["warnings"])

    def test_dxf_import_of_a_useless_file_is_a_bad_request(self) -> None:
        status, _, _ = self.post("/api/import/dxf", {"text": "not a drawing at all"})
        self.assertEqual(status, 400)

    def test_other_formats_are_gone(self) -> None:
        for kind in ("json", "step", "dxf"):
            with self.subTest(kind=kind):
                status, _, _ = self.post(f"/api/export/{kind}", {})
                self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
