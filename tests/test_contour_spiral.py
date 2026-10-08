"""New finishing strategies: containment, continuity, curve sampling and safe scope."""

import unittest
from unittest.mock import patch

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import CircleRegion, EllipseRegion, PolygonRegion, SquareRegion
from toolpath_lab.core.surface import FlatSurface, build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan
from toolpath_lab.planning.contour_geometry import convex_boundary, inward_offset, offset_rings
from toolpath_lab.simulation import build_timeline


def plan(planner="contour", region=None, surface=None, parameters=None, tool=None, roughing=False):
    return run_plan(planner_id=planner, region=region or SquareRegion(40),
                    surface=surface or FlatSurface(), tool=tool or Tool(), parameters=parameters,
                    roughing={"enabled": roughing})


class ContourSpiralTests(unittest.TestCase):
    def test_closed_contours_shrink_and_preserve_square_corners(self):
        path = plan().toolpath
        cuts = [m for m in path.moves if m.kind is MoveKind.CUT and m.label != "中心收尾"]
        widths = []
        for move in cuts:
            np.testing.assert_allclose(move.points[0], move.points[-1])
            widths.append(np.ptp(move.points[:, 0]))
            self.assertTrue(move.preserve_vertices)
        np.testing.assert_allclose(np.diff(widths), -6)
        self.assertEqual(widths[0], 34)
        self.assertEqual(path.metadata["contour"]["ring_count"], len(cuts))

    def test_spiral_is_one_cut_without_internal_rapid_or_links(self):
        path = plan("spiral").toolpath
        self.assertEqual([m.kind for m in path.moves], [MoveKind.RAPID, MoveKind.CUT, MoveKind.RAPID])
        self.assertEqual(path.pass_count, 1)
        self.assertTrue(path.moves[1].preserve_vertices)
        np.testing.assert_allclose(path.moves[0].points[-1], path.moves[1].points[0])
        np.testing.assert_allclose(path.moves[1].points[-1], path.moves[2].points[0])
        self.assertGreater(path.metadata["spiral"]["transition_count"], 1)

    def test_spiral_starts_with_full_outer_perimeter_and_finishes_at_center(self):
        path = plan("spiral", region=CircleRegion(40)).toolpath
        points = path.moves[1].points[:, :2]
        distances = np.linalg.norm(points, axis=1)
        self.assertGreater(np.count_nonzero(distances > 16.98), 100)
        np.testing.assert_allclose(points[-1], [0, 0], atol=1e-6)
        self.assertTrue(np.all(np.diff(distances) < .02))

    def test_winding_reverses_outer_ring_but_not_center(self):
        for planner in ("contour", "spiral"):
            ccw = plan(planner, parameters={"winding": "ccw"}).toolpath.moves[1].points
            cw = plan(planner, parameters={"winding": "cw"}).toolpath.moves[1].points
            np.testing.assert_allclose(ccw[0], cw[0])
            v0, v1 = ccw[1] - ccw[0], cw[1] - cw[0]
            self.assertLess(float(v0 @ v1), 1e-8)
            self.assertEqual(ccw.shape, cw.shape)

    def test_each_cut_point_inside_boundary_with_footprint_clearance(self):
        regions = (SquareRegion(40), CircleRegion(40), EllipseRegion(70, 30, 35),
                   PolygonRegion(((0, 0), (30, 0), (45, 15), (20, 38), (0, 20))))
        for region in regions:
            polygon, normals = convex_boundary(region.boundary())
            for planner in ("contour", "spiral"):
                with self.subTest(region=region.id, planner=planner):
                    path = plan(planner, region=region).toolpath
                    points = np.vstack([m.points[:, :2] for m in path.moves if m.kind is MoveKind.CUT])
                    for origin, normal in zip(polygon, normals):
                        self.assertGreaterEqual(float(((points - origin) @ normal).min()), 3 - 1e-6)

    def test_collapsed_offsets_are_not_inverted(self):
        polygon, normals = convex_boundary(SquareRegion(10).boundary())
        self.assertIsNone(inward_offset(polygon, normals, 6))
        self.assertIsNone(inward_offset(polygon, normals, 5))
        self.assertIsNotNone(inward_offset(polygon, normals, 4.9))

    def test_narrow_ellipse_offsets_remain_inside_all_halfplanes(self):
        polygon, normals = convex_boundary(EllipseRegion(80, 12, 45).boundary())
        rings = offset_rings(polygon, 1, .5)
        for index, ring in enumerate(rings):
            for origin, normal in zip(polygon, normals):
                self.assertGreaterEqual(float(((ring - origin) @ normal).min()), 1 + index * .5 - 1e-6)

    def test_concave_polygon_rejected_with_actionable_message(self):
        region = PolygonRegion(((0, 0), (20, 0), (20, 8), (8, 8), (8, 20), (0, 20)))
        for planner in ("contour", "spiral"):
            with self.assertRaisesRegex(PlanningError, "凹多边形"):
                plan(planner, region=region)

    def test_oversized_tool_rejected(self):
        for planner in ("contour", "spiral"):
            with self.assertRaisesRegex(PlanningError, "减小刀径"):
                plan(planner, region=SquareRegion(5), tool=Tool(diameter_mm=10))

    def test_ring_budget_fails_explicitly(self):
        with patch("toolpath_lab.planning.contour_geometry.MAX_RINGS", 2):
            with self.assertRaises(PlanningError):
                plan("spiral")

    def test_point_budget_fails_explicitly(self):
        with patch("toolpath_lab.planning.spiral.MAX_PATH_POINTS", 40):
            with self.assertRaisesRegex(PlanningError, "刀点"):
                plan("spiral")

    def test_large_stepover_warns(self):
        outcome = plan("spiral", parameters={"stepover_mm": 12})
        self.assertTrue(any("切宽大于" in message for message in outcome.warnings))

    def test_invalid_parameters_rejected(self):
        for parameters in ({"stepover_mm": 0}, {"sample_step_mm": .01}, {"winding": "invalid"}):
            with self.assertRaises(ParameterError):
                plan("spiral", parameters=parameters)

    def test_all_surfaces_follow_target_and_curve_contour_links_are_safe(self):
        for surface_id in ("flat", "freeform", "saddle", "dome", "radial_ripple", "composite"):
            surface = build_surface(surface_id, {})
            for planner in ("contour", "spiral"):
                with self.subTest(surface=surface_id, planner=planner):
                    path = plan(planner, surface=surface).toolpath
                    for move in path.moves:
                        if move.kind is MoveKind.CUT:
                            np.testing.assert_allclose(move.points[:, 2], surface.height_at(move.points[:, :2]))
                        if surface_id != "flat" and move.kind is MoveKind.RAPID:
                            self.assertAlmostEqual(float(move.points[:, 2].max()), surface.height_bounds()[1] + 5)
                    if surface_id != "flat":
                        self.assertFalse(any(m.kind is MoveKind.LINK for m in path.moves))

    def test_timeline_preserves_corners_and_continuous_moves(self):
        for planner in ("contour", "spiral"):
            path = plan(planner).toolpath
            timeline = build_timeline(path, max_samples=20)
            for move in path.moves:
                if move.kind is MoveKind.CUT:
                    for point in move.points:
                        self.assertTrue(np.any(np.linalg.norm(timeline.positions - point, axis=1) < 1e-7))
            self.assertAlmostEqual(timeline.duration_s, sum(m.duration_s for m in path.moves), places=5)

    def test_roughing_retains_finishing_path_and_metadata(self):
        for planner in ("contour", "spiral"):
            off = plan(planner, surface=build_surface("freeform", {})).toolpath
            on = plan(planner, surface=build_surface("freeform", {}), roughing=True).toolpath
            finish = on.moves[on.metadata["roughing"]["finish_start_move_index"]:]
            self.assertEqual(len(finish), len(off.moves))
            for a, b in zip(off.moves, finish):
                np.testing.assert_array_equal(a.points, b.points)
                np.testing.assert_array_equal(a.tool_axes, b.tool_axes)
            self.assertEqual(on.metadata["contour"], off.metadata["contour"])

    def test_three_tool_types_can_generate_and_export_paths(self):
        for kind in ToolKind:
            tool = Tool(kind, 6, 30, 1 if kind is ToolKind.BULL else 0)
            for planner in ("contour", "spiral"):
                path = plan(planner, tool=tool).toolpath
                code = toolpath_to_gcode(path)
                self.assertIn("G1", code)
                self.assertIn("G0", code)
                self.assertGreater(len(code), 100)


if __name__ == "__main__":
    unittest.main()
