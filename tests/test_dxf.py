"""DXF 刀路导出：内容结构与接口。"""

from __future__ import annotations

import unittest

from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export.dxf import toolpath_to_dxf
from toolpath_lab.planning import run_plan
from tests.test_api import ApiTestCase

_LAYER_NAMES = {"cut": "CUT", "link": "LINK", "rapid": "RAPID"}


def _raster_toolpath():
    return run_plan(
        planner_id="raster",
        tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
        region=build_region("square", {"side_mm": 80.0}),
        parameters={"mode": "zigzag", "stepover_mm": 6.0},
    ).toolpath


class DxfExportTests(unittest.TestCase):
    def test_dxf_ends_with_eof_and_balanced_sections(self) -> None:
        text = toolpath_to_dxf(_raster_toolpath(), description="test")
        self.assertTrue(text.rstrip().endswith("EOF"))
        self.assertEqual(text.count("SECTION"), 4)  # HEADER / COMMENTS / TABLES / ENTITIES
        self.assertEqual(text.count("ENDSEC"), 4)

    def test_every_move_becomes_one_polyline(self) -> None:
        toolpath = _raster_toolpath()
        text = toolpath_to_dxf(toolpath)
        self.assertEqual(text.count("POLYLINE"), len(toolpath.moves))
        vertices = sum(move.points.shape[0] for move in toolpath.moves)
        self.assertEqual(text.count("VERTEX"), vertices)
        self.assertEqual(text.count("SEQEND"), len(toolpath.moves))

    def test_layers_only_for_used_move_kinds(self) -> None:
        toolpath = _raster_toolpath()
        text = toolpath_to_dxf(toolpath)
        used = {_LAYER_NAMES[move.kind.value] for move in toolpath.moves}
        for name in ("CUT", "LINK", "RAPID"):
            if name in used:
                self.assertIn("LAYER", text)
            else:
                self.assertNotIn(name, text)

    def test_coordinates_are_rounded_to_decimals(self) -> None:
        text = toolpath_to_dxf(_raster_toolpath(), decimals=2)
        self.assertIn("\n10\n", text)  # 顶点组码存在
        self.assertNotIn("\n10\n-37.0000", text)


class DxfApiTests(ApiTestCase):
    def test_dxf_endpoint_returns_a_download(self) -> None:
        payload = {
            "tool": {"kind": "flat", "diameter_mm": 6.0, "length_mm": 30.0},
            "region": {"shape": "square", "parameters": {"side_mm": 80.0}},
            "planner": {
                "id": "raster",
                "parameters": {"mode": "zigzag", "stepover_mm": 6.0,
                               "direction_deg": 0.0, "feed_mm_per_min": 600.0},
            },
        }
        status, body, headers = self.post("/api/export/dxf", payload)
        self.assertEqual(status, 200)
        self.assertIn(".dxf", headers["Content-Disposition"])
        self.assertIn(b"EOF", body)
        self.assertIn(b"POLYLINE", body)

    def test_dxf_endpoint_works_for_contour_too(self) -> None:
        payload = {
            "tool": {"kind": "ball", "diameter_mm": 6.0, "length_mm": 30.0},
            "region": {"shape": "circle", "parameters": {"diameter_mm": 80.0}},
            "planner": {
                "id": "contour",
                "parameters": {"stepover_mm": 6.0, "sample_step_mm": 1.0,
                               "feed_mm_per_min": 600.0},
            },
        }
        status, body, headers = self.post("/api/export/dxf", payload)
        self.assertEqual(status, 200)
        self.assertIn(b"EOF", body)
        self.assertIn(b"CUT", body)


if __name__ == "__main__":
    unittest.main()
