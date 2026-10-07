"""环切策略与它自带的等距偏置几何。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import PlanningError
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
from toolpath_lab.planning.contour import offset_polygon, resample_ring
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area

_SQUARE_80 = ensure_ccw(build_region("square", {"side_mm": 80.0}).boundary())


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
    """每环的平面点（去掉闭合的重复末点）。"""

    return [move.points[:-1, :2] for move in _cut_moves(toolpath)]


class RegistrationTests(unittest.TestCase):
    def test_contour_is_registered_after_raster(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "contour"])

    def test_catalog_exposes_the_contour_parameters(self) -> None:
        entry = {item["id"]: item for item in planner_catalog()}["contour"]
        self.assertEqual(entry["label"], "环切")
        self.assertEqual(
            [item["key"] for item in entry["parameters"]],
            ["stepover_mm", "sample_step_mm", "feed_mm_per_min"],
        )


class OffsetGeometryTests(unittest.TestCase):
    def test_square_inset_stays_an_exact_square(self) -> None:
        ring = offset_polygon(_SQUARE_80, 10.0)
        self.assertIsNotNone(ring)
        self.assertEqual(ring.shape, (4, 2))
        self.assertAlmostEqual(signed_area(ring), 3600.0, places=6)
        self.assertAlmostEqual(float(np.abs(ring).max()), 30.0, places=6)

    def test_circle_inset_keeps_the_offset_distance(self) -> None:
        circle = ensure_ccw(build_region("circle", {"diameter_mm": 80.0}).boundary())
        ring = offset_polygon(circle, 10.0)
        self.assertIsNotNone(ring)
        radii = np.linalg.norm(ring, axis=1)
        # 向外 40 - 向内 10 = 30（多边形逼近与斜接带来约 1.5e-3 的偏差）。
        self.assertAlmostEqual(float(radii.min()), 30.0, delta=0.01)
        self.assertAlmostEqual(float(radii.max()), 30.0, delta=0.01)

    def test_offset_beyond_the_inradius_degenerates(self) -> None:
        self.assertIsNone(offset_polygon(_SQUARE_80, 41.0))

    def test_zero_offset_returns_the_normalised_polygon(self) -> None:
        ring = offset_polygon(_SQUARE_80, 0.0)
        self.assertAlmostEqual(signed_area(ring), 6400.0, places=6)

    def test_negative_offset_is_rejected(self) -> None:
        # 向外偏置需要另一套规则（凸角补圆弧、凹角斜接），这里明确不做，
        # 否则会悄悄返回一条跑到区域外面的环。
        with self.assertRaises(ValueError):
            offset_polygon(_SQUARE_80, -10.0)


class ResampleTests(unittest.TestCase):
    def test_ring_is_resampled_at_even_arc_length(self) -> None:
        sampled = resample_ring(_SQUARE_80, 5.0)
        self.assertEqual(sampled.shape[0], 64)  # 周长 320 / 5
        closed = np.vstack([sampled, sampled[:1]])
        gaps = np.linalg.norm(np.diff(closed, axis=0), axis=1)
        self.assertTrue(bool(np.allclose(gaps, 5.0, atol=1e-9)))

    def test_resampled_ring_does_not_repeat_the_first_point(self) -> None:
        sampled = resample_ring(_SQUARE_80, 5.0)
        self.assertFalse(bool(np.allclose(sampled[0], sampled[-1])))

    def test_resampling_keeps_the_length(self) -> None:
        sampled = resample_ring(_SQUARE_80, 1.0)
        closed = np.vstack([sampled, sampled[:1]])
        self.assertAlmostEqual(
            float(np.linalg.norm(np.diff(closed, axis=0), axis=1).sum()), 320.0, places=6
        )


class RingLayoutTests(unittest.TestCase):
    def test_ring_count_follows_the_stepover(self) -> None:
        # 80 mm 方形，足迹半径 3，切宽 6 → 偏置 3/9/…/39 共 7 环（45 已超过内切半径 40）。
        self.assertEqual(_plan().pass_count, 7)

    def test_smaller_stepover_leaves_fewer_rings(self) -> None:
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
        # 内缩 3/9/…/39 的方形边长 74/62/50/38/26/14/2 → 周长和 1064
        self.assertAlmostEqual(_plan().cut_length_mm, 1064.0, places=6)

    def test_every_ring_is_closed_and_on_the_machining_plane(self) -> None:
        for move in _cut_moves(_plan()):
            self.assertTrue(bool(np.allclose(move.points[0], move.points[-1])), move.label)
            self.assertTrue(bool(np.allclose(move.points[:, 2], 0.0)))

    def test_neighbouring_rings_run_in_opposite_directions(self) -> None:
        areas = [signed_area(ring) for ring in _rings(_plan())]
        self.assertTrue(all(area > 0.0 for area in areas[::2]))
        self.assertTrue(all(area < 0.0 for area in areas[1::2]))

    def test_rings_are_joined_without_retracting(self) -> None:
        kinds = [move.kind for move in _plan().moves]
        self.assertEqual(kinds.count(MoveKind.LINK), 6)
        # 只有首尾各一次快速移动：下刀与抬刀。
        self.assertEqual(kinds.count(MoveKind.RAPID), 2)

    def test_rapid_moves_use_the_fixed_safe_height_and_feed(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
