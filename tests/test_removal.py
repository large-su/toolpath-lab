"""2.5D material removal: heights, volumes and the depth aware coverage ratio."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import measure_coverage, run_plan
from toolpath_lab.planning.removal import measure_removal


def _tool(diameter: float = 6.0, kind: ToolKind = ToolKind.FLAT) -> Tool:
    return Tool(kind, diameter_mm=diameter, length_mm=30.0)


def _plan(shape: str, planner_id: str, parameters: dict | None = None, tool: Tool | None = None):
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0}
    options.update(parameters or {})
    return run_plan(
        planner_id=planner_id,
        tool=tool or _tool(),
        region=build_region(shape, {}),
        parameters=options,
    ).toolpath


class HeightMapTests(unittest.TestCase):
    def test_a_cell_the_tool_never_touches_stays_at_the_top_face(self) -> None:
        toolpath = Toolpath(
            moves=(Move(MoveKind.CUT, np.array([[0.0, 0.0, -1.0], [10.0, 0.0, -1.0]]), 600.0),)
        )
        removal = measure_removal(toolpath, build_region("square", {"side_mm": 40.0}), _tool())
        untouched = removal.heights_mm == 0.0
        self.assertTrue(untouched.any())
        self.assertLess(removal.removed_volume_mm3, removal.region_area_mm2)

    def test_a_pass_lowers_its_own_strip_only(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[-15.0, 0.0, -2.0], [15.0, 0.0, -2.0]]), 600.0),
                Move(MoveKind.CUT, np.array([[-15.0, 12.0, -2.0], [15.0, 12.0, -2.0]]), 600.0),
            )
        )
        removal = measure_removal(toolpath, build_region("square", {"side_mm": 40.0}), _tool())
        # Two strips 6 mm wide and 30 mm long at 2 mm deep; the grid quantises each strip by a cell,
        # so allow a generous margin around the analytic 720 mm^3.
        self.assertLess(abs(removal.removed_volume_mm3 - 2 * 6.0 * 30.0 * 2.0), 0.2 * 720.0)
        self.assertGreater(removal.uncut_area_mm2, 0.0)

    def test_a_ramp_entry_is_credited_only_down_to_its_own_height(self) -> None:
        """The map follows the entry's slope: the shallow end removes less than the deep end."""

        ramp = Move(
            MoveKind.CUT,
            np.array([[-10.0, 0.0, 0.0], [10.0, 0.0, -2.0]]),
            600.0,
            pass_index=-1,
        )
        removal = measure_removal(
            Toolpath(moves=(ramp,)), build_region("square", {"side_mm": 40.0}), _tool()
        )
        row = removal.heights_mm.shape[0] // 2
        strip = removal.heights_mm[row][removal.heights_mm[row] < 0.0]
        self.assertGreater(strip.size, 0)
        self.assertAlmostEqual(float(strip.min()), -2.0, places=6)  # the deep end
        self.assertGreater(float(strip.max()), -2.0)  # and the shallow end is above it


class PlanTests(unittest.TestCase):
    def test_a_dense_raster_reaches_the_floor_everywhere(self) -> None:
        toolpath = _plan(
            "square", "raster", {"stepover_mm": 3.0, "depth_mm": 4.0, "stepdown_mm": 2.0}
        )
        removal = measure_removal(toolpath, build_region("square", {}), _tool())
        # 80 x 80 to a depth of 4 mm, cut in two layers.
        self.assertAlmostEqual(removal.floor_mm, -4.0, places=6)
        self.assertAlmostEqual(removal.removed_volume_mm3, 80 * 80 * 4.0, delta=80 * 80 * 4.0 * 0.02)
        self.assertGreater(removal.floor_ratio, 0.98)
        self.assertLess(removal.remaining_volume_mm3, 80 * 80 * 4.0 * 0.02)

    def test_a_wide_stepover_leaves_material_behind(self) -> None:
        toolpath = _plan("square", "raster", {"stepover_mm": 12.0, "depth_mm": 2.0})
        removal = measure_removal(toolpath, build_region("square", {}), _tool())
        self.assertLess(removal.floor_ratio, 0.8)
        self.assertGreater(removal.remaining_volume_mm3, 0.0)

    def test_untouched_area_agrees_with_the_planar_coverage(self) -> None:
        """Both modules answer "was the tool ever here", so their uncut areas must line up."""

        region = build_region("square", {})
        toolpath = _plan("square", "raster", {"stepover_mm": 12.0})
        removal = measure_removal(toolpath, region, _tool())
        coverage = measure_coverage(toolpath, region, _tool())
        self.assertAlmostEqual(removal.uncut_area_mm2, coverage.uncut_area_mm2, delta=120.0)

    def test_a_ball_nose_removes_almost_nothing_on_a_flat_floor(self) -> None:
        """Its footprint is a point, so the height map keeps a one-cell trail and leaves the rest."""

        ball = _tool(kind=ToolKind.BALL)
        toolpath = _plan("square", "raster", {"stepover_mm": 3.0, "depth_mm": 2.0}, tool=ball)
        removal = measure_removal(toolpath, build_region("square", {}), ball)
        self.assertLess(removal.removed_volume_mm3, 80 * 80 * 2.0 * 0.25)
        self.assertGreater(removal.uncut_area_mm2, 0.6 * removal.region_area_mm2)

    def test_a_bull_nose_removes_its_flat_annulus(self) -> None:
        bull = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=30.0, bull_corner_radius_mm=2.0)
        toolpath = _plan("square", "raster", {"stepover_mm": 2.0, "depth_mm": 2.0}, tool=bull)
        removal = measure_removal(toolpath, build_region("square", {}), bull)
        self.assertAlmostEqual(removal.floor_mm, -2.0, places=6)
        self.assertGreater(removal.floor_ratio, 0.9)  # a 3 mm flat bottom over a 2 mm stepover


class ResponseTests(unittest.TestCase):
    def test_the_grid_stays_bounded_for_a_large_region(self) -> None:
        toolpath = _plan("square", "raster", {"stepover_mm": 20.0, "depth_mm": 2.0})
        removal = measure_removal(toolpath, build_region("square", {"side_mm": 900.0}), _tool())
        self.assertLessEqual(removal.heights_mm.size, 400_000)
        self.assertGreater(removal.cell_mm, 0.5)  # grown to respect the cell budget

    def test_describe_is_json_friendly(self) -> None:
        removal = measure_removal(
            _plan("square", "raster", {"depth_mm": 2.0}), build_region("square", {}), _tool()
        )
        payload = removal.describe()
        self.assertEqual(sorted(payload), sorted([
            "cell_mm", "floor_mm", "region_area_mm2", "removed_volume_mm3", "remaining_volume_mm3",
            "uncut_area_mm2", "floor_ratio", "bounds_mm",
        ]))
        self.assertAlmostEqual(payload["floor_mm"], -2.0, places=4)
        self.assertEqual(len(payload["bounds_mm"]), 4)


if __name__ == "__main__":
    unittest.main()
