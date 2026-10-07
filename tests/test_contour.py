"""环切策略：层与环的布局、方向交替、环间过渡、几何不可行。

偏置几何本身的数值断言在 tests/test_geometry2d.py。
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
    """每环的平面点（去掉闭合的重复末点）。"""

    return [move.points[:-1, :2] for move in _cut_moves(toolpath)]


def _transitions(toolpath: Toolpath):
    return [move.kind for move in toolpath.moves if move.kind is not MoveKind.CUT]


class RegistrationTests(unittest.TestCase):
    def test_contour_is_registered_after_raster(self) -> None:
        self.assertEqual(PLANNERS.ids(), ["raster", "contour"])

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
        # 80 mm 方形，足迹半径 3，切宽 6 → 偏置 3/9/…/39 共 7 环（45 已超过内切半径 40）。
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

    def test_nested_rings_are_joined_without_retracting(self) -> None:
        transitions = _transitions(_plan())
        self.assertEqual(transitions.count(MoveKind.LINK), 6)
        # 只有首尾各一次快速移动：下刀与抬刀。
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
    """凹形状：细颈被偏置吃掉后一层会分裂成多条环，每块都要单独加工。"""

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
        # 偏置 3/9/15/21/27：前两层细颈还在（各 1 条环），后三层各分裂成 2 条。
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
        # 细颈还在的那两环同时绕过两个方头；分裂出来的环各自只绕一个。
        self.assertEqual(wrapped.count(("left", "right")), 2)
        self.assertEqual(wrapped.count(("left",)), 3)
        self.assertEqual(wrapped.count(("right",)), 3)

    def test_sibling_rings_are_separated_by_a_retract(self) -> None:
        transitions = _transitions(self._dumbbell())
        # 套在里面的环之间用连接进给，同层分裂出的兄弟环之间必须抬刀快移
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
        # 壁厚 25：偏置 3、9 各一条环（6 条臂/底还剩 19、13 厚），15 起整体消失。
        self.assertEqual(toolpath.pass_count, 2)
        self.assertEqual(_transitions(toolpath).count(MoveKind.LINK), 1)


class RingDirectionTests(unittest.TestCase):
    """环绕向（顺铣 / 逆铣 / 交替）。

    约定：环切从外往内走，未加工材料在环**内侧**；按 M03 主轴 + 右手刀具，
    逆时针 = 顺铣。offset_loops 给的环本来就是逆时针，所以"全顺铣"就是保持原样。
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
    """三角形区域：很尖的顶角处偏置也要闭合成环（斜接点能离顶点很远）。"""

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
        # 底边 20、高 200（顶角约 5.7°）：斜接点离顶点约 20×偏置量
        toolpath = self._plan_triangle(20.0, 200.0)
        self.assertGreater(toolpath.pass_count, 0)
        for move in _cut_moves(toolpath):
            self.assertTrue(bool(np.allclose(move.points[0], move.points[-1])))
            self.assertGreater(abs(signed_area(move.points[:-1, :2])), 0.0)

    def test_the_first_ring_matches_the_analytic_erosion_area(self) -> None:
        """三角形的侵蚀还是相似三角形：面积 = 原面积 × ((r − d) / r)²，r 是内切半径。"""

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
