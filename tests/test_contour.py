"""Contour strategy: layer and loop layout, alternating direction, transitions, infeasible geometry.

The numeric assertions for the offset geometry itself live in tests/test_geometry2d.py.
"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import (
    PLANNERS,
    RAPID_FEED_MM_PER_MIN,
    SAFE_HEIGHT_MM,
    planner_catalog,
    run_plan,
)
from toolpath_lab.planning.geometry2d import point_in_polygon, signed_area


def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _plan(parameters=None, *, shape: str = "square", size: float = 80.0,
          diameter: float = 6.0) -> Toolpath:
    options = {"stepover_mm": 6.0, "sample_step_mm": 1.0, "feed_mm_per_min": 600.0}
    options.update(parameters or {})
    key = "side_mm" if shape == "square" else "diameter_mm"
    return run_plan(
        planner_id="contour",
        tool=_tool(diameter),
        region=build_region(shape, {key: size}),
        parameters=options,
    ).toolpath


def _cut_moves(toolpath: Toolpath):
    return [move for move in toolpath.moves if move.kind is MoveKind.CUT]


def _rings(toolpath: Toolpath):
    """Planar points of every ring (without the repeated closing point)."""

    return [move.points[:-1, :2] for move in _cut_moves(toolpath)]


def _transitions(toolpath: Toolpath):
    return [move.kind for move in toolpath.moves if move.kind is not MoveKind.CUT]


class RegistrationTests(unittest.TestCase):
    def test_contour_is_registered_after_raster(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "contour", "adaptive_contour"])

    def test_catalog_exposes_the_contour_parameters(self) -> None:
        entry = {item["id"]: item for item in planner_catalog()}["contour"]
        self.assertEqual(entry["label"], "环切")
        self.assertEqual(
            [item["key"] for item in entry["parameters"]],
            ["stepover_mm", "sample_step_mm", "ring_direction", "feed_mm_per_min",
             "safe_height_mm", "rapid_feed_mm_per_min"],
        )
        direction = {item["key"]: item for item in entry["parameters"]}["ring_direction"]
        self.assertEqual(direction["default"], "alternate")
        self.assertEqual(
            [choice["value"] for choice in direction["choices"]],
            ["alternate", "climb", "conventional"],
        )


class RingLayoutTests(unittest.TestCase):
    def test_ring_count_follows_the_stepover(self) -> None:
        # 80 mm square, footprint 3, stepover 6: offsets 3/9/.../39 give 7 rings (45 exceeds the inradius 40).
        self.assertEqual(_plan().pass_count, 7)

    def test_smaller_stepover_leaves_more_rings(self) -> None:
        self.assertGreater(
            _plan({"stepover_mm": 3.0}).pass_count, _plan({"stepover_mm": 12.0}).pass_count
        )

    def test_rings_shrink_towards_the_centre(self) -> None:
        radii = [float(np.linalg.norm(ring, axis=1).max()) for ring in _rings(_plan())]
        self.assertEqual(len(radii), 7)
        self.assertTrue(all(later < earlier for earlier, later in zip(radii, radii[1:])))

    def test_first_ring_is_inset_by_the_tool_radius(self) -> None:
        ring = _rings(_plan())[0]
        self.assertAlmostEqual(float(np.abs(ring).max()), 37.0, places=6)

    def test_circle_rings_are_shorter_than_the_square(self) -> None:
        self.assertLess(_plan(shape="circle").cut_length_mm, _plan().cut_length_mm)


class ContourStrategyTests(unittest.TestCase):
    def test_cut_length_is_the_sum_of_the_ring_perimeters(self) -> None:
        # Inset squares 3/9/.../39 have sides 74/62/50/38/26/14/2, so the perimeters sum to 1064
        self.assertAlmostEqual(_plan().cut_length_mm, 1064.0, places=6)

    def test_every_ring_is_closed_and_on_the_machining_plane(self) -> None:
        for move in _cut_moves(_plan()):
            self.assertTrue(bool(np.allclose(move.points[0], move.points[-1])), move.label)
            self.assertTrue(bool(np.allclose(move.points[:, 2], 0.0)))

    def test_neighbouring_rings_run_in_opposite_directions(self) -> None:
        areas = [signed_area(ring) for ring in _rings(_plan())]
        self.assertTrue(all(area > 0.0 for area in areas[::2]))
        self.assertTrue(all(area < 0.0 for area in areas[1::2]))

    def test_nested_rings_are_joined_without_retracting(self) -> None:
        transitions = _transitions(_plan())
        self.assertEqual(transitions.count(MoveKind.LINK), 6)
        # Only two rapids, one at each end: the plunge and the retract.
        self.assertEqual(transitions.count(MoveKind.RAPID), 2)

    def test_rapid_moves_use_the_safe_height_and_rapid_feed(self) -> None:
        rapid = [move for move in _plan().moves if move.kind is MoveKind.RAPID]
        self.assertAlmostEqual(
            max(float(move.points[:, 2].max()) for move in rapid), SAFE_HEIGHT_MM, places=6
        )
        self.assertTrue(
            all(move.feed_mm_per_min == RAPID_FEED_MM_PER_MIN for move in rapid)
        )

    def test_notes_describe_the_rings(self) -> None:
        notes = _plan().notes
        self.assertTrue(any("环切" in note and "7 环" in note for note in notes))
        self.assertTrue(any("交替" in note for note in notes))

    def test_oversized_tool_is_reported_as_unprocessable(self) -> None:
        with self.assertRaises(PlanningError):
            _plan(shape="square", size=40.0, diameter=100.0)

    def test_oversized_tool_on_a_circle_is_unprocessable(self) -> None:
        with self.assertRaises(PlanningError):
            _plan(shape="circle", size=80.0, diameter=80.0)

    def test_response_payload_keeps_the_ring_labels(self) -> None:
        payload = _plan().to_payload()
        self.assertEqual(payload["planner"], "contour")
        self.assertEqual(payload["planner_label"], "环切")
        self.assertEqual(payload["moves"][1]["label"], "第 1 环")
        self.assertEqual(payload["moves"][1]["pass_index"], 0)


class MultiLoopTests(unittest.TestCase):
    """Concave shapes: once the offset eats the neck one layer splits into several loops, each machined alone."""

    def _dumbbell(self, parameters=None) -> Toolpath:
        options = {"stepover_mm": 6.0, "sample_step_mm": 1.0, "feed_mm_per_min": 600.0}
        options.update(parameters or {})
        return run_plan(
            planner_id="contour",
            tool=_tool(),
            region=build_region("dumbbell", {}),
            parameters=options,
        ).toolpath

    def test_the_split_layers_are_cut_as_separate_rings(self) -> None:
        toolpath = self._dumbbell()
        # Offsets 3/9/15/21/27: the neck survives the first two layers (one loop each), the rest split in two.
        self.assertEqual(toolpath.pass_count, 8)
        self.assertIn("8 环", toolpath.notes[0])
        self.assertIn("5 层", toolpath.notes[0])

    def test_every_split_ring_wraps_exactly_one_pad(self) -> None:
        centres = {"left": np.array([[-50.0, 0.0]]), "right": np.array([[50.0, 0.0]])}
        wrapped = []
        for ring in _rings(self._dumbbell()):
            wrapped.append(
                tuple(
                    name for name, point in centres.items()
                    if bool(point_in_polygon(point, ring)[0])
                )
            )
        # While the neck survives, one loop wraps both pads; once split, each loop wraps a single pad.
        self.assertEqual(wrapped.count(("left", "right")), 2)
        self.assertEqual(wrapped.count(("left",)), 3)
        self.assertEqual(wrapped.count(("right",)), 3)

    def test_sibling_rings_are_separated_by_a_retract(self) -> None:
        transitions = _transitions(self._dumbbell())
        # Nested rings are joined by link moves, sibling rings split off in the same layer must retract
        self.assertGreater(transitions.count(MoveKind.RAPID), 2)
        self.assertGreater(transitions.count(MoveKind.LINK), 2)
        rapid = [move for move in self._dumbbell().moves if move.kind is MoveKind.RAPID]
        self.assertTrue(
            all(
                abs(float(move.points[:, 2].max()) - SAFE_HEIGHT_MM) < 1e-6
                for move in rapid
            )
        )

    def test_a_u_shape_still_gets_a_single_ring_per_layer(self) -> None:
        toolpath = run_plan(
            planner_id="contour",
            tool=_tool(),
            region=build_region("u_shape", {}),
            parameters={"stepover_mm": 6.0},
        ).toolpath
        # Wall 25: offsets 3 and 9 give one loop each (arms and floor still 19 and 13 thick), from 15 on gone.
        self.assertEqual(toolpath.pass_count, 2)
        self.assertEqual(_transitions(toolpath).count(MoveKind.LINK), 1)


class RingDirectionTests(unittest.TestCase):
    """Ring direction (climb / conventional / alternating).

    Convention: contouring walks outside in, so unmachined material is on the **inner** side; with an
    M03 spindle and a right-hand tool, counter-clockwise = climb, and offset_loops already returns those.
    """

    def test_climb_runs_every_ring_counter_clockwise(self) -> None:
        areas = [signed_area(ring) for ring in _rings(_plan({"ring_direction": "climb"}))]
        self.assertTrue(all(area > 0.0 for area in areas))

    def test_conventional_runs_every_ring_clockwise(self) -> None:
        areas = [signed_area(ring) for ring in _rings(_plan({"ring_direction": "conventional"}))]
        self.assertTrue(all(area < 0.0 for area in areas))

    def test_alternate_is_the_default_and_keeps_swapping(self) -> None:
        default = [signed_area(ring) for ring in _rings(_plan())]
        explicit = [signed_area(ring) for ring in _rings(_plan({"ring_direction": "alternate"}))]
        self.assertEqual(default, explicit)
        self.assertTrue(all(area > 0.0 for area in default[::2]))
        self.assertTrue(all(area < 0.0 for area in default[1::2]))

    def test_only_the_direction_changes_not_the_geometry(self) -> None:
        reference = _plan({"ring_direction": "alternate"})
        for direction in ("climb", "conventional"):
            with self.subTest(direction=direction):
                toolpath = _plan({"ring_direction": direction})
                self.assertEqual(toolpath.pass_count, reference.pass_count)
                self.assertAlmostEqual(toolpath.cut_length_mm, reference.cut_length_mm, places=6)
                self.assertAlmostEqual(
                    toolpath.estimated_time_s, reference.estimated_time_s, places=6
                )

    def test_sibling_rings_of_a_split_layer_follow_the_same_direction(self) -> None:
        toolpath = run_plan(
            planner_id="contour",
            tool=_tool(),
            region=build_region("dumbbell", {}),
            parameters={"stepover_mm": 6.0, "ring_direction": "climb"},
        ).toolpath
        areas = [signed_area(ring) for ring in _rings(toolpath)]
        self.assertEqual(len(areas), 8)
        self.assertTrue(all(area > 0.0 for area in areas))

    def test_notes_record_the_choice(self) -> None:
        for direction, label in (("climb", "全顺铣"), ("conventional", "全逆铣")):
            with self.subTest(direction=direction):
                notes = _plan({"ring_direction": direction}).notes
                self.assertTrue(any(label in note for note in notes))

    def test_an_unknown_direction_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            _plan({"ring_direction": "climb_ccw"})


class SharpCornerTests(unittest.TestCase):
    """Triangle region: the offset must close into a ring even at a very sharp apex (miters sit far away)."""

    def _plan_triangle(self, width: float, height: float, **parameters) -> Toolpath:
        options = {"stepover_mm": 6.0, "sample_step_mm": 1.0}
        options.update(parameters)
        return run_plan(
            planner_id="contour",
            tool=_tool(),
            region=build_region("triangle", {"width_mm": width, "height_mm": height}),
            parameters=options,
        ).toolpath

    def test_a_sharp_triangle_still_gets_rings(self) -> None:
        # Base 20, height 200 (apex about 5.7 degrees): the miter point sits about 20x the offset away
        toolpath = self._plan_triangle(20.0, 200.0)
        self.assertGreater(toolpath.pass_count, 0)
        for move in _cut_moves(toolpath):
            self.assertTrue(bool(np.allclose(move.points[0], move.points[-1])))
            self.assertGreater(abs(signed_area(move.points[:-1, :2])), 0.0)

    def test_the_first_ring_matches_the_analytic_erosion_area(self) -> None:
        """The erosion of a triangle is a similar triangle: area = original area × ((r − d) / r)², r the inradius."""

        width, height = 20.0, 200.0
        toolpath = self._plan_triangle(width, height)
        area = 0.5 * width * height
        side = float(np.hypot(width / 2.0, height))
        inradius = area / ((width + 2.0 * side) / 2.0)
        expected = area * ((inradius - 3.0) / inradius) ** 2
        first = _rings(toolpath)[0]
        self.assertAlmostEqual(abs(signed_area(first)), expected, delta=expected * 0.03)


if __name__ == "__main__":
    unittest.main()
