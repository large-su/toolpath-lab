"""Preset validation and independence from machining configuration."""

import unittest

from toolpath_lab.core.tool import Tool, tool_parameters
from toolpath_lab.core.tool_library import tool_library, tool_preset_selector
from toolpath_lab.core.region import SquareRegion
from toolpath_lab.planning import run_plan
from toolpath_lab.server.catalog import catalog_payload
from toolpath_lab.server.schema import PlanRequest


class ToolLibraryTests(unittest.TestCase):
    def test_each_preset_is_a_valid_complete_geometry(self):
        library = tool_library()
        self.assertEqual(len(library), 8)
        self.assertEqual(len({item["id"] for item in library}), 8)
        keys = set(tool_parameters().defaults())
        for item in library:
            self.assertEqual(set(item["values"]), keys)
            tool = Tool.from_parameters(item["values"])
            self.assertGreater(tool.radius_mm, 0)

    def test_payloads_do_not_share_mutable_state(self):
        payload = tool_library()
        payload[0]["values"]["diameter_mm"] = 100
        self.assertEqual(tool_library()[0]["values"]["diameter_mm"], 3)

    def test_selector_declares_custom_and_every_preset(self):
        selector = tool_preset_selector()
        self.assertEqual(selector.default, "custom")
        self.assertEqual([x.value for x in selector.choices], ["custom"] + [x["id"] for x in tool_library()])

    def test_catalog_keeps_tool_input_schema_unchanged(self):
        tool = catalog_payload()["tool"]
        self.assertEqual(tool["library"], tool_library())
        self.assertEqual([p["key"] for p in tool["parameters"]],
                         ["kind", "diameter_mm", "length_mm", "nose_radius_mm"])

    def test_presets_work_for_both_new_strategies(self):
        for preset in tool_library():
            for planner in ("contour", "spiral"):
                path = run_plan(planner_id=planner, tool=Tool.from_parameters(preset["values"]),
                                region=SquareRegion(80)).toolpath
                self.assertGreater(path.cut_length_mm, 0)

    def test_request_is_normal_editable_values_not_a_preset_reference(self):
        preset = tool_library()[1]
        values = dict(preset["values"])
        values["diameter_mm"] = 7
        request = PlanRequest.from_payload({"tool": values, "planner": {"id": "raster",
                                            "parameters": {"feed_mm_per_min": 1234}}})
        self.assertEqual(request.tool.diameter_mm, 7)
        self.assertEqual(request.planner_parameters["feed_mm_per_min"], 1234)
        self.assertEqual(preset["values"]["diameter_mm"], 6)


if __name__ == "__main__":
    unittest.main()
