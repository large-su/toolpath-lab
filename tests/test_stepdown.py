"""Step-down: the single plane stacked into layers down to the total depth."""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import planner_catalog, run_plan
from toolpath_lab.planning.base import MOTION_PARAMETERS, PlanningContext
from toolpath_lab.planning.stepdown import apply_stepdown, layer_depths

_SAFE = 5.0


def _tool() -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)


def _plan(shape: str, planner_id: str, parameters: dict | None = None) -> Toolpath:
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0}
    options.update(parameters or {})
    return run_plan(
        planner_id=planner_id, tool=_tool(), region=build_region(shape, {}), parameters=options
    ).toolpath


def _zs(toolpath: Toolpath, kind: MoveKind | None = None) -> set[float]:
    return {
        round(float(point[2]), 6)
        for move in toolpath.moves
        if kind is None or move.kind is kind
        for point in move.points
    }


class LayerDepthTests(unittest.TestCase):
    def test_layers_are_stepped_down_to_the_total_depth(self) -> None:
        self.assertEqual(layer_depths(5.0, 2.0), [0.0, -2.0, -4.0, -5.0])

    def test_the_last_layer_takes_the_remainder(self) -> None:
        self.assertEqual(layer_depths(2.0, 5.0), [0.0, -2.0])

    def test_an_exact_multiple_needs_no_remainder_layer(self) -> None:
        self.assertEqual(layer_depths(6.0, 2.0), [0.0, -2.0, -4.0, -6.0])

    def test_zero_depth_is_a_single_layer_on_the_top_face(self) -> None:
        self.assertEqual(layer_depths(0.0, 2.0), [0.0])
        self.assertEqual(layer_depths(5.0, 0.0), [0.0])


class ApplyTests(unittest.TestCase):
    def test_the_default_is_off(self) -> None:
        toolpath = _plan("square", "raster")
        self.assertIs(
            apply_stepdown(toolpath, depth_mm=0.0, stepdown_mm=2.0), toolpath
        )
        self.assertNotIn(-2.0, _zs(toolpath))
        self.assertFalse(any("分层" in note for note in toolpath.notes))

    def test_every_layer_repeats_the_same_planar_path(self) -> None:
        plain = _plan("square", "raster")
        layered = _plan("square", "raster", {"depth_mm": 5.0})
        self.assertEqual(layered.pass_count, plain.pass_count * 4)
        self.assertEqual(len(layered.moves), len(plain.moves) * 4)
        self.assertAlmostEqual(layered.cut_length_mm, plain.cut_length_mm * 4, places=6)
        first_layer = np.vstack([move.points[:, :2] for move in layered.moves[: len(plain.moves)]])
        whole_plain = np.vstack([move.points[:, :2] for move in plain.moves])
        self.assertTrue(np.allclose(first_layer, whole_plain))

    def test_cutting_happens_at_every_layer_depth(self) -> None:
        layered = _plan("square", "raster", {"depth_mm": 5.0})
        depths = {0.0, -2.0, -4.0, -5.0}
        self.assertEqual(_zs(layered, MoveKind.CUT), depths)
        # Links cut at the same depth as the pass they join (raster emits them too).
        self.assertEqual(_zs(layered, MoveKind.LINK), depths)

    def test_the_clearance_plane_stays_absolute(self) -> None:
        """Safe height means "above the top surface", so deeper layers do not drag it down.

        A rapid's top point is the clearance plane, its bottom point is the layer it plunges to, so
        the Z set of rapids is the plane plus every layer depth.
        """

        layered = _plan("square", "raster", {"depth_mm": 5.0})
        self.assertEqual(max(_zs(layered, MoveKind.RAPID)), _SAFE)
        self.assertEqual(_zs(layered, MoveKind.RAPID), {0.0, -2.0, -4.0, -5.0, _SAFE})
        for depth in (0.0, -2.0, -4.0, -5.0):
            layer = [move for move in layered.moves
                     if move.kind is MoveKind.RAPID and move.points[-1][2] == depth]
            with self.subTest(depth=depth):
                self.assertTrue(layer)
                self.assertTrue(all(move.points[0][2] == _SAFE for move in layer))

    def test_pass_indices_are_distinct_per_layer(self) -> None:
        layered = _plan("square", "raster", {"depth_mm": 5.0})
        indices = sorted({move.pass_index for move in layered.moves if move.pass_index >= 0})
        self.assertEqual(indices, list(range(56)))  # 14 passes x 4 layers
        self.assertEqual(layered.pass_count, 56)

    def test_moves_are_labelled_with_their_layer(self) -> None:
        layered = _plan("square", "raster", {"depth_mm": 5.0})
        labels = [move.label for move in layered.moves if move.kind is MoveKind.CUT]
        self.assertEqual(labels[0], "第 1 层 第 1 刀")
        self.assertEqual(labels[-1], "第 4 层 第 14 刀")

    def test_the_estimate_grows_with_the_layers(self) -> None:
        plain = _plan("square", "raster")
        layered = _plan("square", "raster", {"depth_mm": 5.0})
        self.assertGreater(layered.estimated_time_s, plain.estimated_time_s)
        self.assertTrue(any("分层" in note for note in layered.notes))

    def test_it_composes_with_corner_slowdown(self) -> None:
        layered = _plan(
            "square", "contour", {"depth_mm": 4.0, "corner_angle_deg": 30.0}
        )
        feeds = {move.feed_mm_per_min for move in layered.moves if move.kind is MoveKind.CUT}
        self.assertEqual(len(feeds), 2)  # the slowed corners and the straight stretches
        self.assertEqual(_zs(layered, MoveKind.CUT), {0.0, -2.0, -4.0})

    def test_a_single_move_toolpath_keeps_its_geometry(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]), 600.0,
                     pass_index=0, label="一刀"),
            )
        )
        layered = apply_stepdown(toolpath, depth_mm=2.0, stepdown_mm=2.0)
        self.assertEqual(len(layered.moves), 2)
        self.assertEqual([move.points[0][2] for move in layered.moves], [0.0, -2.0])
        self.assertEqual([move.label for move in layered.moves], ["第 1 层 一刀", "第 2 层 一刀"])

    def test_coverage_is_a_planar_measurement(self) -> None:
        """The layers repeat the same XY path, so the planar number does not change."""

        from toolpath_lab.planning import measure_coverage

        region = build_region("square", {})
        plain = _plan("square", "raster")
        layered = _plan("square", "raster", {"depth_mm": 5.0})
        self.assertAlmostEqual(
            measure_coverage(plain, region, _tool()).ratio,
            measure_coverage(layered, region, _tool()).ratio,
            places=9,
        )


class ParameterTests(unittest.TestCase):
    def test_the_keys_are_shared_by_every_strategy(self) -> None:
        keys = [item.key for item in MOTION_PARAMETERS]
        self.assertIn("depth_mm", keys)
        self.assertIn("stepdown_mm", keys)
        for entry in planner_catalog():
            with self.subTest(planner=entry["id"]):
                planner_keys = [item["key"] for item in entry["parameters"]]
                self.assertIn("depth_mm", planner_keys)
                self.assertIn("stepdown_mm", planner_keys)

    def test_a_context_without_the_keys_falls_back_to_the_defaults(self) -> None:
        context = PlanningContext(tool=_tool(), region=build_region("square", {}), parameters={})
        self.assertEqual(context.depth_mm, 0.0)
        self.assertAlmostEqual(context.stepdown_mm, 2.0)

    def test_out_of_range_values_are_rejected(self) -> None:
        for parameters in ({"depth_mm": -1.0}, {"depth_mm": 500.0}, {"stepdown_mm": 0.0},
                           {"stepdown_mm": 100.0}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    _plan("square", "raster", parameters)


if __name__ == "__main__":
    unittest.main()
