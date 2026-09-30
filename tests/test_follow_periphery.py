"""跟随周边（等距轮廓环切）：环距、方向、连接与安全高度。"""

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
from toolpath_lab.planning.geometry2d import distance_to_boundary, ensure_ccw


def _tool(diameter: float = 6.0, kind: ToolKind = ToolKind.FLAT) -> Tool:
    return Tool(kind, diameter_mm=diameter, length_mm=30.0)


def _region(shape: str = "square"):
    if shape == "circle":
        return build_region("circle", {"diameter_mm": 80.0})
    return build_region("square", {"side_mm": 80.0})


def _plan(parameters=None, *, shape: str = "square", diameter: float = 6.0,
          kind: ToolKind = ToolKind.FLAT):
    options = {"direction": "inward", "winding": "ccw", "stepover_mm": 6.0,
               "sample_step_mm": 1.0, "feed_mm_per_min": 600.0}
    options.update(parameters or {})
    return run_plan(
        planner_id="follow_periphery",
        tool=_tool(diameter, kind),
        region=_region(shape),
        parameters=options,
    )


def _rings(toolpath: Toolpath):
    return [move for move in toolpath.moves if move.kind is MoveKind.CUT]


def _ring_area(move) -> float:
    points = move.points[:, :2]
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _entry() -> dict:
    return [item for item in planner_catalog() if item["id"] == "follow_periphery"][0]


class RegistryTests(unittest.TestCase):
    def test_both_strategies_are_registered_in_menu_order(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "follow_periphery"])

    def test_catalog_entry_publishes_the_parameters(self) -> None:
        entry = _entry()
        self.assertEqual(entry["label"], "跟随周边")
        self.assertEqual(
            [item["key"] for item in entry["parameters"]],
            ["direction", "winding", "stepover_mm", "sample_step_mm", "feed_mm_per_min",
             "entry", "layer_depth_mm", "stock_margin_mm"],
        )

    def test_catalog_publishes_both_choices(self) -> None:
        parameters = {item["key"]: item for item in _entry()["parameters"]}
        self.assertEqual(
            [choice["value"] for choice in parameters["direction"]["choices"]],
            ["inward", "outward"],
        )
        self.assertEqual(
            [choice["value"] for choice in parameters["winding"]["choices"]],
            ["ccw", "cw"],
        )
        self.assertEqual(parameters["winding"]["default"], "ccw")
        self.assertEqual(parameters["direction"]["default"], "inward")


class RingLayoutTests(unittest.TestCase):
    def test_auto_stepover_is_capped_by_corner_and_centre(self) -> None:
        """切宽填 0 = 自动（直径 − 1 = 5），但转角对角间距与中心覆盖会把它收紧到 直径/2 = 3。"""

        boundary = ensure_ccw(_region().boundary())
        outcome = _plan({"stepover_mm": 0.0})
        rings = _rings(outcome.toolpath)
        distances = [float(distance_to_boundary(move.points[:, :2], boundary).mean())
                     for move in rings]
        self.assertAlmostEqual(distances[0], 0.0, places=6)
        for previous, current in zip(distances, distances[1:]):
            self.assertAlmostEqual(current - previous, 3.0, places=6)
        self.assertTrue(any("收紧" in warning for warning in outcome.warnings))

    def test_ring_spacing_never_exceeds_the_tool_diameter(self) -> None:
        """平行线与转弯处的间距都必须 ≤ 刀具直径（用户要求验证的那条）。

        逐点量"这一环上任意一点到下一环的最近距离"：它同时覆盖平行段与直角转弯处的对角间距，
        也就是"两环之间漏不掉的那条带"的宽度。
        """

        for stepover in (4.0, 6.0, 20.0):
            rings = _rings(_plan({"stepover_mm": stepover}).toolpath)
            for inner, outer in zip(rings[1:], rings[:-1]):
                points = inner.points[:, :2]
                other = outer.points[:, :2]
                gaps = np.linalg.norm(points[:, None, :] - other[None, :, :], axis=2).min(axis=1)
                self.assertLessEqual(float(gaps.max()), 6.0 + 1e-6,
                                     f"切宽 {stepover} 时最大间距 {gaps.max():.3f}")

    def test_innermost_ring_reaches_the_centre(self) -> None:
        """最内环必须落在中心的一个刀具半径之内，否则中心会留一块够不到的料。"""

        rings = _rings(_plan({"stepover_mm": 6.0}).toolpath)
        centre = np.array([[0.0, 0.0]])
        closest = float(distance_to_boundary(centre, ensure_ccw(rings[-1].points[:, :2]))[0])
        self.assertLessEqual(closest, 3.0 + 1e-6, f"最内环离中心 {closest:.3f}")

    def test_outermost_ring_sits_on_the_contour(self) -> None:
        """刀心可以走到轮廓上：第一环就贴着它（离边界 0 < 刀具半径）。"""

        boundary = ensure_ccw(_region().boundary())
        rings = _rings(_plan().toolpath)
        distances = [
            float(distance_to_boundary(move.points[:, :2], boundary).mean())
            for move in rings
        ]
        self.assertGreaterEqual(len(rings), 2)
        self.assertAlmostEqual(distances[0], 0.0, places=6)
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(distances, distances[1:])))
        self.assertAlmostEqual(distances[-1], 39.0, places=6)
        for previous, current in zip(distances, distances[1:]):
            self.assertAlmostEqual(current - previous, 3.0, places=6)

    def test_rings_are_closed_and_lie_on_the_machining_plane(self) -> None:
        for move in _rings(_plan().toolpath):
            self.assertGreater(move.points.shape[0], 3)
            self.assertTrue(np.allclose(move.points[:, 2], 0.0))
            self.assertTrue(np.allclose(move.points[0][:2], move.points[-1][:2]))

    def test_rings_shrink_towards_the_centre(self) -> None:
        spans = [float(np.ptp(move.points[:, 0])) for move in _rings(_plan().toolpath)]
        self.assertEqual(spans, sorted(spans, reverse=True))
        self.assertAlmostEqual(spans[0], 80.0, places=6)

    def test_rings_keep_the_tool_within_one_footprint_of_the_contour(self) -> None:
        boundary = ensure_ccw(_region().boundary())
        for move in _rings(_plan().toolpath):
            closest = float(distance_to_boundary(move.points[:, :2], boundary).min())
            self.assertGreaterEqual(closest, -3.0 - 1e-3)

    def test_larger_stepover_needs_fewer_rings(self) -> None:
        self.assertGreater(
            _plan({"stepover_mm": 2.0}).toolpath.pass_count,
            _plan({"stepover_mm": 12.0}).toolpath.pass_count,
        )

    def test_sample_step_controls_the_point_count(self) -> None:
        coarse = _rings(_plan({"sample_step_mm": 5.0}).toolpath)[0]
        fine = _rings(_plan({"sample_step_mm": 1.0}).toolpath)[0]
        self.assertLess(coarse.points.shape[0], fine.points.shape[0])


class DirectionTests(unittest.TestCase):
    def test_inward_goes_from_the_contour_to_the_centre(self) -> None:
        spans = [float(np.ptp(move.points[:, 0])) for move in _rings(_plan().toolpath)]
        self.assertEqual(spans, sorted(spans, reverse=True))

    def test_outward_goes_from_the_centre_to_the_contour(self) -> None:
        rings = _rings(_plan({"direction": "outward"}).toolpath)
        spans = [float(np.ptp(move.points[:, 0])) for move in rings]
        self.assertEqual(spans, sorted(spans))

    def test_each_ring_matches_between_the_two_directions(self) -> None:
        inward = _rings(_plan({"direction": "inward"}).toolpath)
        outward = _rings(_plan({"direction": "outward"}).toolpath)
        self.assertEqual(len(inward), len(outward))
        for first, second in zip(inward, outward[::-1]):
            self.assertTrue(
                np.allclose(sorted(map(tuple, first.points)), sorted(map(tuple, second.points)))
            )

    def test_both_directions_remove_the_same_material(self) -> None:
        inward = _plan({"direction": "inward"}).toolpath
        outward = _plan({"direction": "outward"}).toolpath
        self.assertAlmostEqual(inward.cut_length_mm, outward.cut_length_mm, places=6)

    def test_the_first_plunge_happens_at_the_contour_for_inward(self) -> None:
        plunge = _plan({"direction": "inward"}).toolpath.moves[0].points[0]
        self.assertGreater(abs(float(plunge[0])), 30.0)

    def test_the_first_plunge_happens_at_the_centre_for_outward(self) -> None:
        plunge = _plan({"direction": "outward"}).toolpath.moves[0].points[0]
        self.assertLess(abs(float(plunge[0])), 5.0)


class LinkingTests(unittest.TestCase):
    def test_rings_are_linked_without_retracting(self) -> None:
        toolpath = _plan().toolpath
        kinds = [move.kind for move in toolpath.moves]
        self.assertEqual(kinds.count(MoveKind.LINK), toolpath.pass_count - 1)
        # 只有首刀下刀与最后抬刀两段快速移动
        self.assertEqual(kinds.count(MoveKind.RAPID), 2)

    def test_links_are_short_radial_steps_at_the_seam(self) -> None:
        toolpath = _plan().toolpath
        rings = _rings(toolpath)
        links = [m for m in toolpath.moves if m.kind is MoveKind.LINK]
        self.assertEqual(len(links), len(rings) - 1)
        for move in links:
            # 沿同一条缝径向过渡一个实际环距（3 mm）；方形角上是斜向，上限取 1.5 倍
            self.assertGreater(move.length_mm, 0.0)
            self.assertLessEqual(move.length_mm, 3.0 * 1.5 + 1e-6)


class WindingTests(unittest.TestCase):
    def test_default_winding_is_counter_clockwise_on_every_ring(self) -> None:
        areas = [_ring_area(move) for move in _rings(_plan().toolpath)]
        self.assertTrue(all(area > 0.0 for area in areas))

    def test_clockwise_reverses_every_ring(self) -> None:
        areas = [_ring_area(move) for move in _rings(_plan({"winding": "cw"}).toolpath)]
        self.assertTrue(all(area < 0.0 for area in areas))

    def test_all_rings_share_a_single_winding(self) -> None:
        for winding in ("ccw", "cw"):
            signs = {
                _ring_area(move) > 0.0
                for move in _rings(_plan({"winding": winding}).toolpath)
            }
            self.assertEqual(len(signs), 1, msg=f"{winding} 的每一环应当同向")

    def test_the_two_windings_cover_the_same_rings(self) -> None:
        counter = _rings(_plan({"winding": "ccw"}).toolpath)
        clockwise = _rings(_plan({"winding": "cw"}).toolpath)
        self.assertEqual(len(counter), len(clockwise))
        for first, second in zip(counter, clockwise):
            self.assertTrue(np.allclose(first.points, second.points[::-1]))

    def test_the_two_windings_cut_the_same_length(self) -> None:
        self.assertAlmostEqual(
            _plan({"winding": "ccw"}).toolpath.cut_length_mm,
            _plan({"winding": "cw"}).toolpath.cut_length_mm,
            places=6,
        )

    def test_winding_also_applies_to_the_outward_order(self) -> None:
        rings = _rings(_plan({"direction": "outward", "winding": "cw"}).toolpath)
        self.assertTrue(all(_ring_area(move) < 0.0 for move in rings))
        spans = [float(np.ptp(move.points[:, 0])) for move in rings]
        self.assertEqual(spans, sorted(spans))


class RegionTests(unittest.TestCase):
    def test_circle_rings_keep_the_tool_within_one_footprint(self) -> None:
        boundary = ensure_ccw(_region("circle").boundary())
        for move in _rings(_plan(shape="circle").toolpath):
            closest = float(distance_to_boundary(move.points[:, :2], boundary).min())
            self.assertGreaterEqual(closest, -3.0 - 1e-3)

    def test_circle_rings_are_shorter_than_the_square_ones(self) -> None:
        self.assertLess(
            _plan(shape="circle").toolpath.cut_length_mm,
            _plan().toolpath.cut_length_mm,
        )


class BallToolTests(unittest.TestCase):
    """球头刀足迹半径为 0，所以第一环就落在轮廓上。"""

    def test_first_ring_is_the_contour_itself(self) -> None:
        boundary = ensure_ccw(_region().boundary())
        rings = _rings(_plan(kind=ToolKind.BALL).toolpath)
        distances = [
            float(distance_to_boundary(move.points[:, :2], boundary).mean())
            for move in rings
        ]
        self.assertGreaterEqual(len(rings), 2)
        self.assertAlmostEqual(distances[0], 0.0, places=6)
        for previous, current in zip(distances, distances[1:]):
            self.assertAlmostEqual(current - previous, 3.0, places=6)

    def test_flat_tool_starts_on_the_contour(self) -> None:
        boundary = ensure_ccw(_region().boundary())
        rings = _rings(_plan().toolpath)
        first = float(distance_to_boundary(rings[0].points[:, :2], boundary).mean())
        self.assertAlmostEqual(first, 0.0, places=6)


class SlopedSurfaceTests(unittest.TestCase):
    """斜面区域：每一环都贴合加工面，折痕处要有顶点。"""

    def _ramp_plan(self, angle: float = 60.0):
        return run_plan(
            planner_id="follow_periphery",
            tool=_tool(kind=ToolKind.BALL),
            region=build_region("ramp", {"side_mm": 80.0, "angle_deg": angle}),
            parameters={"direction": "inward", "winding": "ccw", "stepover_mm": 6.0,
                        "sample_step_mm": 1.0, "feed_mm_per_min": 600.0},
        )

    def test_rings_follow_the_surface_between_the_cap_limits(self) -> None:
        rings = _rings(self._ramp_plan().toolpath)
        self.assertTrue(
            all(float(move.points[:, 2].min()) >= -1e-9 for move in rings)
        )
        self.assertTrue(
            all(float(move.points[:, 2].max()) <= 80.0 + 1e-9 for move in rings)
        )

    def test_ring_vertices_snap_to_the_crease(self) -> None:
        region = build_region("ramp", {"side_mm": 80.0, "angle_deg": 60.0})
        ring = _rings(self._ramp_plan().toolpath)[0]
        at_crease = ring.points[np.isclose(ring.points[:, 0], region.crease_x_mm, atol=1e-9)]
        self.assertGreater(at_crease.shape[0], 0)
        self.assertTrue(np.allclose(at_crease[:, 2], 80.0))

    def test_flat_square_still_has_no_extra_vertices(self) -> None:
        flat = _rings(_plan().toolpath)[0]
        self.assertTrue(np.allclose(flat.points[:, 2], 0.0))


class ErrorAndWarningTests(unittest.TestCase):
    def test_oversized_tool_can_still_machine_the_region(self) -> None:
        """刀具比区域还大也能加工：刀心走到轮廓上，刀盘盖住整块。"""

        self.assertGreaterEqual(_plan(diameter=120.0).toolpath.pass_count, 1)

    def test_large_stepover_is_warned_about(self) -> None:
        outcome = _plan({"stepover_mm": 20.0})
        self.assertTrue(any("收紧" in warning for warning in outcome.warnings))

    def test_sane_stepover_is_not_warned_about(self) -> None:
        self.assertEqual(_plan({"stepover_mm": 3.0}).warnings, ())

    def test_invalid_parameters_are_rejected(self) -> None:
        for bad in ({"stepover_mm": -1.0}, {"direction": "sideways"}, {"winding": "diagonal"},
                    {"sample_step_mm": -1.0}):
            with self.assertRaises(ParameterError):
                _plan(bad)

    def test_notes_describe_the_configuration(self) -> None:
        notes = _plan({"direction": "outward", "winding": "cw", "stepover_mm": 3.0}).toolpath.notes
        self.assertTrue(any("向外" in note for note in notes))
        self.assertTrue(any("顺时针" in note for note in notes))
        self.assertTrue(any("环" in note for note in notes))
        self.assertTrue(any("固定值" in note for note in notes))


class SafetyTests(unittest.TestCase):
    def test_rapid_moves_use_the_fixed_safe_height(self) -> None:
        rapid = [m for m in _plan().toolpath.moves if m.kind is MoveKind.RAPID]
        highest = max(float(move.points[:, 2].max()) for move in rapid)
        self.assertAlmostEqual(highest, SAFE_HEIGHT_MM, places=6)

    def test_rapid_moves_use_the_fixed_rapid_feed(self) -> None:
        rapid = [m for m in _plan().toolpath.moves if m.kind is MoveKind.RAPID]
        self.assertTrue(all(move.feed_mm_per_min == RAPID_FEED_MM_PER_MIN for move in rapid))

    def test_cut_and_link_moves_use_the_requested_feed(self) -> None:
        for move in _plan({"feed_mm_per_min": 900.0}).toolpath.moves:
            if move.kind is not MoveKind.RAPID:
                self.assertEqual(move.feed_mm_per_min, 900.0)

    def test_statistics_are_consistent(self) -> None:
        statistics = _plan().toolpath.statistics()
        moves = _plan().toolpath.moves
        link_length = sum(move.length_mm for move in moves if move.kind is MoveKind.LINK)
        self.assertAlmostEqual(
            statistics["total_length_mm"],
            statistics["cut_length_mm"] + statistics["rapid_length_mm"] + link_length,
            places=6,
        )
        self.assertGreater(statistics["estimated_time_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
