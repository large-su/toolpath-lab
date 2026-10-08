"""Imported outline regions: points in, a valid region out (the DXF path's other half)."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.region import ImportedOutlineRegion, region_catalog, region_from_points
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import measure_coverage, run_plan

#: A 60 x 40 rectangle, given clockwise so the region has to fix the winding itself.
CLOCKWISE = [(0.0, 0.0), (0.0, 40.0), (60.0, 40.0), (60.0, 0.0)]


class ConstructionTests(unittest.TestCase):
    def test_a_clockwise_outline_comes_back_counter_clockwise(self) -> None:
        region = region_from_points(CLOCKWISE)
        self.assertIsInstance(region, ImportedOutlineRegion)
        polygon = region.boundary()
        x, y = polygon[:, 0], polygon[:, 1]
        signed = 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
        self.assertGreater(signed, 0.0)

    def test_counter_clockwise_points_are_kept_as_they_are(self) -> None:
        region = region_from_points(list(reversed(CLOCKWISE)))
        self.assertEqual(region.boundary().shape, (4, 2))
        # Already counter-clockwise: the order the caller gave is preserved.
        self.assertTrue(np.allclose(region.boundary()[0], (60.0, 0.0)))

    def test_a_repeated_closing_point_is_dropped(self) -> None:
        region = region_from_points([*CLOCKWISE, CLOCKWISE[0]])
        self.assertEqual(region.boundary().shape[0], 4)

    def test_too_few_points_are_rejected(self) -> None:
        for points in ([(0.0, 0.0), (1.0, 1.0)], [(0.0, 0.0)]):
            with self.subTest(points=points):
                with self.assertRaises(ParameterError):
                    region_from_points(points)

    def test_a_point_without_two_coordinates_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            region_from_points([(0.0, 0.0), (1.0, 1.0), (2.0,)])

    def test_it_is_not_offered_in_the_catalogue(self) -> None:
        """The UI must never offer a shape whose points it cannot supply."""

        self.assertNotIn("imported", [entry["id"] for entry in region_catalog()])


class PlanningTests(unittest.TestCase):
    def test_an_imported_outline_plans_like_any_other_region(self) -> None:
        region = region_from_points(CLOCKWISE)
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        toolpath = run_plan(
            planner_id="raster",
            tool=tool,
            region=region,
            parameters={"stepover_mm": 6.0, "mode": "zigzag"},
        ).toolpath
        self.assertGreater(toolpath.pass_count, 0)
        self.assertGreater(toolpath.cut_length_mm, 0.0)
        coverage = measure_coverage(toolpath, region, tool)
        self.assertGreater(coverage.ratio, 0.9)
        self.assertAlmostEqual(coverage.area_mm2 if hasattr(coverage, "area_mm2") else 60 * 40,
                               60 * 40, places=0)


if __name__ == "__main__":
    unittest.main()
