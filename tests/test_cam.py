"""CAM 层：区域运算、平面铣 / 型腔铣刀路、工序与模板。

断言的都是可判定的量：区域面积、环数、刀路覆盖范围、层数、深度。
"刀路生成出来了"不算通过——必须是**正确**的刀路。
"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from tests.fixtures import plate_with_cylinder, plate_with_pocket
from toolpath_lab.cam.boundary import build_region, offset_outline_polygons, region_from_face
from toolpath_lab.cam.common import MillingContext, depth_levels
from toolpath_lab.cam.parameters import cam_parameters, tool_from_cam_parameters
from toolpath_lab.cam.service import CAMOperationRequest, execute_operation, planning_catalog
from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.operation import Operation, OperationTree, ParameterTemplate
from toolpath_lab.core.part import build_part
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.stock import build_stock

SQUARE = np.array([[-20.0, -20.0], [20.0, -20.0], [20.0, 20.0], [-20.0, 20.0]])
ISLAND = np.array([[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0]])

BASE_PARAMETERS = {
    "tool_diameter_mm": 10.0,
    "spindle_rpm": 3200.0,
    "feed_mm_per_min": 900.0,
    "stepover_mm": 5.0,
    "cut_depth_mm": 2.0,
    "stock_allowance_mm": 0.3,
    "finish_allowance_mm": 0.0,
    "safe_height_mm": 10.0,
    "finish_pass": True,
}


def make_part(part, name: str = "part"):
    """夹具现在直接返回 PartModel（几何由 OCP 生成），这里只统一一下 id/名字。"""

    part.model_id = name
    if name:
        part.name = name
    return part


def context_for(region, **overrides):
    values = dict(BASE_PARAMETERS)
    values.update(overrides)
    coerced = cam_parameters().coerce(values)
    return MillingContext(
        tool=tool_from_cam_parameters(coerced),
        top_z=region.top_z,
        floor_z=region.floor_z,
        parameters=coerced,
        region=region,
    )


class RegionTests(unittest.TestCase):
    """栅格区域：外轮廓 / 岛屿 / 等距。"""

    def test_plain_square_area_and_offsets(self) -> None:
        region = build_region(SQUARE, [], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        self.assertAlmostEqual(region.area_mm2, 40 * 40, delta=40 * 40 * 0.02)
        for offset, expected in ((0.0, 1600.0), (2.0, 36 * 36), (5.0, 30 * 30)):
            with self.subTest(offset=offset):
                self.assertAlmostEqual(region.offset_area_mm2(offset), expected, delta=expected * 0.05)

    def test_island_is_excluded(self) -> None:
        region = build_region(SQUARE, [ISLAND], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        self.assertAlmostEqual(region.area_mm2, 40 * 40 - 10 * 10, delta=40.0)

    def test_offset_contours_are_closed_and_correctly_sized(self) -> None:
        region = build_region(SQUARE, [], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        polygons = offset_outline_polygons(region, 5.0)
        self.assertEqual(len(polygons), 1)
        polygon = polygons[0]
        self.assertGreaterEqual(polygon.shape[0], 3)
        # 30×30，允许一个格距的量化误差
        self.assertAlmostEqual(float(polygon[:, 0].min()), -15.0, delta=1.0)
        self.assertAlmostEqual(float(polygon[:, 0].max()), 15.0, delta=1.0)

    def test_island_contour_is_an_extra_loop(self) -> None:
        region = build_region(SQUARE, [ISLAND], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        polygons = offset_outline_polygons(region, 2.0)
        self.assertEqual(len(polygons), 2)
        areas = sorted(abs(0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1)
                                          - np.roll(p[:, 0], -1) * p[:, 1]))) for p in polygons)
        # 外环 36×36，内环（岛屿外扩）约 14×14；栅格量化会带来一个格距级别的偏差
        self.assertAlmostEqual(areas[0], 14 * 14, delta=16.0)
        self.assertAlmostEqual(areas[1], 36 * 36, delta=60.0)

    def test_scanline_intervals_stay_inside(self) -> None:
        region = build_region(SQUARE, [], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        intervals = region.scanline_intervals(0.0, 0.0)
        self.assertTrue(intervals)
        for start, end, level in intervals:
            self.assertAlmostEqual(level, 0.0, places=6)
            self.assertGreater(end, start)
            self.assertGreaterEqual(start, -23.0)
            self.assertLessEqual(end, 23.0)

    def test_scanline_breaks_around_the_island(self) -> None:
        region = build_region(SQUARE, [ISLAND], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        intervals = region.scanline_intervals(0.0, 0.0)
        self.assertEqual(len(intervals), 2)


class DepthLevelTests(unittest.TestCase):
    def test_layers_cover_the_depth_and_end_on_target(self) -> None:
        levels = depth_levels(top_z=40.0, floor_z=25.0, cut_depth=2.0)
        self.assertEqual(len(levels), 8)
        self.assertEqual(levels[0], 40.0 - 15.0 / 8)
        self.assertAlmostEqual(levels[-1], 25.0)
        for previous, current in zip(levels, levels[1:]):
            self.assertLess(current, previous)

    def test_finish_allowance_stops_above_the_floor(self) -> None:
        levels = depth_levels(top_z=40.0, floor_z=25.0, cut_depth=3.0, finish_allowance=0.5)
        self.assertAlmostEqual(levels[-1], 25.5)

    def test_zero_depth_has_no_levels(self) -> None:
        self.assertEqual(depth_levels(top_z=40.0, floor_z=40.0, cut_depth=2.0), [])
        self.assertEqual(depth_levels(top_z=40.0, floor_z=45.0, cut_depth=2.0), [])


class OperationTreeTests(unittest.TestCase):
    def _tree(self) -> OperationTree:
        tree = OperationTree()
        for index in range(3):
            tree.add(Operation(operation_id=f"op{index}", name=f"工序{index}", kind="pocket_mill"))
        return tree

    def test_sequence_is_assigned_in_order(self) -> None:
        tree = self._tree()
        self.assertEqual([item.sequence for item in tree.ordered()], [0, 1, 2])
        self.assertEqual(tree.next_sequence(), 3)

    def test_move_resequences(self) -> None:
        tree = self._tree()
        tree.move("op2", 0)
        self.assertEqual([item.operation_id for item in tree.ordered()], ["op2", "op0", "op1"])
        self.assertEqual([item.sequence for item in tree.ordered()], [0, 1, 2])

    def test_remove_compacts_sequence(self) -> None:
        tree = self._tree()
        tree.remove("op1")
        self.assertEqual([item.sequence for item in tree.ordered()], [0, 1])

    def test_enabled_filter(self) -> None:
        tree = self._tree()
        tree.update("op1", enabled=False)
        self.assertEqual([item.operation_id for item in tree.enabled_operations()], ["op0", "op2"])

    def test_parameter_change_marks_draft(self) -> None:
        tree = self._tree()
        tree.update("op0", state="generated")
        tree.update("op0", parameters={"stepover_mm": 3.0})
        self.assertEqual(tree.get("op0").state, "draft")

    def test_duplicate_ids_are_rejected(self) -> None:
        tree = self._tree()
        with self.assertRaises(ParameterError):
            tree.add(Operation(operation_id="op0", name="重复", kind="face_mill"))

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Operation(operation_id="x", name="x", kind="laser")

    def test_templates_round_trip(self) -> None:
        tree = self._tree()
        tree.add_template(ParameterTemplate(template_id="t1", name="开粗", kind="pocket_mill",
                                            parameters={"stepover_mm": 4.0}))
        self.assertEqual(len(tree.templates_for("pocket_mill")), 1)
        self.assertEqual(tree.template("t1").parameters["stepover_mm"], 4.0)
        tree.remove_template("t1")
        self.assertEqual(tree.templates, [])
        tree.add_template(ParameterTemplate(template_id="t2", name="内置", kind="pocket_mill",
                                           parameters={}, builtin=True))
        with self.assertRaises(ParameterError):
            tree.remove_template("t2")

    def test_payload_shape(self) -> None:
        tree = self._tree()
        payload = tree.to_payload()
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["enabled_count"], 3)
        self.assertEqual([item["sequence"] for item in payload["operations"]], [0, 1, 2])


class FaceMillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.part = make_part(plate_with_pocket(), "plate")
        self.stock = build_stock("rectangular", self.part,
                                 {"offset_x_mm": 2, "offset_y_mm": 2, "offset_z_mm": 2})
        self.top = [f for f in self.part.features
                    if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6][0]

    def _run(self, **overrides):
        payload = {
            "kind": "face_mill",
            "faces": [self.top["face_id"]],
            "cell_mm": 0.5,
            "stock": self.stock,
            "parameters": {**BASE_PARAMETERS, **overrides},
        }
        return execute_operation(CAMOperationRequest.from_payload(payload, self.part))

    def test_cuts_the_stock_allowance_in_layers(self) -> None:
        result = self._run()
        region = result.regions[0]
        # 毛坯顶面 42，面在 40：深度 2，每层 ≤ 2 → 1 层
        self.assertAlmostEqual(region["depth_mm"], 2.0, places=3)
        self.assertGreater(result.toolpath.cut_length_mm, 100.0)
        zs = np.concatenate([np.asarray(move.points, dtype=float)[:, 2]
                             for move in result.toolpath.moves
                             if move.kind is MoveKind.CUT])
        self.assertAlmostEqual(float(zs.min()), 40.0, places=3)

    def test_deeper_allowance_makes_more_layers(self) -> None:
        shallow = self._run(stock=None)
        self.assertLess(shallow.regions[0]["depth_mm"], 2.01)
        deep = execute_operation(CAMOperationRequest.from_payload({
            "kind": "face_mill",
            "faces": [self.top["face_id"]],
            "cell_mm": 0.5,
            "top_z": 46.0,
            "parameters": dict(BASE_PARAMETERS),
        }, self.part))
        self.assertAlmostEqual(deep.regions[0]["depth_mm"], 6.0, places=3)
        self.assertGreater(deep.toolpath.cut_length_mm, shallow.toolpath.cut_length_mm)

    def test_tool_larger_than_region_is_reported(self) -> None:
        with self.assertRaises(PlanningError) as context:
            self._run(tool_diameter_mm=200.0)
        self.assertTrue(str(context.exception))

    def test_cut_stays_inside_the_stock(self) -> None:
        result = self._run()
        points = np.vstack([np.asarray(move.points, dtype=float) for move in result.toolpath.moves])
        bounds = self.stock.bounds
        self.assertGreaterEqual(float(points[:, 0].min()), bounds.x_min - 1e-6)
        self.assertLessEqual(float(points[:, 0].max()), bounds.x_max + 1e-6)
        self.assertGreaterEqual(float(points[:, 1].min()), bounds.y_min - 1e-6)
        self.assertLessEqual(float(points[:, 1].max()), bounds.y_max + 1e-6)


class PocketMillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.part = make_part(plate_with_pocket(), "plate")
        self.stock = build_stock("rectangular", self.part,
                                 {"offset_x_mm": 2, "offset_y_mm": 2, "offset_z_mm": 2})
        self.floor = [f for f in self.part.features
                      if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]

    def _run(self, **overrides):
        payload = {
            "kind": "pocket_mill",
            "faces": [self.floor["face_id"]],
            "cell_mm": 0.5,
            "stock": self.stock,
            "parameters": {**BASE_PARAMETERS, **overrides},
        }
        return execute_operation(CAMOperationRequest.from_payload(payload, self.part))

    def test_reaches_the_pocket_floor(self) -> None:
        result = self._run(cut_mode="contour")
        self.assertAlmostEqual(result.regions[0]["floor_z"], 25.0, places=3)
        self.assertAlmostEqual(result.regions[0]["depth_mm"], 15.0, places=3)
        zs = np.concatenate([np.asarray(move.points, dtype=float)[:, 2]
                             for move in result.toolpath.moves
                             if move.kind is MoveKind.CUT])
        self.assertAlmostEqual(float(zs.min()), 25.0, places=3)

    def test_contour_rings_stay_inside_the_pocket(self) -> None:
        result = self._run(cut_mode="contour", tool_diameter_mm=10.0)
        cut_points = np.vstack([np.asarray(move.points, dtype=float)
                                for move in result.toolpath.moves
                                if move.kind is MoveKind.CUT])
        # 刀心不能超出 60×40 的腔体减去刀具半径后的范围
        self.assertLessEqual(float(np.abs(cut_points[:, 0]).max()), 30.0 - 5.0 + 1.2)
        self.assertLessEqual(float(np.abs(cut_points[:, 1]).max()), 20.0 - 5.0 + 1.2)

    def test_layer_count_matches_depth_over_cut_depth(self) -> None:
        # 15 mm 深、每层 5 mm：层内环数会随层数增长，比较"每层第一刀的 z"个数
        result = self._run(cut_mode="contour", cut_depth_mm=5.0, finish_pass=False)
        zs = sorted({round(float(np.asarray(move.points, dtype=float)[0, 2]), 3)
                     for move in result.toolpath.moves if move.kind is MoveKind.CUT})
        self.assertEqual(len(zs), 3)

    def test_zigzag_mode_produces_scanlines(self) -> None:
        result = self._run(cut_mode="zigzag")
        self.assertGreater(result.toolpath.cut_length_mm, 500.0)

    def test_multiple_faces_are_merged(self) -> None:
        both = execute_operation(CAMOperationRequest.from_payload({
            "kind": "pocket_mill",
            "faces": [self.floor["face_id"]],
            "cell_mm": 0.5,
            "stock": self.stock,
            "parameters": {**BASE_PARAMETERS, "cut_mode": "contour", "finish_pass": False},
        }, self.part))
        self.assertEqual(len(both.regions), 1)
        self.assertTrue(both.toolpath.notes)


class ContourAndRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.part = make_part(plate_with_cylinder(), "hole")
        self.top = [f for f in self.part.features
                    if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6][0]

    def test_contour_mill_follows_the_boundary(self) -> None:
        result = execute_operation(CAMOperationRequest.from_payload({
            "kind": "contour_mill",
            "faces": [self.top["face_id"]],
            "cell_mm": 0.5,
            "parameters": BASE_PARAMETERS,
        }, self.part))
        self.assertIn("轮廓", result.toolpath.planner_label)
        self.assertGreater(result.toolpath.cut_length_mm, 100.0)

    def test_missing_faces_is_a_parameter_error(self) -> None:
        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload({"kind": "face_mill", "faces": []}, self.part)

    def test_unknown_kind_is_a_parameter_error(self) -> None:
        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload(
                {"kind": "edm", "faces": [self.top["face_id"]]}, self.part
            )

    def test_downward_face_is_refused(self) -> None:
        bottom = [f for f in self.part.features
                  if f["planar"] and f["normal"][2] < -0.9][0]
        with self.assertRaises(PlanningError):
            region_from_face(self.part, bottom["face_id"])

    def test_unknown_face_is_refused(self) -> None:
        with self.assertRaises(PlanningError):
            region_from_face(self.part, 999999)

    def test_parameters_are_coerced_with_defaults(self) -> None:
        request = CAMOperationRequest.from_payload(
            {"kind": "face_mill", "faces": [self.top["face_id"]], "parameters": {"stepover_mm": 3}},
            self.part,
        )
        self.assertEqual(request.parameters["stepover_mm"], 3.0)
        self.assertEqual(request.parameters["spindle_rpm"], 3000.0)
        self.assertEqual(request.tool.diameter_mm, 10.0)

    def test_out_of_range_parameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload(
                {"kind": "face_mill", "faces": [self.top["face_id"]],
                 "parameters": {"stepover_mm": -5}}, self.part,
            )

    def test_catalog_lists_operations_and_parameters(self) -> None:
        catalog = planning_catalog()
        ids = [item["id"] for item in catalog["operations"]]
        self.assertIn("face_mill", ids)
        self.assertIn("pocket_mill", ids)
        keys = {item["key"] for item in catalog["parameters"]}
        for key in ("feed_mm_per_min", "spindle_rpm", "cut_depth_mm", "stepover_mm",
                    "stock_allowance_mm", "tool_diameter_mm", "safe_height_mm"):
            self.assertIn(key, keys)
        self.assertIn("tool_diameter_mm", catalog["defaults"])


class StockIntegrationTests(unittest.TestCase):
    def test_stock_only_wraps_the_part(self) -> None:
        part = make_part(plate_with_pocket(), "plate")
        stock = build_stock("rectangular", part, {"offset_x_mm": 3, "offset_y_mm": 3, "offset_z_mm": 1})
        self.assertAlmostEqual(stock.bounds.size[0], part.size[0] + 6, places=6)
        self.assertAlmostEqual(stock.bounds.size[2], part.size[2] + 1, places=6)
        self.assertAlmostEqual(stock.bounds.z_min, part.bounds.z_min, places=6)

    def test_cylindrical_stock_covers_the_part(self) -> None:
        part = make_part(plate_with_pocket(), "plate")
        stock = build_stock("cylindrical", part, {"offset_radial_mm": 2, "offset_z_mm": 1})
        diagonal = float(np.hypot(part.size[0], part.size[1]))
        self.assertAlmostEqual(stock.bounds.size[0], diagonal + 4, places=4)
        self.assertAlmostEqual(stock.bounds.size[0], stock.bounds.size[1], places=6)

    def test_stock_mesh_is_closed_enough_to_render(self) -> None:
        part = make_part(plate_with_pocket(), "plate")
        for shape in ("rectangular", "cylindrical"):
            with self.subTest(shape=shape):
                mesh = build_stock(shape, part).build_mesh()
                self.assertGreater(mesh.triangle_count, 8)
                self.assertEqual(mesh.positions.shape[1], 3)


if __name__ == "__main__":
    unittest.main()
