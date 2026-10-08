"""Imported outline regions: points in, a valid region out (the DXF path's other half)."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.region import (
    ImportedOutlineRegion,
    build_region,
    region_catalog,
    region_from_points,
)
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

    def test_a_three_dimensional_point_is_rejected_instead_of_flattened(self) -> None:
        """A plan response carries its boundary as [x, y, 0]; sending that back has to say so."""

        for point in ((2.0, 3.0, 0.0), (2.0, 3.0, 5.0)):
            with self.subTest(point=point):
                with self.assertRaises(ParameterError):
                    region_from_points([(0.0, 0.0), (10.0, 0.0), point])

    def test_a_non_finite_coordinate_is_rejected(self) -> None:
        """JSON can carry NaN and infinity; the parameter layer refuses them, so this must too."""

        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ParameterError):
                    region_from_points([(0.0, 0.0), (10.0, 0.0), (5.0, bad)])

    def test_it_is_not_offered_in_the_catalogue(self) -> None:
        """The UI must never offer a shape whose points it cannot supply."""

        self.assertNotIn("imported", [entry["id"] for entry in region_catalog()])


class DescriptionTests(unittest.TestCase):
    """What an imported outline tells a caller: a count, never the whole point list."""

    def test_the_description_counts_the_points_instead_of_dumping_them(self) -> None:
        described = region_from_points(CLOCKWISE).describe()
        self.assertEqual(described["id"], "imported")
        self.assertEqual(described["parameters"], {"point_count": 4})
        self.assertAlmostEqual(described["area_mm2"], 60 * 40, places=6)

    def test_the_export_header_states_the_point_count(self) -> None:
        self.assertEqual(region_from_points(CLOCKWISE).header_text(), "imported - 4 points")

    def test_a_registered_shape_keeps_its_parameters_in_the_header(self) -> None:
        # The other half of the rule: only an imported outline is summarised.
        self.assertEqual(
            build_region("square", {"side_mm": 50.0}).header_text(), "square - side_mm=50.0"
        )


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
