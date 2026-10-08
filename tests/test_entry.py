"""Entry moves: how the tool descends to the layer it is about to cut."""

from __future__ import annotations

import unittest
from math import ceil, hypot, pi, radians, sin, tan

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath, polyline_length
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import planner_catalog, run_plan
from toolpath_lab.planning.base import MOTION_PARAMETERS
from toolpath_lab.planning.entry import measure_entry


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


class NoteTests(unittest.TestCase):
    """The entry length reaches the plan's notes: a shallow ramp is a long cut."""

    @staticmethod
    def _entry_note(toolpath: Toolpath) -> str:
        return next(note for note in toolpath.notes if note.startswith("进刀："))

    def test_a_plunge_says_nothing(self) -> None:
        toolpath = _plan("contour", {"depth_mm": 2.0})
        self.assertFalse([note for note in toolpath.notes if note.startswith("进刀：")])
        self.assertIsNone(measure_entry(toolpath, build_region("square", {})))

    def test_a_ramp_reports_its_analytic_length(self) -> None:
        toolpath = _plan("contour", {"entry_mode": "ramp", "ramp_angle_deg": 10.0, "depth_mm": 2.0})
        note = self._entry_note(toolpath)
        self.assertIn("斜坡", note)
        self.assertIn("每段 11.52 mm", note)
        # A ramp is one straight 3D segment, so its length is depth / sin(angle) -- not the planar
        # depth / tan(angle) the tool walks along: the vertical part counts too.
        summary = measure_entry(toolpath, build_region("square", {}))
        self.assertAlmostEqual(summary.longest_mm, 2.0 / sin(radians(10.0)), places=6)

    def test_the_note_reports_what_the_statistics_counted(self) -> None:
        """The note claims the entries are part of the cutting length, so that has to hold."""

        ramped = _plan("contour", {"entry_mode": "ramp", "depth_mm": 4.0, "stepdown_mm": 2.0})
        plain = _plan("contour", {"depth_mm": 4.0, "stepdown_mm": 2.0})
        summary = measure_entry(ramped, build_region("square", {}))
        self.assertEqual(summary.count, 3)  # one per layer: 0, -2, -4
        self.assertIn("3 段", self._entry_note(ramped))
        self.assertAlmostEqual(summary.total_mm, 3 * 2.0 / sin(radians(10.0)), places=6)
        self.assertAlmostEqual(
            summary.total_mm, ramped.cut_length_mm - plain.cut_length_mm, places=6
        )

    def test_a_helix_reports_whole_turns_of_travel(self) -> None:
        """The pitch follows the angle and the entry ends above its own start, so turns are whole."""

        radius = 1.5
        pitch = 2.0 * pi * radius * tan(radians(10.0))
        expected = hypot(ceil(2.0 / pitch) * 2.0 * pi * radius, 2.0)
        toolpath = _plan("contour", {"entry_mode": "helix", "depth_mm": 2.0})
        self.assertIn("螺旋", self._entry_note(toolpath))
        reported = measure_entry(toolpath, build_region("square", {})).longest_mm
        # A helix is emitted as a sampled polyline (the machine descends in straight segments), so the
        # measured length sits a hair under the arc -- 48 samples per turn keep it within 0.1 %.
        self.assertLess(reported, expected)
        self.assertAlmostEqual(reported, expected, delta=expected * 0.001)

    def test_a_long_entry_says_how_far_it_leaves_the_region(self) -> None:
        """Entries are not clipped to the outline, so a shallow one walks out of the part."""

        toolpath = _plan("contour", {"entry_mode": "ramp", "ramp_angle_deg": 1.0, "depth_mm": 2.0})
        summary = measure_entry(toolpath, build_region("square", {}))
        # 2 mm down at 1° is 114.6 mm of travel backwards from a start inset by the tool radius (3 mm).
        self.assertAlmostEqual(summary.outside_mm, 2.0 / tan(radians(1.0)) - 3.0, delta=0.5)
        self.assertIn("在区域轮廓之外", self._entry_note(toolpath))

    def test_a_steep_entry_stays_inside_and_says_nothing_about_it(self) -> None:
        toolpath = _plan("contour", {"entry_mode": "ramp", "ramp_angle_deg": 45.0, "depth_mm": 2.0})
        summary = measure_entry(toolpath, build_region("square", {}))
        self.assertAlmostEqual(summary.longest_mm, 2.0 / sin(radians(45.0)), places=6)
        self.assertLess(summary.outside_mm, 1e-6)
        self.assertNotIn("在区域轮廓之外", self._entry_note(toolpath))

    def test_entry_segments_from_one_descent_count_as_one(self) -> None:
        """Corner slowdown or a plugin may cut an entry into pieces; it is still one entry."""

        points = np.array([[-10.0, 0.0, 2.0], [0.0, 0.0, 1.0], [10.0, 0.0, 0.0]])
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, points[:2], 600.0, pass_index=-1, label="斜坡进刀"),
                Move(MoveKind.CUT, points[1:], 600.0, pass_index=-1, label="斜坡进刀"),
                Move(MoveKind.CUT, np.array([[10.0, 0.0, 0.0], [30.0, 0.0, 0.0]]), 600.0,
                     pass_index=0),
            )
        )
        summary = measure_entry(toolpath, build_region("square", {}))
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.modes, ("斜坡",))
        self.assertAlmostEqual(summary.total_mm, polyline_length(points), places=6)

    def test_an_entry_this_module_cannot_name_is_still_measured(self) -> None:
        """A plugin may build its own entry: the length is real even when the name is not ours."""

        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[0.0, 0.0, 2.0], [3.0, 0.0, -2.0]]), 600.0,
                     pass_index=-1),
                Move(MoveKind.CUT, np.array([[3.0, 0.0, -2.0], [30.0, 0.0, -2.0]]), 600.0,
                     pass_index=0),
            )
        )
        summary = measure_entry(toolpath, build_region("square", {}))
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.modes, ())
        self.assertAlmostEqual(summary.total_mm, hypot(3.0, 4.0), places=6)
        self.assertTrue(summary.note().startswith("进刀：1 段"))

    def test_entries_of_different_lengths_are_reported_as_a_range(self) -> None:
        toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[0.0, 0.0, 2.0], [2.0, 0.0, 0.0]]), 600.0,
                     pass_index=-1, label="斜坡进刀"),
                Move(MoveKind.CUT, np.array([[2.0, 0.0, 0.0], [30.0, 0.0, 0.0]]), 600.0,
                     pass_index=0),
                Move(MoveKind.CUT, np.array([[30.0, 0.0, 2.0], [38.0, 0.0, 0.0]]), 600.0,
                     pass_index=-1, label="斜坡进刀"),
                Move(MoveKind.CUT, np.array([[38.0, 0.0, 0.0], [50.0, 0.0, 0.0]]), 600.0,
                     pass_index=1),
            )
        )
        summary = measure_entry(toolpath, build_region("square", {}))
        self.assertEqual(summary.count, 2)
        self.assertAlmostEqual(summary.shortest_mm, hypot(2.0, 2.0), places=6)
        self.assertAlmostEqual(summary.longest_mm, hypot(8.0, 2.0), places=6)
        self.assertIn("~", summary.note())
        self.assertIn("2 段", summary.note())


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
