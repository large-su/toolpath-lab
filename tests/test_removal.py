"""2.5D material removal: heights, volumes and the depth aware coverage ratio."""

from __future__ import annotations

import json
import unittest

import numpy as np

from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import measure_coverage, run_plan
from toolpath_lab.planning.removal import MAX_HEIGHT_CELLS, measure_removal


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


class HeightMapPayloadTests(unittest.TestCase):
    """The reduced grid that travels to the 3D view (the measurement grid never leaves the backend)."""

    def _measure(self, shape: str, region_parameters: dict, parameters: dict):
        region = build_region(shape, region_parameters)
        options = {"sample_step_mm": 1.0, **parameters}
        toolpath = run_plan(
            planner_id="raster", tool=_tool(), region=region, parameters=options
        ).toolpath
        return measure_removal(toolpath, region, _tool())

    @staticmethod
    def _values(height_map: dict) -> list:
        return [value for row in height_map["cells"] for value in row]

    def test_the_map_is_placed_on_the_region_bounds(self) -> None:
        removal = self._measure("square", {}, {"stepover_mm": 6.0, "depth_mm": 2.0})
        height_map = removal.height_map()
        x_min, x_max, y_min, y_max = removal.bounds_mm
        self.assertAlmostEqual(height_map["origin_mm"][0], x_min, places=4)
        self.assertAlmostEqual(height_map["origin_mm"][1], y_min, places=4)
        # Corner origin + rows/cols * cell size has to cover the grid the cells were measured on.
        self.assertAlmostEqual(
            height_map["cols"] * height_map["cell_size_mm"][0], x_max - x_min, delta=0.01
        )
        self.assertAlmostEqual(
            height_map["rows"] * height_map["cell_size_mm"][1], y_max - y_min, delta=0.01
        )

    def test_cells_whose_centre_is_outside_the_region_stay_empty(self) -> None:
        removal = self._measure(
            "circle", {"diameter_mm": 80.0}, {"stepover_mm": 6.0, "depth_mm": 2.0}
        )
        height_map = removal.height_map()
        values = self._values(height_map)
        # The bounding box corners of a circle are not part of the region: the overlay paints the
        # region, not the box around it.
        self.assertTrue(any(value is None for value in values))
        self.assertGreater(sum(1 for value in values if value is not None), 0.7 * len(values))
        # None (JSON null) is what carries "there is no cell here" to the viewport.
        self.assertIn("null", json.dumps(removal.describe()))

    def test_a_small_region_keeps_the_measured_resolution(self) -> None:
        removal = self._measure("square", {"side_mm": 20.0}, {"stepover_mm": 3.0, "depth_mm": 1.0})
        height_map = removal.height_map()
        self.assertEqual(height_map["cell_size_mm"], [0.5, 0.5])  # no reduction needed
        self.assertEqual(height_map["rows"], removal.heights_mm.shape[0])
        self.assertEqual(height_map["cols"], removal.heights_mm.shape[1])
        for row in range(height_map["rows"]):
            for column in range(height_map["cols"]):
                value = height_map["cells"][row][column]
                if removal.inside_mask[row, column]:
                    self.assertAlmostEqual(value, float(removal.heights_mm[row, column]), places=4)
                else:
                    self.assertIsNone(value)

    def test_a_coarse_cell_shows_the_depth_the_tool_reached(self) -> None:
        """The cell takes its deepest material: a strip thinner than a cell still shows up."""

        dense = self._measure(
            "square", {}, {"stepover_mm": 3.0, "depth_mm": 4.0, "stepdown_mm": 2.0}
        )
        sparse = self._measure("square", {}, {"stepover_mm": 12.0, "depth_mm": 2.0})
        dense_map, sparse_map = dense.height_map(), sparse.height_map()
        dense_values = [value for value in self._values(dense_map) if value is not None]
        sparse_values = [value for value in self._values(sparse_map) if value is not None]

        def at_floor(values, floor):
            return sum(1 for value in values if abs(value - floor) <= 1e-6) / len(values)

        self.assertGreater(at_floor(dense_values, dense.floor_mm), 0.9)
        # The displayed surface never claims a depth the tool did not reach, and it never goes below
        # the measured floor either: both ends are anchored on real values of the height map.
        self.assertAlmostEqual(min(dense_values), dense.floor_mm, places=4)
        self.assertEqual(max(sparse_values), 0.0)  # whole cells the tool missed stay at the top face
        self.assertGreaterEqual(min(sparse_values), sparse.floor_mm - 1e-9)
        # A cell that holds any floor-depth material is painted as floor: the map answers "how deep
        # did it get here", while how much was missed in total is the uncut overlay's job.
        self.assertGreaterEqual(at_floor(sparse_values, sparse.floor_mm), sparse.floor_ratio)

    def test_the_display_grid_stays_bounded(self) -> None:
        for shape, region_parameters, grown in (
            ("square", {"side_mm": 900.0}, True),
            # Long and thin: the two axes share one reduction factor, so a skewed region is the case
            # that could blow the cap.
            ("rectangle", {"width_mm": 1000.0, "height_mm": 20.0}, False),
        ):
            with self.subTest(shape=shape, region=region_parameters):
                removal = self._measure(shape, region_parameters, {"stepover_mm": 40.0, "depth_mm": 2.0})
                height_map = removal.height_map()
                self.assertLessEqual(height_map["rows"] * height_map["cols"], MAX_HEIGHT_CELLS)
                self.assertGreaterEqual(height_map["measured_cell_mm"], 0.5)
                self.assertEqual(height_map["measured_cell_mm"] > 0.5, grown)

    def test_a_plan_without_depth_carries_no_map(self) -> None:
        # Cutting at Z = 0 has no depth to colour, so the map is left out of the payload instead of
        # sending a grid of zeroes with every default request.
        removal = self._measure("square", {}, {"stepover_mm": 6.0})
        self.assertIsNone(removal.describe()["height_map"])


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
            "uncut_area_mm2", "floor_ratio", "bounds_mm", "height_map",
        ]))
        self.assertAlmostEqual(payload["floor_mm"], -2.0, places=4)
        self.assertEqual(len(payload["bounds_mm"]), 4)
        # The grid has to survive the round trip unchanged, or the overlay loses its depth scale.
        self.assertEqual(
            json.loads(json.dumps(payload))["height_map"], payload["height_map"]
        )


if __name__ == "__main__":
    unittest.main()
