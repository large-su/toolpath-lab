"""2.5D material removal: heights, volumes and the depth aware coverage ratio."""

from __future__ import annotations

import json
import unittest
from math import pi, sqrt

import numpy as np

from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import measure_coverage, run_plan
from toolpath_lab.planning.removal import (
    MAX_HEIGHT_CELLS,
    MAX_CHECKPOINTS,
    measure_removal,
)


def _tool(diameter: float = 6.0, kind: ToolKind = ToolKind.FLAT) -> Tool:
    return Tool(kind, diameter_mm=diameter, length_mm=30.0)


def _lined_toolpath(region, offsets: list[float], z: float = -2.0) -> Toolpath:
    """Straight cutting passes along X, `offsets` mm above/below the middle of the region.

    Region shapes are centred on the origin, so a pass has to be placed from the region's own bounds:
    a pass at y = 20 would sit outside a 40 mm square (which spans -20..20) and remove nothing.
    """

    polygon = np.asarray(region.boundary(), dtype=np.float64).reshape(-1, 2)
    x_min, y_min = polygon.min(axis=0)
    x_max, y_max = polygon.max(axis=0)
    middle = (y_min + y_max) / 2.0
    moves = tuple(
        Move(
            MoveKind.CUT,
            np.array([[x_min, middle + offset, z], [x_max, middle + offset, z]]),
            600.0,
            pass_index=index,
        )
        for index, offset in enumerate(offsets)
    )
    return Toolpath(moves=moves)


def _middle_y(region) -> float:
    polygon = np.asarray(region.boundary(), dtype=np.float64).reshape(-1, 2)
    return float((polygon[:, 1].min() + polygon[:, 1].max()) / 2.0)


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

    def test_a_ball_nose_sweeps_its_own_sphere_and_leaves_ridges(self) -> None:
        """The bottom is curved, so the sweep is a trough, not a flat trail: it takes a wide swath."""

        ball = _tool(kind=ToolKind.BALL)
        region = build_region("square", {})
        toolpath = _plan("square", "raster", {"stepover_mm": 3.0, "depth_mm": 2.0}, tool=ball)
        removal = measure_removal(toolpath, region, ball, stepover_mm=3.0)
        # The grid cannot land exactly on a pass axis, so the deepest cell sits a hair above the
        # commanded depth -- but never below it.
        self.assertGreaterEqual(removal.floor_mm, -2.0)
        self.assertAlmostEqual(removal.floor_mm, -2.0, delta=0.05)
        # Analytic bracket: every swept point is within half a stepover of some axis, so its depth is
        # between `depth - cusp` and `depth` (2 - 0.402 ... 2.0).
        swept_area = removal.region_area_mm2 - removal.uncut_area_mm2
        shallowest = 2.0 - ball.cusp_height_mm(3.0)
        self.assertGreater(removal.removed_volume_mm3, swept_area * shallowest * 0.97)
        self.assertLess(removal.removed_volume_mm3, swept_area * 2.0 * 1.03)
        # What is left standing is the scallop, an order of magnitude less than what came off.
        self.assertLess(removal.remaining_volume_mm3, removal.removed_volume_mm3 * 0.2)

    def test_a_ball_nose_height_map_follows_its_circle(self) -> None:
        """Every machined cell sits exactly on the sphere the sweep carried, within a grid cell."""

        ball = _tool(kind=ToolKind.BALL)  # R3
        region = build_region("square", {"side_mm": 40.0})
        # One pass along X through the middle, so the distance from the tool axis is |y - middle|.
        middle = _middle_y(region)
        removal = measure_removal(_lined_toolpath(region, [0.0], z=-2.0), region, ball, cell_mm=0.25)
        heights = removal.heights_mm
        column = heights.shape[1] // 2  # the middle of the pass, so only the distance in Y matters
        y_min = removal.bounds_mm[2]
        worst = 0.0
        touched_cells = 0
        for row in range(heights.shape[0]):
            value = float(heights[row, column])
            if value >= 0.0:
                continue  # untouched, or grazed above the top face
            distance = abs(y_min + (row + 0.5) * removal.cell_mm - middle)
            expected = -2.0 + 3.0 - sqrt(9.0 - distance**2)
            touched_cells += 1
            worst = max(worst, abs(value - expected))
        self.assertGreater(touched_cells, 20)  # the trough really is several cells wide
        self.assertLess(worst, 0.1)

    def test_the_floor_tolerance_is_the_scallop_the_tool_leaves(self) -> None:
        """A curved bottom always leaves a wavy floor; at the ridge height it counts as reached."""

        ball = _tool(kind=ToolKind.BALL)
        region = build_region("square", {})
        toolpath = _plan("square", "raster", {"stepover_mm": 1.0, "depth_mm": 2.0}, tool=ball)
        fine = measure_removal(toolpath, region, ball, stepover_mm=1.0)
        strict = measure_removal(toolpath, region, ball)
        # 1 mm passes with R3: the ridge is R - sqrt(R^2 - (s/2)^2) = 0.042 mm.
        self.assertAlmostEqual(fine.cusp_mm, 3.0 - sqrt(9.0 - 0.25), places=6)
        self.assertAlmostEqual(fine.floor_tolerance_mm, fine.cusp_mm, places=9)
        self.assertGreater(fine.floor_ratio, 0.8)
        # Without a stepover the tolerance is the strict one, and the wavy floor then counts as unfinished.
        self.assertEqual(strict.cusp_mm, None)
        self.assertLess(strict.floor_ratio, 0.1)
        # A gap so wide that the passes never overlap has no scallop at all: the ridge is full height.
        wide = _plan("square", "raster", {"stepover_mm": 8.0, "depth_mm": 2.0}, tool=ball)
        not_overlapping = measure_removal(wide, region, ball, stepover_mm=8.0)
        self.assertIsNone(not_overlapping.cusp_mm)
        self.assertLess(not_overlapping.floor_ratio, 0.9)

    def test_a_bull_nose_rounds_its_corner_outside_the_flat_bottom(self) -> None:
        """The flat bottom spans R - Rc; past it the corner torus cuts a fillet, not a wall."""

        bull = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=30.0, bull_corner_radius_mm=2.0)
        region = build_region("square", {"side_mm": 40.0})
        middle = _middle_y(region)
        removal = measure_removal(_lined_toolpath(region, [0.0], z=-2.0), region, bull, cell_mm=0.25)
        heights = removal.heights_mm
        column = heights.shape[1] // 2  # the middle of the pass, so only the distance in Y matters
        y_min = removal.bounds_mm[2]
        # Flat bottom out to R - Rc = 3 mm at the cut depth, then the torus rises to the full radius.
        for row in range(heights.shape[0]):
            distance = abs(y_min + (row + 0.5) * removal.cell_mm - middle)
            value = float(heights[row, column])
            if distance <= 2.9:
                self.assertAlmostEqual(value, -2.0, places=6)
            elif 3.3 <= distance <= 4.9:
                beyond = distance - 3.0
                self.assertAlmostEqual(value, -2.0 + 2.0 - sqrt(4.0 - beyond**2), places=2)

    def test_a_flat_bottom_still_cuts_a_flat_floor(self) -> None:
        """The profile must not move the numbers a flat mill has always produced."""

        region = build_region("square", {})
        toolpath = _plan("square", "raster", {"stepover_mm": 4.0, "depth_mm": 2.0})
        removal = measure_removal(toolpath, region, _tool(), stepover_mm=4.0)
        self.assertEqual(removal.cusp_mm, 0.0)  # the 6 mm flat bottom spans a 4 mm stepover
        self.assertEqual(removal.floor_tolerance_mm, 0.0)
        self.assertAlmostEqual(removal.floor_mm, -2.0, places=6)
        self.assertGreater(removal.floor_ratio, 0.95)

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
            _plan("square", "raster", {"depth_mm": 2.0}), build_region("square", {}), _tool(),
            stepover_mm=6.0,
        )
        payload = removal.describe()
        self.assertEqual(sorted(payload), sorted([
            "cell_mm", "floor_mm", "region_area_mm2", "removed_volume_mm3", "remaining_volume_mm3",
            "uncut_area_mm2", "floor_ratio", "floor_tolerance_mm", "cusp_mm", "stepover_mm",
            "checkpoints", "bounds_mm", "height_map",
        ]))
        self.assertAlmostEqual(payload["floor_mm"], -2.0, places=4)
        self.assertEqual(payload["stepover_mm"], 6.0)
        self.assertEqual(payload["cusp_mm"], 0.0)  # a flat mill spans a 6 mm stepover
        self.assertEqual(len(payload["bounds_mm"]), 4)
        # The grid has to survive the round trip unchanged, or the overlay loses its depth scale.
        self.assertEqual(
            json.loads(json.dumps(payload))["height_map"], payload["height_map"]
        )


class CheckpointTests(unittest.TestCase):
    """The progressive snapshots the playback scrubs through (one sweep, a few block reductions)."""

    def _measure(self, **options):
        region = build_region("square", {})
        toolpath = _plan(
            "square", "raster",
            {"stepover_mm": 4.0, "depth_mm": 4.0, "stepdown_mm": 2.0},
        )
        return measure_removal(toolpath, region, _tool(), stepover_mm=4.0, **options)

    @staticmethod
    def _removed(map_payload) -> float:
        step_x, step_y = map_payload["cell_size_mm"]
        cells = [value for row in map_payload["cells"] for value in row if value is not None]
        return float(-sum(cells)) * step_x * step_y

    def test_snapshots_follow_the_path_as_it_is_cut(self) -> None:
        removal = self._measure(checkpoints=8)
        self.assertEqual(len(removal.checkpoints), 8)
        indices = [entry["move_index"] for entry in removal.checkpoints]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(len(set(indices)), len(indices))
        progresses = [entry["progress"] for entry in removal.checkpoints]
        for value in progresses:
            self.assertGreater(value, 0.0)
            self.assertLess(value, 1.0)
        self.assertEqual(progresses, sorted(progresses))
        # Material only ever comes off, so the removed volume can never shrink between snapshots.
        volumes = [self._removed(entry["map"]) for entry in removal.checkpoints]
        for before, after in zip(volumes, volumes[1:]):
            self.assertLessEqual(before, after + 1e-6)
        self.assertGreater(volumes[-1], 0.0)
        self.assertLessEqual(volumes[-1], removal.removed_volume_mm3 + 1e-6)

    def test_a_snapshot_never_shows_more_than_the_finished_floor(self) -> None:
        """A snapshot is the floor *so far*: every cell is at or above where it ends up."""

        removal = self._measure(checkpoints=4)
        finished = removal.height_map(max_cells=1024)
        for entry in removal.checkpoints:
            with self.subTest(move_index=entry["move_index"]):
                snapshot = entry["map"]
                self.assertEqual(snapshot["rows"], finished["rows"])
                self.assertEqual(snapshot["cols"], finished["cols"])
                for mine, theirs in zip(snapshot["cells"], finished["cells"]):
                    for first, second in zip(mine, theirs):
                        if first is None or second is None:
                            self.assertEqual(first, second)
                        else:
                            self.assertGreaterEqual(first, second - 1e-9)

    def test_every_snapshot_is_coloured_on_the_finished_scale(self) -> None:
        removal = self._measure(checkpoints=4)
        self.assertAlmostEqual(removal.floor_mm, -4.0, places=6)
        for entry in removal.checkpoints:
            with self.subTest(move_index=entry["move_index"]):
                self.assertAlmostEqual(entry["map"]["floor_mm"], removal.floor_mm, places=6)
                self.assertEqual(entry["map"]["measured_cell_mm"], removal.height_map()["measured_cell_mm"])

    def test_the_snapshot_count_is_capped(self) -> None:
        self.assertLessEqual(len(self._measure(checkpoints=50).checkpoints), MAX_CHECKPOINTS)

    def test_there_are_no_snapshots_unless_they_are_asked_for(self) -> None:
        removal = self._measure()
        self.assertEqual(removal.checkpoints, ())
        self.assertEqual(removal.describe()["checkpoints"], [])

    def test_the_payload_carries_them_in_json(self) -> None:
        payload = self._measure(checkpoints=3).describe()
        self.assertEqual(len(payload["checkpoints"]), 3)
        entry = payload["checkpoints"][0]
        self.assertEqual(sorted(entry), ["map", "move_index", "progress"])
        self.assertIn("cells", entry["map"])
        self.assertIsInstance(entry["move_index"], int)


    def test_a_domed_blank_is_measured_from_its_curved_top(self) -> None:
        """The height map starts on the surface, so the crown counts as material to remove."""

        dome = build_region("dome", {"diameter_mm": 80.0, "dome_height_mm": 12.0})
        tool = _tool()
        depth = 6.0
        toolpath = run_plan(
            planner_id="raster", tool=tool, region=dome,
            parameters={"stepover_mm": 4.0, "depth_mm": depth, "stepdown_mm": 1.0,
                        "sample_step_mm": 1.0},
        ).toolpath
        removal = measure_removal(toolpath, dome, tool, stepover_mm=4.0)
        # A straight cylinder of that depth plus the whole cap: the crown really is in the stock.
        analytic = pi * 40.0**2 * depth + dome.cap_volume_mm3
        self.assertAlmostEqual(removal.removed_volume_mm3, analytic, delta=analytic * 0.01)
        # The very same toolpath on a flat disc removes the cylinder only, so the difference between
        # the two measurements *is* the cap: nothing else about the two regions differs.
        flat = measure_removal(toolpath, build_region("circle", {"diameter_mm": 80.0}), tool,
                               stepover_mm=4.0)
        self.assertAlmostEqual(
            removal.removed_volume_mm3 - flat.removed_volume_mm3,
            dome.cap_volume_mm3,
            delta=analytic * 0.02,
        )
        self.assertAlmostEqual(removal.floor_mm, -depth, delta=0.05)


if __name__ == "__main__":
    unittest.main()
