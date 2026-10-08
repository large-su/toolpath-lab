"""加工曲面：高度场数学、定义域校验、采样网格与曲面刀路。"""

from __future__ import annotations

import unittest
from math import radians, sqrt, tan

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError, RegistryError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region, points_in_polygon
from toolpath_lab.core.surface import SURFACES, build_surface, surface_catalog
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan
from toolpath_lab.planning.surface_finish import scallop_stepover_mm

_DOME = {"radius_mm": 100.0, "crown_mm": 10.0}


class SurfaceMathTests(unittest.TestCase):
    def test_flat_surface_is_the_reference_plane(self) -> None:
        flat = build_surface("flat")
        self.assertTrue(flat.is_planar)
        self.assertTrue(flat.is_xy_plane)
        self.assertEqual(float(flat.height(3.0, -4.0)), 0.0)

    def test_incline_follows_its_own_direction(self) -> None:
        incline = build_surface("incline", {"angle_deg": 10.0, "direction_deg": 0.0})
        self.assertTrue(incline.is_planar)
        self.assertFalse(incline.is_xy_plane)
        self.assertAlmostEqual(float(incline.height(50.0, 20.0)), tan(radians(10.0)) * 50.0)
        # 与倾斜方向垂直的那条轴上不升高。
        self.assertAlmostEqual(float(incline.height(0.0, 40.0)), 0.0)

    def test_cylinder_and_dome_are_curved_height_fields(self) -> None:
        cylinder = build_surface("cylinder", _DOME)
        dome = build_surface("dome", _DOME)
        self.assertFalse(cylinder.is_planar)
        self.assertAlmostEqual(float(cylinder.height(0.0, 0.0)), 10.0)
        self.assertAlmostEqual(float(dome.height(0.0, 0.0)), 10.0)
        # 冠顶在 z = crown；球（圆弧）心在冠顶下方 R 处，所以在半径处降到 crown − R。
        self.assertAlmostEqual(float(cylinder.height(100.0, 0.0)), 10.0 - 100.0, places=9)
        self.assertAlmostEqual(float(dome.height(100.0, 0.0)), 10.0 - 100.0, places=9)
        # 圆柱面沿 Y 不变，球冠面往两边都降。
        self.assertAlmostEqual(
            float(cylinder.height(30.0, 40.0)), float(cylinder.height(30.0, -10.0))
        )
        self.assertLess(float(dome.height(30.0, 30.0)), float(dome.height(30.0, 0.0)))

    def test_height_accepts_arrays(self) -> None:
        dome = build_surface("dome", _DOME)
        values = dome.height(np.array([0.0, 10.0]), np.array([0.0, 10.0]))
        self.assertEqual(values.shape, (2,))
        self.assertGreater(float(values[0]), float(values[1]))

    def test_curved_surfaces_reject_a_crown_larger_than_the_radius(self) -> None:
        with self.assertRaises(ParameterError):
            build_surface("dome", {"radius_mm": 50.0, "crown_mm": 60.0})
        with self.assertRaises(ParameterError):
            build_surface("cylinder", {"radius_mm": 50.0, "crown_mm": 50.0})

    def test_unknown_surface_id_is_reported(self) -> None:
        with self.assertRaises(RegistryError):
            build_surface("torus")

    def test_catalog_lists_every_registered_surface(self) -> None:
        self.assertEqual(SURFACES.ids(), ["flat", "incline", "cylinder", "dome"])
        entries = {item["id"]: item for item in surface_catalog()}
        self.assertEqual(entries["flat"]["parameters"], [])
        self.assertEqual(
            [item["key"] for item in entries["dome"]["parameters"]],
            ["radius_mm", "crown_mm"],
        )


class DomainTests(unittest.TestCase):
    def test_region_outside_the_domain_is_a_planning_error(self) -> None:
        dome = build_surface("dome", {"radius_mm": 50.0, "crown_mm": 5.0})
        with self.assertRaises(PlanningError):
            dome.ensure_covers(
                np.array([[-40.0, -40.0], [40.0, -40.0], [40.0, 40.0], [-40.0, 40.0]])
            )

    def test_planning_refuses_a_region_larger_than_the_surface(self) -> None:
        with self.assertRaises(PlanningError):
            run_plan(
                planner_id="raster",
                tool=Tool(ToolKind.BALL, 8.0, 40.0),
                region=build_region("square", {"side_mm": 120.0}),
                parameters={"stepover_mm": 5.0},
                surface=build_surface("dome", {"radius_mm": 50.0, "crown_mm": 5.0}),
            )


class MeshTests(unittest.TestCase):
    def test_sample_grid_masks_points_outside_the_region(self) -> None:
        # 圆形的包围盒四角落在区域之外，正好用来验证掩码。
        boundary = build_region("circle", {"diameter_mm": 40.0}).boundary()
        grid = build_surface("dome", _DOME).sample_grid(boundary, 21)
        self.assertEqual(grid["resolution"], 21)
        self.assertEqual(len(grid["x"]), 21)
        self.assertEqual(len(grid["z"]), 21)
        self.assertEqual(len(grid["z"][0]), 21)
        self.assertTrue(grid["inside"][10][10])
        self.assertFalse(grid["inside"][0][0])
        # 网格中心就是球冠的最高点。
        self.assertAlmostEqual(grid["z"][10][10], 10.0, places=3)

    def test_points_in_polygon_uses_the_even_odd_rule(self) -> None:
        square = np.array([[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0]])
        inside = points_in_polygon(square, np.array([[0.0, 0.0], [2.0, 0.0]]))
        self.assertTrue(bool(inside[0]))
        self.assertFalse(bool(inside[1]))


class ScallopTests(unittest.TestCase):
    def test_stepover_formula_inverts_the_cusp_height(self) -> None:
        radius, scallop = 4.0, 0.02
        stepover = scallop_stepover_mm(radius, scallop)
        # 反代回 h = R − sqrt(R² − (ae/2)²) 应当还原残留高度。
        cusp = radius - sqrt(radius * radius - (stepover / 2.0) ** 2)
        self.assertAlmostEqual(cusp, scallop, places=9)

    def test_a_tool_without_a_corner_arc_has_no_finite_stepover(self) -> None:
        self.assertEqual(scallop_stepover_mm(0.0, 0.02), 0.0)

    def test_surface_finish_derives_the_stepover_from_the_scallop(self) -> None:
        dome = build_surface("dome", _DOME)
        outcome = run_plan(
            planner_id="surface_finish",
            tool=Tool(ToolKind.BALL, 8.0, 40.0),
            region=build_region("circle", {"diameter_mm": 40.0}),
            parameters={"scallop_mm": 0.02, "max_stepover_mm": 3.0,
                        "sample_step_mm": 2.0},
            surface=dome,
        )
        cuts = [move for move in outcome.toolpath.moves if move.kind is MoveKind.CUT]
        # 残留高度 0.02、刀尖圆弧半径 4 → 切宽约 0.799 mm，比上限小，因此按反算值走刀。
        expected = scallop_stepover_mm(4.0, 0.02)
        self.assertAlmostEqual(expected, 0.799, places=3)
        # 40 mm 宽的区域按 0.799 mm 布刀 → 约 50 条刀轨（圆的两端各退化掉一条）。
        self.assertGreaterEqual(len(cuts), int(40.0 / expected) - 2)
        self.assertLessEqual(len(cuts), int(40.0 / expected) + 2)
        for move in cuts[::5]:
            height = dome.height(move.points[:, 0], move.points[:, 1])
            np.testing.assert_allclose(move.points[:, 2], height, atol=1e-6)

    def test_flat_tool_falls_back_to_the_stepover_cap_with_a_warning(self) -> None:
        outcome = run_plan(
            planner_id="surface_finish",
            tool=Tool(ToolKind.FLAT, 8.0, 40.0),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"scallop_mm": 0.02},
        )
        self.assertTrue(outcome.warnings)
        self.assertIn("圆鼻刀", "".join(outcome.warnings))

    def test_exceeding_the_cap_is_reported(self) -> None:
        outcome = run_plan(
            planner_id="surface_finish",
            tool=Tool(ToolKind.BALL, 8.0, 40.0),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"scallop_mm": 0.02, "max_stepover_mm": 0.5},
        )
        self.assertTrue(any("上限" in warning for warning in outcome.warnings))


class SurfaceToolpathTests(unittest.TestCase):
    def test_flat_surface_keeps_two_points_per_pass(self) -> None:
        outcome = run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.FLAT, 6.0, 30.0),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"stepover_mm": 6.0},
        )
        cuts = [move for move in outcome.toolpath.moves if move.kind is MoveKind.CUT]
        self.assertTrue(cuts)
        self.assertTrue(all(move.points.shape[0] == 2 for move in cuts))
        self.assertTrue(all(float(np.abs(move.points[:, 2]).max()) == 0.0 for move in cuts))

    def test_curved_surface_densifies_the_passes_and_follows_the_height(self) -> None:
        dome = build_surface("dome", _DOME)
        outcome = run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.BALL, 8.0, 40.0),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"stepover_mm": 5.0, "sample_step_mm": 2.5},
            surface=dome,
        )
        cuts = [move for move in outcome.toolpath.moves if move.kind is MoveKind.CUT]
        self.assertTrue(cuts)
        self.assertTrue(all(move.points.shape[0] > 2 for move in cuts))
        for move in cuts:
            height = dome.height(move.points[:, 0], move.points[:, 1])
            np.testing.assert_allclose(move.points[:, 2], height, atol=1e-6)
        # 有一条刀线经过球冠顶点，因此整条刀路的最高点接近冠高。
        self.assertGreater(max(float(move.points[:, 2].max()) for move in cuts), 9.5)

    def test_rapids_never_dive_into_the_surface(self) -> None:
        dome = build_surface("dome", _DOME)
        outcome = run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.BALL, 8.0, 40.0),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"stepover_mm": 6.0, "mode": "one_way"},
            surface=dome,
        )
        rapids = [move for move in outcome.toolpath.moves if move.kind is MoveKind.RAPID]
        self.assertTrue(rapids)
        for move in rapids:
            floor = dome.height(move.points[:, 0], move.points[:, 1])
            self.assertTrue(bool(np.all(move.points[:, 2] >= floor - 1e-6)))
            self.assertLessEqual(float(move.points[:, 2].max()), 10.0 + 5.0 + 1e-6)

    def test_incline_only_needs_two_points_per_pass(self) -> None:
        incline = build_surface("incline", {"angle_deg": 12.0})
        outcome = run_plan(
            planner_id="raster",
            tool=Tool(ToolKind.BULL, 10.0, 40.0, corner_radius_mm=2.0),
            region=build_region("square", {"side_mm": 40.0}),
            parameters={"stepover_mm": 5.0, "sample_step_mm": 1.0},
            surface=incline,
        )
        cuts = [move for move in outcome.toolpath.moves if move.kind is MoveKind.CUT]
        # 斜面是平面：采样步长再小，一条刀线仍然只要两个端点。
        self.assertTrue(all(move.points.shape[0] == 2 for move in cuts))
        self.assertTrue(any(float(move.points[:, 2].max()) > 3.0 for move in cuts))


if __name__ == "__main__":
    unittest.main()
