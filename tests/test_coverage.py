"""Coverage analysis: does this toolpath machine the region out?"""

from __future__ import annotations

import unittest
from math import sqrt

import numpy as np

from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import coverage_warnings, measure_coverage, run_plan
from toolpath_lab.planning.coverage import MAX_CELLS


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(region, planner_id: str = "raster", parameters=None, tool=None) -> Toolpath:
    return run_plan(
        planner_id=planner_id,
        tool=tool or _tool(),
        region=region,
        parameters=parameters or {},
    ).toolpath


class AnalyticTests(unittest.TestCase):
    """Calibrate the grid statistics on examples whose answer can be computed analytically."""

    def test_a_single_pass_across_a_narrow_region_covers_everything(self) -> None:
        region = build_region("rectangle", {"width_mm": 10.0, "height_mm": 6.0})
        toolpath = Toolpath(
            moves=(
                Move(
                    MoveKind.CUT,
                    np.array([[-5.0, 0.0, 0.0], [5.0, 0.0, 0.0]]),
                    600.0,
                    pass_index=0,
                ),
            )
        )
        coverage = measure_coverage(toolpath, region, _tool(8.0))
        # A D8 tool has footprint radius 4, covering |y| <= 4, while the region is only |y| <= 3
        self.assertAlmostEqual(coverage.ratio, 1.0, places=9)
        self.assertAlmostEqual(coverage.uncut_area_mm2, 0.0, places=9)
        self.assertEqual(coverage.patch_count, 0)
        self.assertEqual(coverage.patches, ())

    def test_a_stepover_wider_than_the_tool_leaves_stripes(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        tool = _tool(6.0)
        coverage = measure_coverage(
            _plan(region, parameters={"stepover_mm": 12.0}), region, tool
        )
        # Passes 12 apart with a 6 mm tool leave stripes of about 6 x 74 each, six of them,
        # plus a few small gaps near the boundary (a round tool misses corners and pass ends).
        self.assertLess(coverage.ratio, 0.65)
        stripes = [patch for patch in coverage.patches if patch.area_mm2 > 100.0]
        self.assertEqual(len(stripes), 6)
        biggest = coverage.patches[0]
        self.assertAlmostEqual(biggest.area_mm2, 6.0 * 74.0, delta=80.0)
        x_span = biggest.bounds_mm[1] - biggest.bounds_mm[0]
        self.assertGreater(x_span, 70.0)

    def test_measuring_a_small_toolpath_against_a_bigger_region(self) -> None:
        small = build_region("square", {"side_mm": 80.0})
        big = build_region("square", {"side_mm": 100.0})
        coverage = measure_coverage(_plan(small), big, _tool())
        # A ring of 100² − 80² = 3600 is uncut, and it is a single connected patch
        self.assertAlmostEqual(coverage.uncut_area_mm2, 3600.0, delta=120.0)
        self.assertAlmostEqual(coverage.ratio, 0.64, delta=0.02)
        self.assertEqual(coverage.patch_count, 1)

    def test_a_round_tool_leaves_only_edge_residue(self) -> None:
        """A round tool on straight passes only leaves material near the boundary.

        Two sources: the sharp corners of the square (about 3² − π·3²/4 ≈ 1.9 mm² each), and the
        scalloped gaps between pass ends (passes stop at x=±37, so the outer 3 mm is covered only by the end caps).
        """

        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(_plan(region), region, _tool())
        self.assertGreater(coverage.ratio, 0.97)
        self.assertLess(coverage.uncut_area_mm2, 120.0)
        for patch in coverage.patches:
            centre_x, centre_y = patch.centre_mm
            with self.subTest(patch=patch.centre_mm):
                # Within 4 mm of the boundary: |x| >= 36 or |y| >= 36
                self.assertTrue(abs(centre_x) >= 36.0 or abs(centre_y) >= 36.0)

    def test_contour_leaves_the_centre_when_the_stepover_is_large(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(
            _plan(region, planner_id="contour", parameters={"stepover_mm": 12.0}),
            region,
            _tool(),
        )
        biggest = coverage.patches[0]
        self.assertGreater(biggest.area_mm2, 1000.0)
        self.assertAlmostEqual(biggest.centre_mm[0], 0.0, delta=2.0)
        self.assertAlmostEqual(biggest.centre_mm[1], 0.0, delta=2.0)

    def test_a_finer_stepover_leaves_less_material(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        tool = _tool()
        coarse = measure_coverage(_plan(region, parameters={"stepover_mm": 12.0}), region, tool)
        fine = measure_coverage(_plan(region, parameters={"stepover_mm": 3.0}), region, tool)
        self.assertLess(fine.uncut_area_mm2, coarse.uncut_area_mm2)


class GridTests(unittest.TestCase):
    def test_the_grid_is_refined_for_small_regions_and_capped_for_large_ones(self) -> None:
        small = build_region("square", {"side_mm": 20.0})
        toolpath = _plan(small)
        self.assertAlmostEqual(measure_coverage(toolpath, small, _tool()).cell_mm, 0.5)

        big = build_region("square", {"side_mm": 900.0})
        coarse = measure_coverage(toolpath, big, _tool(), cell_mm=0.05)
        self.assertGreater(coarse.cell_mm, 0.05)
        self.assertLessEqual(
            (900.0 / coarse.cell_mm + 1) ** 2, MAX_CELLS * 1.2
        )

    def test_the_grid_area_approaches_the_true_area(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(_plan(region), region, _tool())
        self.assertAlmostEqual(coverage.region_area_mm2, 6400.0, delta=40.0)

    def test_covered_and_uncut_add_up_to_the_region(self) -> None:
        region = build_region("dumbbell", {})
        coverage = measure_coverage(_plan(region, planner_id="contour"), region, _tool())
        self.assertAlmostEqual(
            coverage.covered_area_mm2 + coverage.uncut_area_mm2,
            coverage.region_area_mm2,
            places=6,
        )


class WarningTests(unittest.TestCase):
    def test_small_corner_residue_does_not_warn(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(_plan(region), region, _tool())
        self.assertEqual(coverage_warnings(coverage), [])

    def test_a_big_uncut_area_warns_with_the_location(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(
            _plan(region, parameters={"stepover_mm": 12.0}), region, _tool()
        )
        warnings = coverage_warnings(coverage)
        self.assertEqual(len(warnings), 1)
        self.assertIn("没有切到", warnings[0])
        self.assertIn("覆盖率", warnings[0])
        self.assertIn("最大一块", warnings[0])

    def test_a_fully_covered_region_never_warns(self) -> None:
        region = build_region("rectangle", {"width_mm": 10.0, "height_mm": 6.0})
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[-5.0, 0.0, 0.0], [5.0, 0.0, 0.0]]), 600.0),
            )
        )
        self.assertEqual(coverage_warnings(measure_coverage(toolpath, region, _tool(8.0))), [])

    def test_rapid_moves_do_not_count_as_removing_material(self) -> None:
        region = build_region("rectangle", {"width_mm": 10.0, "height_mm": 6.0})
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.RAPID, np.array([[-5.0, 0.0, 0.0], [5.0, 0.0, 0.0]]), 5000.0),
            )
        )
        coverage = measure_coverage(toolpath, region, _tool(8.0))
        self.assertAlmostEqual(coverage.ratio, 0.0, places=9)
        self.assertAlmostEqual(coverage.uncut_area_mm2, coverage.region_area_mm2, places=6)

    def test_links_do_remove_material(self) -> None:
        region = build_region("rectangle", {"width_mm": 10.0, "height_mm": 6.0})
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.LINK, np.array([[-5.0, 0.0, 0.0], [5.0, 0.0, 0.0]]), 600.0),
            )
        )
        self.assertAlmostEqual(measure_coverage(toolpath, region, _tool(8.0)).ratio, 1.0, places=9)


class RectangleTests(unittest.TestCase):
    """Rectangles merged from uncut cells for the 3D overlay: their area must match the statistics."""

    def test_rectangles_tile_the_uncut_cells(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(
            _plan(region, parameters={"stepover_mm": 12.0}), region, _tool()
        )
        total = sum(
            (x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in coverage.uncut_rects
        )
        self.assertAlmostEqual(total, coverage.uncut_area_mm2, places=6)
        self.assertFalse(coverage.uncut_rects_truncated)
        # A missed band merges into a few big rectangles instead of thousands of cells
        self.assertLess(len(coverage.uncut_rects), 200)

    def test_rectangles_stay_inside_the_region_bounds(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(
            _plan(region, parameters={"stepover_mm": 12.0}), region, _tool()
        )
        for x0, y0, x1, y1 in coverage.uncut_rects:
            with self.subTest(rect=(x0, y0, x1, y1)):
                self.assertGreaterEqual(x0, -40.0)
                self.assertLessEqual(x1, 40.0)
                self.assertGreaterEqual(y0, -40.0)
                self.assertLessEqual(y1, 40.0)
                self.assertGreater(x1, x0)
                self.assertGreater(y1, y0)

    def test_the_rectangle_list_can_be_capped(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        coverage = measure_coverage(
            _plan(region, parameters={"stepover_mm": 12.0}), region, _tool(), max_rects=3
        )
        self.assertEqual(len(coverage.uncut_rects), 3)
        self.assertTrue(coverage.uncut_rects_truncated)

    def test_a_fully_covered_toolpath_has_no_rectangles(self) -> None:
        region = build_region("rectangle", {"width_mm": 10.0, "height_mm": 6.0})
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[-5.0, 0.0, 0.0], [5.0, 0.0, 0.0]]), 600.0),
            )
        )
        coverage = measure_coverage(toolpath, region, _tool(8.0))
        self.assertEqual(coverage.uncut_rects, ())
        self.assertFalse(coverage.uncut_rects_truncated)

    def test_describe_exposes_the_rectangles(self) -> None:
        region = build_region("square", {"side_mm": 80.0})
        payload = measure_coverage(
            _plan(region, parameters={"stepover_mm": 12.0}), region, _tool()
        ).describe()
        self.assertTrue(payload["uncut_rects"])
        self.assertEqual(len(payload["uncut_rects"][0]), 4)
        self.assertFalse(payload["uncut_rects_truncated"])


if __name__ == "__main__":
    unittest.main()
