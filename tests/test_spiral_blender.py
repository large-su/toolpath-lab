"""Geometric, timing, exchange and HTTP regression tests for the course extension."""
import io
import json
from math import pi
import unittest
from unittest.mock import patch
import zipfile
import numpy as np
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan
from toolpath_lab.simulation import build_timeline
from toolpath_lab.export.blender import blender_bundle
from toolpath_lab.server.comparison import compare_strategies, comparison_csv
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan
from tests.test_api import ApiTestCase


def demo_request(**options):
    return {"tool": {"diameter_mm": 6, "length_mm": 30},
            "region": {"shape": "circle", "parameters": {"diameter_mm": 80}},
            "planner": {"id": "spiral", "parameters": {"stepover_mm": 3,
                        "feed_mm_per_min": 800, **options}}}


def nearest_distances(probes, points):
    starts, edges = points[:-1], np.diff(points, axis=0)
    squared = np.sum(edges * edges, axis=1)
    result = []
    for batch in np.array_split(probes, max(1, len(probes) // 32)):
        offsets = batch[:, None, :] - starts[None, :, :]
        ratios = np.clip(np.sum(offsets * edges, axis=2) / np.maximum(squared, 1e-20), 0, 1)
        closest = offsets - ratios[:, :, None] * edges
        result.extend(np.sqrt(np.sum(closest * closest, axis=2)).min(axis=1))
    return np.asarray(result)


class SpiralTests(unittest.TestCase):
    def test_center_outer_ring_and_continuity(self):
        result = execute_plan(PlanRequest.from_payload(demo_request()))
        moves = result.toolpath.moves
        self.assertEqual([m.kind for m in moves], [MoveKind.RAPID, MoveKind.CUT, MoveKind.RAPID])
        points = moves[1].points
        np.testing.assert_allclose(points[-1], [0, 0, 0], atol=1e-12)
        radii = np.linalg.norm(points[:, :2], axis=1)
        self.assertLessEqual(radii.max(), 37 + 1e-9)
        ring = points[np.abs(radii - 37) < 1e-9]
        self.assertGreater(len(ring), 360)
        np.testing.assert_allclose(ring[0], ring[-1], atol=1e-12)
        for previous, current in zip(moves, moves[1:]):
            np.testing.assert_allclose(previous.points[-1], current.points[0])

    def test_directions_reverse_the_same_geometry(self):
        inward = execute_plan(PlanRequest.from_payload(demo_request())).toolpath.moves[1].points
        outward = execute_plan(PlanRequest.from_payload(demo_request(radial_direction="outward"))).toolpath.moves[1].points
        np.testing.assert_array_equal(inward, outward[::-1])

    def test_spacing_and_swept_coverage(self):
        for pitch in (3, 6):
            result = execute_plan(PlanRequest.from_payload(demo_request(stepover_mm=pitch)))
            points = result.toolpath.moves[1].points[:, :2]
            self.assertLessEqual(np.linalg.norm(np.diff(points, axis=0), axis=1).max(), 0.5 + 1e-9)
            radial = np.linspace(0, 40, 41)
            angles = np.linspace(0, 2 * pi, 64, endpoint=False)
            probes = np.array([[r * np.cos(a), r * np.sin(a)] for r in radial for a in angles])
            self.assertLessEqual(nearest_distances(probes, points).max(), 3.01)

    def test_small_and_near_fitting_tool(self):
        for size, diameter, pitch in ((12, 6, 2), (6.01, 6, 3), (10, 1, 0.5)):
            result = run_plan(planner_id="spiral", tool=Tool(diameter_mm=diameter),
                              region=build_region("circle", {"diameter_mm": size}),
                              parameters={"stepover_mm": pitch})
            self.assertGreater(result.toolpath.estimated_time_s, 0)
            self.assertTrue(np.all(np.isfinite(result.toolpath.moves[1].points)))

    def test_invalid_geometry(self):
        for request in (demo_request(stepover_mm=7),
                        {**demo_request(), "region": {"shape": "square"}},
                        {**demo_request(), "tool": {"diameter_mm": 80}},
                        {**demo_request(), "tool": {"kind": "ball"}}):
            with self.subTest(request=request), self.assertRaises(PlanningError):
                execute_plan(PlanRequest.from_payload(request))

    def test_point_limit_is_actionable(self):
        with patch("toolpath_lab.planning.spiral.MAX_POINTS", 100), self.assertRaisesRegex(PlanningError, "刀点"):
            execute_plan(PlanRequest.from_payload(demo_request()))


class FidelityTests(unittest.TestCase):
    def test_bent_move_retains_corner_even_under_tiny_budget(self):
        move = Move(MoveKind.RAPID, np.array([[0, 0, 0], [0, 0, 5], [10, 0, 5], [10, 0, 0]]), 600)
        timeline = build_timeline(Toolpath((move,)), max_samples=2)
        self.assertAlmostEqual(timeline.duration_s, 2)
        np.testing.assert_array_equal(timeline.positions, move.points)
        np.testing.assert_allclose(timeline.state_at(0.75).position, [2.5, 0, 5])

    def test_curve_duration_and_payload_precision(self):
        result = execute_plan(PlanRequest.from_payload(demo_request()), max_samples=2)
        self.assertAlmostEqual(result.timeline.duration_s, result.toolpath.estimated_time_s, places=10)
        self.assertGreater(result.timeline.sample_count, 1000)
        payload = result.timeline.to_payload()
        self.assertLess(np.max(np.abs(np.asarray(payload["positions"]) - result.timeline.positions)), 1e-6)

    def test_zero_length_move(self):
        move = Move(MoveKind.CUT, np.zeros((3, 3)), 600)
        timeline = build_timeline(Toolpath((move,)))
        self.assertEqual(timeline.duration_s, 0)
        self.assertEqual(timeline.sample_count, 1)

    def test_kind_at_shared_boundary_is_outgoing(self):
        a = Move(MoveKind.RAPID, np.array([[0, 0, 5], [0, 0, 0]]), 600)
        b = Move(MoveKind.CUT, np.array([[0, 0, 0], [10, 0, 0]]), 600)
        timeline = build_timeline(Toolpath((a, b)), max_samples=3)
        self.assertEqual(timeline.state_at(0.5).kind, "cut")


class ExchangeTests(unittest.TestCase):
    def test_bundle_roundtrip_and_defaults(self):
        result = execute_plan(PlanRequest.from_payload(demo_request()))
        blob = blender_bundle(result.request.tool, result.request.region, result.toolpath,
                              result.timeline.to_payload())
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertIn("build_scene.py", archive.namelist())
            self.assertIn("01_create_scene.bat", archive.namelist())
            manifest = json.loads(archive.read("scene.json"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["settings"]["fps"], 30)
        self.assertEqual(manifest["settings"]["playback_speed"], 1)
        self.assertEqual(manifest["position_reference"], "tool_tip")
        self.assertAlmostEqual(manifest["timeline"]["duration_s"], result.toolpath.estimated_time_s)

    def test_square_raster_bundle(self):
        result = execute_plan(PlanRequest.from_payload({}))
        blob = blender_bundle(result.request.tool, result.request.region, result.toolpath,
                              result.timeline.to_payload())
        self.assertTrue(blob.startswith(b"PK"))

    def test_comparison_common_parameters_and_csv(self):
        result = compare_strategies(PlanRequest.from_payload(demo_request()))
        self.assertEqual(len(result["rows"]), 3)
        zigzag, one_way = result["rows"][1:]
        self.assertAlmostEqual(zigzag["cut_length_mm"], one_way["cut_length_mm"])
        self.assertGreater(one_way["rapid_length_mm"], zigzag["rapid_length_mm"])
        self.assertIn("link_length_mm", comparison_csv(result))
        self.assertTrue(comparison_csv(result).startswith("\ufeff"))


class ExtensionApiTests(ApiTestCase):
    def test_blender_zip_and_csv_downloads(self):
        for route, marker in (("blender", b"PK"), ("comparison", b"\xef\xbb\xbf")):
            status, body, headers = self.post("/api/export/" + route, demo_request())
            self.assertEqual(status, 200)
            self.assertTrue(body.startswith(marker))
            self.assertIn("attachment", headers["Content-Disposition"])

    def test_spiral_and_comparison(self):
        for route in ("/api/plan", "/api/compare"):
            status, body, _ = self.post(route, demo_request())
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])

    def test_options_and_geometry_errors(self):
        for payload, code in (({**demo_request(), "blender": {"fps": 0}}, 400),
                              ({**demo_request(), "blender": "invalid"}, 400),
                              (demo_request(stepover_mm=7), 422)):
            status, _, _ = self.post("/api/export/blender", payload)
            self.assertEqual(status, code)


if __name__ == "__main__":
    unittest.main()
