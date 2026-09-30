"""螺旋刀路：单条连续等距螺旋。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import PLANNERS, planner_catalog, run_plan
from toolpath_lab.planning.offset import distance_to_boundary
from toolpath_lab.server.app import create_server


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(shape: str = "circle", parameters=None):
    options = {"direction": "outward", "stepover_mm": 6.0,
               "sample_step_mm": 1.0, "feed_mm_per_min": 600.0}
    options.update(parameters or {})
    shape_parameters = {"diameter_mm": 80.0} if shape == "circle" else {"side_mm": 80.0}
    return run_plan(
        planner_id="spiral",
        tool=_tool(),
        region=build_region(shape, shape_parameters),
        parameters=options,
    )


def _cut_moves(toolpath: Toolpath):
    return [move for move in toolpath.moves if move.kind is MoveKind.CUT]


class RegistryTests(unittest.TestCase):
    def test_spiral_is_registered_after_raster(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "spiral"])

    def test_catalog_exposes_the_spiral_parameters(self) -> None:
        entry = next(item for item in planner_catalog() if item["id"] == "spiral")
        keys = [item["key"] for item in entry["parameters"]]
        self.assertEqual(keys, ["direction", "stepover_mm", "sample_step_mm", "feed_mm_per_min"])
        self.assertEqual(entry["label"], "螺旋刀路")


class StructureTests(unittest.TestCase):
    def test_toolpath_is_one_continuous_cut(self) -> None:
        self.assertEqual(len(_cut_moves(_plan().toolpath)), 1)

    def test_only_the_lead_in_and_lead_out_are_rapid(self) -> None:
        kinds = [move.kind for move in _plan().toolpath.moves]
        self.assertEqual(kinds.count(MoveKind.RAPID), 2)
        self.assertEqual(kinds.count(MoveKind.LINK), 0)

    def test_cut_points_lie_on_the_machining_plane(self) -> None:
        for move in _cut_moves(_plan().toolpath):
            self.assertTrue(np.allclose(move.points[:, 2], 0.0))

    def test_cut_move_uses_the_requested_feed(self) -> None:
        for move in _cut_moves(_plan(parameters={"feed_mm_per_min": 900.0}).toolpath):
            self.assertEqual(move.feed_mm_per_min, 900.0)


class GeometryTests(unittest.TestCase):
    def test_cut_never_leaves_the_region(self) -> None:
        # 刀心离轮廓的最近距离必须 >= 刀具足迹半径，否则刀会切出区域。
        for shape in ("circle", "square"):
            with self.subTest(shape=shape):
                region = build_region(
                    shape, {"diameter_mm": 80.0} if shape == "circle" else {"side_mm": 80.0}
                )
                for direction in ("outward", "inward"):
                    with self.subTest(direction=direction):
                        move = _cut_moves(_plan(shape, {"direction": direction}).toolpath)[0]
                        clearance = distance_to_boundary(move.points[:, :2], region.boundary()).min()
                        self.assertGreaterEqual(clearance, _tool().footprint_radius_mm - 1e-6)

    def test_outward_starts_at_the_pivot_and_ends_on_the_boundary(self) -> None:
        move = _cut_moves(_plan("circle", {"direction": "outward"}).toolpath)[0]
        self.assertAlmostEqual(float(np.linalg.norm(move.points[0][:2])), 0.0, places=6)
        # 圆形 D80、足迹半径 3 → 最外圈刀心半径 37。
        self.assertAlmostEqual(float(np.linalg.norm(move.points[-1][:2])), 37.0, places=3)

    def test_inward_starts_on_the_boundary_and_ends_at_the_pivot(self) -> None:
        move = _cut_moves(_plan("circle", {"direction": "inward"}).toolpath)[0]
        self.assertAlmostEqual(float(np.linalg.norm(move.points[0][:2])), 37.0, places=3)
        self.assertAlmostEqual(float(np.linalg.norm(move.points[-1][:2])), 0.0, places=6)

    def test_inward_is_the_reverse_of_outward(self) -> None:
        outward = _cut_moves(_plan("circle", {"direction": "outward"}).toolpath)[0]
        inward = _cut_moves(_plan("circle", {"direction": "inward"}).toolpath)[0]
        self.assertEqual(outward.points.shape, inward.points.shape)
        self.assertTrue(np.allclose(outward.points[::-1], inward.points))

    def test_spiral_radius_grows_monotonically_outward(self) -> None:
        # 螺旋半径只增不减（允许弧长重采样带来的微小抖动）。
        move = _cut_moves(_plan("circle", {"direction": "outward"}).toolpath)[0]
        radii = np.linalg.norm(move.points[:, :2], axis=1)
        self.assertTrue(np.all(np.diff(radii) >= -1e-6))

    def test_round_region_stays_inside_the_circularity_tolerance(self) -> None:
        # 圆形区域足够回转对称，不应该触发"圈间距会变"的提醒。
        outcome = _plan("circle")
        self.assertFalse(any("圈间距" in warning for warning in outcome.warnings))

    def test_square_region_also_works(self) -> None:
        move = _cut_moves(_plan("square").toolpath)[0]
        self.assertGreater(move.length_mm, 0.0)
        self.assertGreater(move.points.shape[0], 100)


class StepoverTests(unittest.TestCase):
    def test_smaller_stepover_gives_a_longer_path(self) -> None:
        fine = _plan("circle", {"stepover_mm": 3.0}).toolpath.cut_length_mm
        coarse = _plan("circle", {"stepover_mm": 12.0}).toolpath.cut_length_mm
        self.assertGreater(fine, coarse)

    def test_large_stepover_is_warned_about(self) -> None:
        outcome = _plan("circle", {"stepover_mm": 20.0})
        self.assertTrue(any("切宽" in warning for warning in outcome.warnings))

    def test_notes_describe_the_configuration(self) -> None:
        notes = _plan("circle", {"stepover_mm": 6.0}).toolpath.notes
        self.assertTrue(any("螺旋" in note for note in notes))
        self.assertTrue(any("固定值" in note for note in notes))

    def test_statistics_are_consistent(self) -> None:
        statistics = _plan().toolpath.statistics()
        self.assertGreater(statistics["estimated_time_s"], 0.0)
        self.assertAlmostEqual(
            statistics["total_length_mm"],
            statistics["cut_length_mm"] + statistics["rapid_length_mm"],
            places=6,
        )


class ErrorTests(unittest.TestCase):
    def test_oversized_tool_is_reported_as_unprocessable(self) -> None:
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="spiral",
                tool=_tool(120.0),
                region=build_region("square", {"side_mm": 40.0}),
                parameters={"stepover_mm": 5.0},
            )

    def test_bad_direction_is_a_parameter_error(self) -> None:
        with self.assertRaises(ParameterError):
            _plan("circle", {"direction": "sideways"})


class SpiralApiTests(unittest.TestCase):
    """螺旋通过 HTTP 也能直接用——注册之后接口自动包含它。"""

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

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_catalog_lists_the_spiral(self) -> None:
        with urllib.request.urlopen(self.base + "/api/catalog", timeout=30) as response:
            payload = json.loads(response.read())
        self.assertEqual([item["id"] for item in payload["planners"]["list"]], ["raster", "spiral"])

    def test_spiral_plan_round_trip(self) -> None:
        status, payload = self._post(
            "/api/plan",
            {
                "tool": {"diameter_mm": 6.0, "length_mm": 30.0},
                "region": {"shape": "circle", "parameters": {"diameter_mm": 80.0}},
                "planner": {"id": "spiral", "parameters": {"direction": "outward", "stepover_mm": 6.0}},
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["toolpath"]["planner"], "spiral")
        self.assertIsNotNone(payload["timeline"])
        self.assertEqual(len(payload["toolpath"]["moves"]), 3)


if __name__ == "__main__":
    unittest.main()
