"""Projected finishing coverage: continuous sweeps, scope and resource bounds."""

import unittest

import numpy as np

from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import CircleRegion, PolygonRegion, SquareRegion
from toolpath_lab.core.surface import FlatSurface, FreeformSurface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan
from toolpath_lab.planning.coverage import analyze_coverage
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan


def segment(a, b, kind=MoveKind.CUT):
    return Move(kind, np.array([a, b], dtype=float), 600)


def measure(moves, *, region=None, tool=None, metadata=None, **kwargs):
    return analyze_coverage(Toolpath(tuple(moves), metadata=metadata or {}),
                            region or SquareRegion(20), tool or Tool(diameter_mm=2),
                            FlatSurface(), **kwargs)


class CoverageTests(unittest.TestCase):
    def test_continuous_segment_sweep_not_just_endpoints(self):
        result = measure([segment([-10, 0, 0], [10, 0, 0])])
        # Width 2, length 20 through the middle of a 400 mm² square.
        self.assertAlmostEqual(result["coverage_percent"], 10)
        self.assertAlmostEqual(result["covered_area_mm2"], 40)
        self.assertEqual(result["active_cell_count"], 1600)

    def test_diagonal_sweep_and_reverse_are_identical(self):
        a, b = [-10, -10, 0], [10, 10, 0]
        result = measure([segment(a, b)])
        reverse = measure([segment(b, a)])
        self.assertEqual(result["grid"], reverse["grid"])
        self.assertGreater(result["coverage_percent"], 12)
        self.assertLess(result["coverage_percent"], 15)

    def test_rapid_is_never_counted(self):
        result = measure([segment([-10, 0, 0], [10, 0, 0], MoveKind.RAPID)])
        self.assertEqual(result["coverage_percent"], 0)
        self.assertEqual(result["uncovered_area_mm2"], 400)

    def test_link_sweep_is_counted(self):
        cut = measure([segment([-10, 0, 0], [10, 0, 0])])
        link = measure([segment([-10, 0, 0], [10, 0, 0], MoveKind.LINK)])
        self.assertEqual(cut["grid"], link["grid"])

    def test_roughing_excluded(self):
        cut = segment([-10, 0, 0], [10, 0, 0])
        rapid = segment([-10, 5, 10], [10, 5, 10], MoveKind.RAPID)
        result = measure([cut, rapid], metadata={"roughing": {"finish_start_move_index": 1}})
        self.assertEqual(result["coverage_percent"], 0)
        self.assertEqual(result["finish_start_move_index"], 1)

    def test_stationary_xy_segment_is_a_disc(self):
        result = measure([segment([0, 0, 0], [0, 0, 2])])
        self.assertGreater(result["covered_area_mm2"], 2.8)
        self.assertLess(result["covered_area_mm2"], 3.6)

    def test_outside_path_cannot_cover_region(self):
        result = measure([segment([100, 100, 0], [200, 100, 0])])
        self.assertEqual(result["coverage_percent"], 0)

    def test_sparse_vs_dense_and_partition_of_area(self):
        ratios = []
        for step in (12, 3):
            region, tool = SquareRegion(80), Tool(diameter_mm=6)
            path = run_plan(planner_id="raster", tool=tool, region=region,
                            parameters={"stepover_mm": step}).toolpath
            result = analyze_coverage(path, region, tool, FlatSurface())
            ratios.append(result["coverage_percent"])
            self.assertAlmostEqual(result["covered_area_mm2"] + result["uncovered_area_mm2"], 6400)
        self.assertLess(ratios[0], 65)
        self.assertGreater(ratios[1], 99)

    def test_circle_and_concave_polygon_exclude_outside_centers(self):
        polygon = PolygonRegion(((0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)))
        for region in (CircleRegion(20), polygon):
            result = measure([segment([100, 100, 0], [200, 100, 0])], region=region)
            grid = result["grid"]
            self.assertLess(result["active_cell_count"], grid["nx"] * grid["ny"])
            self.assertEqual(sum(grid["uncovered_mask"]), result["active_cell_count"])
        self.assertEqual(result["active_cell_count"], 256)
        self.assertEqual(result["region_area_mm2"], 64)

    def test_grid_budget_large_and_thin_regions(self):
        for region in (SquareRegion(1000), PolygonRegion(((0, 0), (100000, 0), (100000, 1), (0, 1)))):
            result = measure([segment([0, 0, 0], [1, 0, 0])], region=region, max_cells=400)
            self.assertLessEqual(result["grid"]["nx"] * result["grid"]["ny"], 400)
            self.assertEqual(len(result["grid"]["uncovered_mask"]),
                             result["grid"]["nx"] * result["grid"]["ny"])

    def test_invalid_grid_arguments(self):
        for kwargs in ({"cell_mm": 0}, {"cell_mm": float("nan")}, {"max_cells": 2}):
            with self.assertRaises(ValueError):
                measure([segment([0, 0, 0], [1, 0, 0])], **kwargs)

    def test_thin_polygon_without_centers_is_unavailable_not_zero_percent(self):
        region = PolygonRegion(((0, 0), (10, 0), (10, .01), (.01, .01),
                                (.01, 9.99), (10, 9.99), (10, 10), (0, 10)))
        result = measure([segment([0, 0, 0], [1, 0, 0])], region=region)
        self.assertFalse(result["available"])
        self.assertIsNone(result["coverage_percent"])
        self.assertIsNone(result["uncovered_area_mm2"])

    def test_texture_dimensions_are_bounded_for_extreme_aspect_ratio(self):
        region = PolygonRegion(((0, 0), (100000, 0), (100000, 1), (0, 1)))
        result = measure([segment([0, 0, 0], [1, 0, 0])], region=region)
        self.assertLessEqual(max(result["grid"]["nx"], result["grid"]["ny"]), 2048)


    def test_height_shape_and_orientation_are_explicitly_approximations(self):
        a = segment([-10, 0, 0], [10, 0, 0])
        b = Move(a.kind, a.points + [0, 0, 100], 600, tool_axes=[[0.6, 0, 0.8]] * 2)
        before = b.points.copy(), b.tool_axes.copy()
        region, tool = SquareRegion(20), Tool(ToolKind.BALL, 2)
        flat = analyze_coverage(Toolpath((a,)), region, tool, FlatSurface())
        curved = analyze_coverage(Toolpath((b,)), region, tool, FreeformSurface())
        self.assertEqual(flat["grid"], curved["grid"])
        self.assertTrue(curved["estimate_only"])
        self.assertTrue(curved["approximate_contact"])
        np.testing.assert_array_equal(before[0], b.points)
        np.testing.assert_array_equal(before[1], b.tool_axes)

    def test_all_planners_and_roughing_produce_same_finish_coverage(self):
        for planner in ("raster", "crosshatch", "five_axis", "adaptive_scallop", "five_axis_adaptive"):
            with self.subTest(planner=planner):
                body = {"tool": {"kind": "ball"}, "region": {"parameters": {"side_mm": 20}},
                        "surface": {"type": "freeform"}, "planner": {"id": planner}}
                off = execute_plan(PlanRequest.from_payload(body), with_timeline=False)
                body["roughing"] = {"enabled": True}
                on = execute_plan(PlanRequest.from_payload(body), with_timeline=False)
                self.assertEqual(off.coverage["grid"], on.coverage["grid"])
                self.assertEqual(off.coverage["coverage_percent"], on.coverage["coverage_percent"])
                self.assertIsNone(off.timeline)
                self.assertEqual(off.to_payload()["coverage"], off.coverage)


if __name__ == "__main__":
    unittest.main()
