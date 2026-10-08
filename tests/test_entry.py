"""Entry moves: how the tool descends to the layer it is about to cut."""

from __future__ import annotations

import unittest
from math import radians, tan

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import planner_catalog, run_plan
from toolpath_lab.planning.base import MOTION_PARAMETERS


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(planner_id: str, parameters: dict | None = None) -> Toolpath:
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0}
    options.update(parameters or {})
    return run_plan(
        planner_id=planner_id, tool=_tool(), region=build_region("square", {}), parameters=options
    ).toolpath


def _entry_moves(toolpath: Toolpath) -> list:
    """The moves before the first real pass: the approach plus any entry move."""

    taken = []
    for move in toolpath.moves:
        if move.pass_index >= 0:
            break
        taken.append(move)
    return taken


class PlungeTests(unittest.TestCase):
    def test_the_default_is_a_single_rapid_plunge(self) -> None:
        toolpath = _plan("contour")
        approaches = _entry_moves(toolpath)
        self.assertEqual(len(approaches), 1)
        first = approaches[0]
        self.assertIs(first.kind, MoveKind.RAPID)
        self.assertEqual(first.points.shape[0], 2)
        self.assertAlmostEqual(float(first.points[0][2]), 5.0)
        self.assertAlmostEqual(float(first.points[1][2]), 0.0)


class RampTests(unittest.TestCase):
    def test_the_ramp_descends_the_layer_depth_at_the_configured_angle(self) -> None:
        toolpath = _plan(
            "contour", {"entry_mode": "ramp", "ramp_angle_deg": 10.0, "depth_mm": 2.0}
        )
        approach, entry = _entry_moves(toolpath)
        self.assertIs(approach.kind, MoveKind.RAPID)
        self.assertAlmostEqual(float(approach.points[1][2]), 2.0)  # down to the layer's depth of cut
        self.assertIs(entry.kind, MoveKind.CUT)
        self.assertEqual(entry.pass_index, -1)  # an entry is not a pass
        self.assertAlmostEqual(float(entry.points[0][2]), 2.0)
        self.assertAlmostEqual(float(entry.points[-1][2]), 0.0)
        travel = float(np.linalg.norm(entry.points[-1][:2] - entry.points[0][:2]))
        self.assertAlmostEqual(travel, 2.0 / tan(radians(10.0)), places=6)

    def test_the_ramp_runs_along_the_first_cut_segment(self) -> None:
        toolpath = _plan("raster", {"entry_mode": "ramp"})
        _approach, entry = _entry_moves(toolpath)
        first_cut = next(move for move in toolpath.moves if move.pass_index >= 0)
        ramp = entry.points[-1][:2] - entry.points[0][:2]
        cut = first_cut.points[1][:2] - first_cut.points[0][:2]
        cosine = float(np.dot(ramp, cut) / (np.linalg.norm(ramp) * np.linalg.norm(cut)))
        self.assertAlmostEqual(cosine, 1.0, places=6)

    def test_the_entry_ends_where_the_cut_starts(self) -> None:
        for planner_id in ("raster", "contour"):
            with self.subTest(planner=planner_id):
                toolpath = _plan(planner_id, {"entry_mode": "ramp", "depth_mm": 3.0})
                _approach, entry = _entry_moves(toolpath)
                first_cut = next(move for move in toolpath.moves if move.pass_index >= 0)
                self.assertTrue(np.allclose(entry.points[-1], first_cut.points[0]))

    def test_the_pass_count_is_unchanged(self) -> None:
        plain = _plan("contour", {"depth_mm": 2.0})
        ramped = _plan("contour", {"entry_mode": "ramp", "depth_mm": 2.0})
        self.assertEqual(ramped.pass_count, plain.pass_count)
        # An entry is a cutting move, so it adds its own ramp length to the cutting length.
        self.assertGreater(ramped.cut_length_mm, plain.cut_length_mm)

    def test_each_layer_gets_its_own_ramp(self) -> None:
        """With step-down the ramp spans the depth of cut of the layer it belongs to."""

        toolpath = _plan("contour", {"entry_mode": "ramp", "depth_mm": 4.0, "stepdown_mm": 2.0})
        starts = sorted(
            round(float(move.points[0][2]), 6)
            for move in toolpath.moves
            if "斜坡进刀" in move.label
        )
        self.assertEqual(starts, [-2.0, 0.0, 2.0])  # sorted: top layer ramps through air (+2)


class HelixTests(unittest.TestCase):
    def test_the_helix_ends_above_its_own_start_and_on_the_cut(self) -> None:
        toolpath = _plan("contour", {"entry_mode": "helix", "depth_mm": 2.0})
        _approach, entry = _entry_moves(toolpath)
        self.assertIs(entry.kind, MoveKind.CUT)
        self.assertEqual(entry.pass_index, -1)
        self.assertGreater(entry.points.shape[0], 16)
        # Whole turns: the last point returns to the XY the helix started above.
        self.assertTrue(np.allclose(entry.points[0][:2], entry.points[-1][:2]))
        self.assertAlmostEqual(float(entry.points[0][2]), 2.0)
        self.assertAlmostEqual(float(entry.points[-1][2]), 0.0)
        first_cut = next(move for move in toolpath.moves if move.pass_index >= 0)
        self.assertTrue(np.allclose(entry.points[-1], first_cut.points[0]))
        # Every point sits on a circle of the helix radius, so the XY spread is its diameter.
        spread = max(
            float(np.linalg.norm(a[:2] - b[:2]))
            for a in entry.points
            for b in entry.points
        )
        self.assertAlmostEqual(spread, 2.0 * 1.5, places=3)

    def test_the_helix_is_clamped_to_the_wall_clearance(self) -> None:
        """A 5 mm helix under a 3 mm radius tool would cut the wall, so it is pulled in."""

        toolpath = _plan(
            "contour", {"entry_mode": "helix", "helix_radius_mm": 5.0, "depth_mm": 2.0}
        )
        _approach, entry = _entry_moves(toolpath)
        spread = max(
            float(np.linalg.norm(a[:2] - b[:2]))
            for a in entry.points
            for b in entry.points
        )
        self.assertLess(spread, 2.0 * 3.0)

    def test_the_descent_is_monotonic(self) -> None:
        _approach, entry = _entry_moves(
            _plan("contour", {"entry_mode": "helix", "depth_mm": 3.0})
        )
        heights = [float(point[2]) for point in entry.points]
        self.assertEqual(heights, sorted(heights, reverse=True))


class ParameterTests(unittest.TestCase):
    def test_the_keys_are_shared_by_every_strategy(self) -> None:
        keys = [item.key for item in MOTION_PARAMETERS]
        for key in ("entry_mode", "ramp_angle_deg", "helix_radius_mm"):
            self.assertIn(key, keys)
        for entry in planner_catalog():
            with self.subTest(planner=entry["id"]):
                planner_keys = [item["key"] for item in entry["parameters"]]
                self.assertIn("entry_mode", planner_keys)
                self.assertIn("ramp_angle_deg", planner_keys)
                self.assertIn("helix_radius_mm", planner_keys)

    def test_the_default_is_a_plunge(self) -> None:
        self.assertEqual(MOTION_PARAMETERS.spec("entry_mode").default, "plunge")

    def test_out_of_range_values_are_rejected(self) -> None:
        for parameters in ({"entry_mode": "spiral"}, {"ramp_angle_deg": 0.0},
                           {"ramp_angle_deg": 90.0}, {"helix_radius_mm": 0.0},
                           {"helix_radius_mm": 100.0}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ParameterError):
                    _plan("contour", parameters)


if __name__ == "__main__":
    unittest.main()
