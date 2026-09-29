"""CAM 层：区域运算、平面铣 / 型腔铣刀路、工序与模板。

断言的都是可判定的量：区域面积、环数、刀路覆盖范围、层数、深度。
"刀路生成出来了"不算通过——必须是**正确**的刀路。
"""

from __future__ import annotations

import re
import unittest
from collections import Counter
from math import pi

import numpy as np

from tests.fixtures import (plate_with_boss, plate_with_cylinder, plate_with_pocket,
                            plate_with_two_pockets, simple_box)
from toolpath_lab.cam.boundary import build_region, offset_outline_polygons, region_from_face
from toolpath_lab.cam.common import (LevelCoverage, MillingContext, depth_levels,
                                     level_key, stepped_levels)
from toolpath_lab.cam.face_mill import plan_face_mill
from toolpath_lab.cam.parameters import cam_parameters, tool_from_cam_parameters
from toolpath_lab.cam.pocket_mill import plan_pocket_mill
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

    def test_stepped_levels_absorbs_mesh_noise_without_ghost_layers(self) -> None:
        """层键按 1 nm 归一：网格面拟合噪声与浮点步进误差都不产生同高度幽灵层。

        网格零件的平面拟合实测会把 floor_z 噪声到 40.00000015（对 40.0 差 1.5e-7），
        旧的 9 位小数去重吸收不了，同一物理高度留下两条层——两条刀轨重切同一层。
        """

        levels = stepped_levels(44.0, [40.00000015, 25.00000015], 2.0)
        self.assertEqual(levels[0], 42.0)
        self.assertIn(40.0, levels)
        self.assertEqual(levels[-1], 25.0)
        for previous, current in zip(levels, levels[1:]):
            self.assertGreater(previous - current, 1e-6)
        self.assertEqual(level_key(40.00000015), 40.0)
        self.assertEqual(level_key(42.0), 42.0)


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


class MultiFaceCuttingOrderTests(unittest.TestCase):
    """多加工面的区域调度：层优先合并同高度，深度优先按面切完再换（UG/NX 同名概念）。

    夹具：带型腔的板，顶面 z=40、型腔底 z=25。加工起始高度显式给 44（等效"毛坯顶面
    高于零件"），每层切深 2 →所有面共用的层高网格为 [42, 40, 38, …, 26, 25]（层键按
    1 nm 归一，网格面拟合噪声 40.00000015 不产生幽灵薄层）：顶面区域切 [42, 40]，
    腔底区域登记到 [42 … 25]。

    顶面内环并入可切区域（459abab 的几何障碍判断）之后，顶面在 42 / 40 两个共享层
    会横穿型腔开口，与腔底刀轨切同一块 XY——同一层重叠。现在靠同层去重
    （:class:`toolpath_lab.cam.common.LevelCoverage`）：先选的顶面一次切完共享层，
    腔底让出，第一刀落到 38（顶面加工底之下）。
    """

    def setUp(self) -> None:
        self.part = make_part(plate_with_pocket(), "plate")
        self.top = [f for f in self.part.features
                    if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6][0]
        self.floor = [f for f in self.part.features
                      if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]
        self.faces = [self.top["face_id"], self.floor["face_id"]]

    def _run(self, kind: str, faces, **overrides):
        payload = {
            "kind": kind,
            "faces": list(faces),
            "cell_mm": 0.5,
            "top_z": 44.0,
            "parameters": {**BASE_PARAMETERS, "cut_mode": "contour",
                           "cutting_order": "level_first", **overrides},
        }
        return execute_operation(CAMOperationRequest.from_payload(payload, self.part))

    @staticmethod
    def _cuts(result):
        """非下刀的切削段序列 ``[(面号, 首点 Z, 标签), …]``；面号取自"面 #N："前缀。"""

        items = []
        for move in result.toolpath.moves:
            if move.kind is not MoveKind.CUT or "下刀" in move.label:
                continue
            match = re.match(r"^面 #(\d+)：", move.label)
            items.append((int(match.group(1)) if match else None,
                          round(float(np.asarray(move.points, dtype=float)[0][2]), 3),
                          move.label))
        return items

    @staticmethod
    def _cut_signatures(result):
        """切削段几何签名的多重集（与先后顺序无关）。"""

        counts: Counter = Counter()
        for move in result.toolpath.moves:
            if move.kind is not MoveKind.CUT or "下刀" in move.label:
                continue
            points = np.asarray(move.points, dtype=float)
            counts[(move.label, len(points),
                    round(float(points[0][0]), 4), round(float(points[0][1]), 4),
                    round(float(points[0][2]), 4))] += 1
        return counts

    def test_level_first_yields_shared_levels_to_the_first_face(self) -> None:
        """层优先 + 同层去重：共享层 42/40 只由先选的顶面切，腔底第一刀落在 38。"""

        result = self._run("pocket_mill", self.faces, cutting_order="level_first")
        self.assertEqual(len(result.regions), 2)
        seq = self._cuts(result)
        top_id, floor_id = self.top["face_id"], self.floor["face_id"]
        top_zs = [z for face, z, _ in seq if face == top_id]
        floor_zs = [z for face, z, _ in seq if face == floor_id]
        # 顶面切自己的 [42, 40] 两层（含横穿型腔开口的共享层）
        self.assertEqual([round(z, 3) for z in top_zs], sorted(
            [round(z, 3) for z in top_zs], reverse=True))
        self.assertAlmostEqual(min(top_zs), 40.0, places=3)
        # 腔底在共享层整层让出：第一刀从 38 开始，绝不与顶面同层重叠
        self.assertAlmostEqual(max(floor_zs), 38.0, places=3)
        self.assertEqual([round(z, 3) for z in floor_zs], sorted(
            [round(z, 3) for z in floor_zs], reverse=True))
        # 层优先：顶面全部层在腔底之前做完（顶面让出后共享层只剩它一家）
        last_top = max(i for i, item in enumerate(seq) if item[0] == top_id)
        first_floor = min(i for i, item in enumerate(seq) if item[0] == floor_id)
        self.assertLess(last_top, first_floor)
        # 腔底一路下潜到底
        self.assertAlmostEqual(min(floor_zs), 25.0, places=3)
        self.assertTrue(any("层优先" in note for note in result.toolpath.notes))
        self.assertTrue(any("同层去重" in note for note in result.toolpath.notes))

    def test_depth_first_finishes_one_face_before_the_next(self) -> None:
        result = self._run("pocket_mill", self.faces, cutting_order="depth_first")
        seq = self._cuts(result)
        top_id, floor_id = self.top["face_id"], self.floor["face_id"]
        last_top = max(i for i, item in enumerate(seq) if item[0] == top_id)
        first_floor = min(i for i, item in enumerate(seq) if item[0] == floor_id)
        # 深度优先：顶面（含它的全部层）加工完才开始腔底
        self.assertLess(last_top, first_floor)
        # 每个面内部仍然是从上到下逐层
        top_zs = [z for face, z, _ in seq if face == top_id]
        floor_zs = [z for face, z, _ in seq if face == floor_id]
        self.assertEqual(top_zs, sorted(top_zs, reverse=True))
        self.assertEqual(floor_zs, sorted(floor_zs, reverse=True))
        # 顶面切完 42/40 后，腔底同样让出共享层（第一刀 38，与层优先一致）
        self.assertAlmostEqual(max(floor_zs), 38.0, places=3)
        self.assertTrue(any("深度优先" in note for note in result.toolpath.notes))

    def test_cutting_order_does_not_change_the_cut_geometry(self) -> None:
        """切削顺序只改排列：两种顺序切的层与每层刀路必须完全一致。"""

        level = self._run("pocket_mill", self.faces, cutting_order="level_first")
        depth = self._run("pocket_mill", self.faces, cutting_order="depth_first")
        self.assertEqual(self._cut_signatures(level), self._cut_signatures(depth))
        # 层高与每层刀路一致（集合语义）：两种顺序切出的 (面, 层) 组合完全相同
        self.assertEqual(
            sorted((face, round(z, 3)) for face, z, _ in self._cuts(level)),
            sorted((face, round(z, 3)) for face, z, _ in self._cuts(depth)),
        )
        # 共享层只由顶面一家切（两种顺序都是），腔底从 38 起——重叠已消除
        for result in (level, depth):
            floor_zs = [z for face, z, _ in self._cuts(result)
                        if face == self.floor["face_id"]]
            self.assertAlmostEqual(max(floor_zs), 38.0, places=3)

    def test_single_face_is_identical_for_both_orders(self) -> None:
        """只选一个面时层优先 ≡ 深度优先：参数不能改变单面刀路。"""

        level = self._run("pocket_mill", [self.floor["face_id"]])
        depth = self._run("pocket_mill", [self.floor["face_id"]],
                          cutting_order="depth_first")
        left = [(move.label, move.points.tobytes()) for move in level.toolpath.moves]
        right = [(move.label, move.points.tobytes()) for move in depth.toolpath.moves]
        self.assertEqual(left, right)

    def test_unknown_cutting_order_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            self._run("pocket_mill", self.faces, cutting_order="sideways")

    def test_shared_level_yield_does_not_leave_uncut_material(self) -> None:
        """守恒：腔底在共享层让出的面积 ⊆ 顶面同层已切面积（跳过不等于漏切）。

        直接驱动同层去重登记簿：顶面在 42 层登记后，腔底查询到的排除掩码必须把
        它自己这一层会切的单元全部盖住——否则"整层跳过"就会留下未切材料。
        """

        top_region = region_from_face(self.part, self.top["face_id"],
                                      cell_mm=0.5, ceiling_z=44.0)
        floor_region = region_from_face(self.part, self.floor["face_id"],
                                        cell_mm=0.5, ceiling_z=44.0)
        tool = tool_from_cam_parameters(cam_parameters().coerce(dict(BASE_PARAMETERS)))
        offset = tool.radius_mm + float(BASE_PARAMETERS["stock_allowance_mm"])
        coverage = LevelCoverage()
        top_mask = LevelCoverage.cut_mask(top_region, 42.0, tool, offset)
        self.assertIsNotNone(top_mask)
        coverage.record(top_region, 42.0, top_mask)
        floor_mask = LevelCoverage.cut_mask(floor_region, 42.0, tool, offset)
        exclude = coverage.exclude(floor_region, 42.0)
        self.assertIsNotNone(floor_mask)
        self.assertIsNotNone(exclude)
        # 腔底会切的每个单元都已被顶面覆盖 → 整层跳过不漏切
        self.assertFalse(bool((floor_mask & ~exclude).any()))
        # 40 层同样成立（顶面的加工底所在层）
        coverage.record(top_region, 40.0, LevelCoverage.cut_mask(top_region, 40.0,
                                                                 tool, offset))
        exclude_40 = coverage.exclude(floor_region, 40.0)
        self.assertIsNotNone(exclude_40)
        self.assertFalse(bool((floor_mask & ~exclude_40).any()))
        # 38 层顶面不参与：无登记 → 返回 None，腔底走老路径原样加工
        self.assertIsNone(coverage.exclude(floor_region, 38.0))

    def test_face_mill_honours_cutting_order(self) -> None:
        faces = [self.floor["face_id"], self.top["face_id"]]
        level = self._run("face_mill", faces, cutting_order="level_first")
        depth = self._run("face_mill", faces, cutting_order="depth_first")

        def runs(result):
            order = []
            for face, _, _ in self._cuts(result):
                if not order or order[-1] != face:
                    order.append(face)
            return order

        # 深度优先：按面分组，一个面铣完再换下一个
        self.assertEqual(runs(depth), faces)
        # 层优先：同高度的两面交错出现，首段仍按选面顺序
        level_runs = runs(level)
        self.assertGreater(len(level_runs), len(faces))
        self.assertEqual(level_runs[0], faces[0])
        self.assertEqual(self._cut_signatures(level), self._cut_signatures(depth))

    def test_contour_mill_honours_cutting_order(self) -> None:
        level = self._run("contour_mill", self.faces, cutting_order="level_first")
        depth = self._run("contour_mill", self.faces, cutting_order="depth_first")
        top_id, floor_id = self.top["face_id"], self.floor["face_id"]
        seq_level = [item[0] for item in self._cuts(level)]
        seq_depth = [item[0] for item in self._cuts(depth)]
        # 层优先：腔底的 42 层轮廓出现在顶面 40 层之前（同高度合并、交替下降）
        first_floor = seq_level.index(floor_id)
        self.assertLess(first_floor, len(seq_level) - 1 - seq_level[::-1].index(top_id))
        # 深度优先：顶面的全部轮廓在腔底之前
        self.assertLess(max(i for i, face in enumerate(seq_depth) if face == top_id),
                        min(i for i, face in enumerate(seq_depth) if face == floor_id))
        self.assertEqual(self._cut_signatures(level), self._cut_signatures(depth))

    def test_catalog_declares_cutting_order(self) -> None:
        catalog = planning_catalog()
        entry = next((item for item in catalog["parameters"]
                      if item["key"] == "cutting_order"), None)
        self.assertIsNotNone(entry, "切削顺序参数必须出现在能力目录里")
        self.assertEqual(entry["default"], "level_first")
        self.assertEqual({choice["value"] for choice in entry["choices"]},
                         {"level_first", "depth_first"})
        self.assertEqual(catalog["defaults"]["cutting_order"], "level_first")


class MultiPocketCuttingOrderTests(unittest.TestCase):
    """多型腔加工：层优先=所有型腔共用当前高度、该层全部完成再下降；深度优先=单腔挖完再换。

    夹具：120×80×40 的板带两个 40×50、深 15 的并排型腔，腔底同在 z=25。
    加工起始高度 45（毛坯高于型腔上表面），每层切深 2 → 两个型腔共用层高网格
    [43, 41, 39, …, 27, 25]。调度序列折叠成 ``[(面, 层), …]`` 后可与两种策略的
    期望**逐项精确**比对。
    """

    def setUp(self) -> None:
        self.part = make_part(plate_with_two_pockets(), "plate2")
        floors = sorted([f for f in self.part.features
                         if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6],
                        key=lambda item: item["face_id"])
        self.assertEqual(len(floors), 2, "双型腔夹具应有两张腔底面")
        self.pockets = [floors[0]["face_id"], floors[1]["face_id"]]
        self.levels = [43.0, 41.0, 39.0, 37.0, 35.0, 33.0, 31.0, 29.0, 27.0, 25.0]

    def _run(self, **overrides):
        payload = {
            "kind": "pocket_mill",
            "faces": list(self.pockets),
            "cell_mm": 0.5,
            "top_z": 45.0,
            "parameters": {**BASE_PARAMETERS, "cut_mode": "contour",
                           "cutting_order": "level_first", **overrides},
        }
        return execute_operation(CAMOperationRequest.from_payload(payload, self.part))

    @staticmethod
    def _schedule(result) -> list[tuple[int, float]]:
        """把连续同 (面, 层) 的切削段折叠成调度序列 ``[(面号, 层高 Z), …]``。"""

        pairs: list[tuple[int, float]] = []
        for move in result.toolpath.moves:
            if move.kind is not MoveKind.CUT or "下刀" in move.label:
                continue
            match = re.match(r"^面 #(\d+)：", move.label)
            if match is None:
                continue
            item = (int(match.group(1)),
                    round(float(np.asarray(move.points, dtype=float)[0][2]), 3))
            if not pairs or pairs[-1] != item:
                pairs.append(item)
        return pairs

    @staticmethod
    def _signatures(result) -> Counter:
        counts: Counter = Counter()
        for move in result.toolpath.moves:
            if move.kind is not MoveKind.CUT or "下刀" in move.label:
                continue
            points = np.asarray(move.points, dtype=float)
            counts[(move.label, len(points),
                    round(float(points[0][0]), 4), round(float(points[0][1]), 4),
                    round(float(points[0][2]), 4))] += 1
        return counts

    def test_level_first_shares_every_level_across_pockets(self) -> None:
        result = self._run(cutting_order="level_first")
        expected = [(face, z) for z in self.levels for face in self.pockets]
        self.assertEqual(self._schedule(result), expected,
                         "层优先：每个高度上两个型腔依次切完才允许下降")
        self.assertTrue(any("层优先" in note for note in result.toolpath.notes))

    def test_depth_first_digs_one_pocket_out_before_the_next(self) -> None:
        result = self._run(cutting_order="depth_first")
        expected = [(face, z) for face in self.pockets for z in self.levels]
        self.assertEqual(self._schedule(result), expected,
                         "深度优先：单个型腔由上至下完整切完再切换到下一个型腔")
        self.assertTrue(any("深度优先" in note for note in result.toolpath.notes))

    def test_both_orders_cut_the_same_geometry(self) -> None:
        level = self._run(cutting_order="level_first")
        depth = self._run(cutting_order="depth_first")
        self.assertEqual(self._signatures(level), self._signatures(depth))
        self.assertNotEqual(self._schedule(level), self._schedule(depth))

    def test_face_selection_order_does_not_break_level_scheduling(self) -> None:
        """选面顺序反过来：层优先仍按高度对齐，只是同层内先后随选面顺序。"""

        payload = {
            "kind": "pocket_mill",
            "faces": [self.pockets[1], self.pockets[0]],
            "cell_mm": 0.5,
            "top_z": 45.0,
            "parameters": {**BASE_PARAMETERS, "cut_mode": "contour",
                           "cutting_order": "level_first"},
        }
        result = execute_operation(CAMOperationRequest.from_payload(payload, self.part))
        expected = [(face, z) for z in self.levels
                    for face in (self.pockets[1], self.pockets[0])]
        self.assertEqual(self._schedule(result), expected)


class LevelTransferTests(unittest.TestCase):
    """层内一次下刀切完整层：能平移就连，出界/跨区域才回退抬刀。

    断言的都是"抬刀发生了没有"这类可判定的量：入口下刀之后，整层乃至整条
    刀路不允许再出现抬到安全面的段（RAPID 带 Z 升降即视为抬刀）。
    """

    def _entry_index(self, tp) -> int:
        for index, move in enumerate(tp.moves):
            if move.kind is MoveKind.CUT and "下刀" in move.label:
                return index
        raise AssertionError("刀路里没有下刀段")

    def _assert_single_entry_stays_in_level(self, tp, first_level_z: float) -> None:
        entry = self._entry_index(tp)
        plunges = [m for m in tp.moves
                   if m.kind is MoveKind.CUT and "下刀" in m.label]
        self.assertEqual(len(plunges), 1, "入口之后又出现了下刀")
        for move in tp.moves[entry + 1:]:
            z_max = float(np.asarray(move.points, float)[:, 2].max())
            self.assertLessEqual(z_max, first_level_z + 1e-6,
                                 f"{move.kind.name}/{move.label} 抬到了层高以上")

    def test_pocket_completes_every_level_after_one_entry(self) -> None:
        """单型腔：整条刀路只在入口下刀一次，层间用斜降连接。"""

        part = make_part(plate_with_pocket())
        floor_id = [f["face_id"] for f in part.features
                    if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]
        region = region_from_face(part, floor_id, cell_mm=0.5, ceiling_z=45.0,
                                  require_horizontal=False)
        tp = plan_pocket_mill(context_for(region, cut_mode="contour"))
        levels = depth_levels(region.top_z, region.floor_z, 2.0)
        self.assertGreater(len(levels), 3)
        self._assert_single_entry_stays_in_level(tp, levels[0])
        # 层间必须有斜降（Z 跨层的连接段），而不是抬刀
        descents = [m for m in tp.moves
                    if m.kind is MoveKind.LINK
                    and np.ptp(np.asarray(m.points, float)[:, 2]) > 1e-9]
        self.assertGreaterEqual(len(descents), len(levels) - 1)
        # 每层的环与壁精修依然齐全（几何没有因连贯化而丢失）
        rings = [m for m in tp.moves
                 if m.kind is MoveKind.CUT and "条刀轨" in m.label]
        finishes = [m for m in tp.moves
                    if m.kind is MoveKind.CUT and "精修腔壁" in m.label]
        self.assertGreaterEqual(len(rings), len(levels))
        self.assertEqual(len(finishes), len(levels))

    def test_face_zigzag_finishes_level_with_one_entry(self) -> None:
        """面铣往复：相邻刀线在层内平移，换行与层间都不再抬刀。"""

        part = make_part(simple_box(100.0, 80.0, 20.0))
        top = [f for f in part.features
               if f["horizontal"] and abs(f["plane"][3] - 20.0) < 1e-6][0]
        region = region_from_face(part, top["face_id"], cell_mm=0.5,
                                  ceiling_z=22.0, require_horizontal=True)
        tp = plan_face_mill(context_for(region, cut_depth_mm=1.5,
                                        cut_mode="zigzag", finish_pass=False))
        levels = depth_levels(region.top_z, region.floor_z, 1.5)
        self.assertGreater(len(levels), 1)
        self._assert_single_entry_stays_in_level(tp, levels[0])
        rows = [m for m in tp.moves
                if m.kind is MoveKind.CUT and "第" in m.label and "层" in m.label]
        self.assertGreaterEqual(len(rows), 10)

    def test_one_way_face_mill_keeps_per_pass_retract(self) -> None:
        """单向走刀的语义不变：每一刀仍抬刀、同向落刀。"""

        part = make_part(simple_box(100.0, 80.0, 20.0))
        top = [f for f in part.features
               if f["horizontal"] and abs(f["plane"][3] - 20.0) < 1e-6][0]
        region = region_from_face(part, top["face_id"], cell_mm=0.5,
                                  ceiling_z=22.0, require_horizontal=True)
        tp = plan_face_mill(context_for(region, cut_depth_mm=1.5,
                                        cut_mode="one_way", finish_pass=False))
        lifts = [m for m in tp.moves
                 if m.kind is MoveKind.RAPID and m.label == "层内转移"]
        self.assertGreaterEqual(len(lifts), 3)

    def test_contour_mill_chains_levels_without_retract(self) -> None:
        """轮廓铣：各层同 XY 直降衔接，全程只在入口下刀一次。"""

        part = make_part(simple_box(100.0, 80.0, 20.0))
        top = [f for f in part.features
               if f["horizontal"] and abs(f["plane"][3] - 20.0) < 1e-6][0]
        result = execute_operation(CAMOperationRequest.from_payload({
            "kind": "contour_mill",
            "faces": [top["face_id"]],
            "cell_mm": 0.5,
            "top_z": 25.0,
            "parameters": BASE_PARAMETERS,
        }, part))
        tp = result.toolpath
        levels = depth_levels(25.0, 20.0, float(BASE_PARAMETERS["cut_depth_mm"]))
        self._assert_single_entry_stays_in_level(tp, levels[0])
        cuts = [m for m in tp.moves
                if m.kind is MoveKind.CUT and "轮廓" in m.label]
        self.assertEqual(len(cuts), len(levels))

    def test_island_pocket_links_never_enter_the_island(self) -> None:
        """带岛型腔：层内平移的直线绝不穿岛——穿岛的转移必须回退成抬刀。"""

        region = build_region(SQUARE, [ISLAND], top_z=10.0, floor_z=0.0,
                              cell_mm=0.4)
        tp = plan_pocket_mill(context_for(region, cut_depth_mm=2.0,
                                          cut_mode="zigzag"))
        links = [m for m in tp.moves if m.kind is MoveKind.LINK]
        self.assertTrue(links)
        for move in links:
            points = np.asarray(move.points, float)
            samples = np.vstack([points, (points[:-1] + points[1:]) / 2.0])
            crossed = ((np.abs(samples[:, 0]) < 5.0)
                       & (np.abs(samples[:, 1]) < 5.0))
            self.assertFalse(bool(crossed.any()),
                             f"层内转移穿过岛屿：{move.label}")

    def test_multi_face_still_retracts_between_regions(self) -> None:
        """多面同层：区域内部平移衔接，跨区域的转移必须抬刀。"""

        part = make_part(plate_with_pocket())
        top = [f for f in part.features
               if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6][0]
        floor = [f for f in part.features
                 if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]
        result = execute_operation(CAMOperationRequest.from_payload({
            "kind": "pocket_mill",
            "faces": [top["face_id"], floor["face_id"]],
            "cell_mm": 0.5,
            "top_z": 45.0,
            "parameters": {**BASE_PARAMETERS, "cut_mode": "contour",
                           "cutting_order": "level_first"},
        }, part))
        lifts = 0
        for move in result.toolpath.moves:
            zs = np.asarray(move.points, float)[:, 2]
            if move.kind is MoveKind.RAPID and float(zs.max() - zs.min()) > 1e-9:
                lifts += 1
        links = [m for m in result.toolpath.moves if m.kind is MoveKind.LINK]
        # 两个型腔之间的实体高出当前层，跨区域转移必须保留抬刀
        self.assertGreaterEqual(lifts, 2)
        # 而区域内部的环间/精修转移应当是层内平移
        self.assertGreaterEqual(len(links), 3)


class ObstacleConnectivityTests(unittest.TestCase):
    """逐层几何障碍判断：区域连通按"本层高度处的几何遮挡"决定。

    - 层内没有凸台遮挡 → 该层整体按连续平面规划（刀线跨过）；
    - 孔上方无障碍 → 孔与周边连通、合并进同一层加工；
    - 孔/面上有凸台 → 低于其顶面的层逐列避让，层高越过顶面后恢复连通。
    """

    # plate_with_boss 的凸台：34×14 mm、居中，半宽 17 / 半深 7
    PAD_HALF = (17.0, 7.0)

    # -- 工具 --------------------------------------------------------------
    def _boss_region(self, ceiling: float = 45.0):
        part = make_part(plate_with_boss(), "boss")
        moat = [f["face_id"] for f in part.features
                if f["horizontal"] and abs(f["plane"][3] - 30.0) < 1e-6][0]
        return region_from_face(part, moat, cell_mm=0.5, ceiling_z=ceiling)

    @staticmethod
    def _cut_polylines(tp) -> list:
        return [np.asarray(m.points, dtype=float)
                for m in tp.moves if m.kind is MoveKind.CUT and len(m.points) >= 2]

    @staticmethod
    def _min_dist_point(polylines, px: float, py: float) -> float:
        """折线（含线段内部）到点的最小距离。"""

        best = float("inf")
        for poly in polylines:
            a = poly[:, :2]
            v = a[1:] - a[:-1]
            w = a[:-1] - np.array([px, py])
            denom = np.einsum("ij,ij->i", v, v)
            safe = np.where(denom > 1e-18, denom, 1.0)
            t = np.clip(-np.einsum("ij,ij->i", w, v) / safe, 0.0, 1.0)
            t = np.where(denom > 1e-18, t, 0.0)
            proj = a[:-1] + t[:, None] * v
            best = min(best, float(np.hypot(proj[:, 0] - px, proj[:, 1] - py).min()))
        return best

    @staticmethod
    def _min_dist_rect(polylines, half_x: float, half_y: float) -> float:
        """折线（含线段内部）到原点矩形的最小距离（0 = 穿过）。"""

        best = float("inf")
        for poly in polylines:
            for start, end in zip(poly[:-1], poly[1:]):
                for point in start + np.linspace(0.0, 1.0, 64)[:, None] * (end - start):
                    dx = max(abs(float(point[0])) - half_x, 0.0)
                    dy = max(abs(float(point[1])) - half_y, 0.0)
                    best = min(best, float(np.hypot(dx, dy)))
        return best

    @staticmethod
    def _min_dist_points_rect(points, half_x: float, half_y: float) -> float:
        """点集到原点矩形的最小距离（0 = 点在矩形内）。"""

        dx = np.maximum(np.abs(points[:, 0]) - half_x, 0.0)
        dy = np.maximum(np.abs(points[:, 1]) - half_y, 0.0)
        return float(np.hypot(dx, dy).min())

    def _cell(self, region, x: float, y: float) -> tuple[int, int]:
        return (int((x - region.bounds[0]) / region.cell_mm),
                int((y - region.bounds[1]) / region.cell_mm))

    # -- 用例 --------------------------------------------------------------
    def test_through_hole_merges_with_the_surrounding_plane(self) -> None:
        """孔上方无障碍：孔并入同一层，面铣刀路从孔心跨过（要求 2）。"""

        part = make_part(plate_with_cylinder(), "hole")
        top = [f["face_id"] for f in part.features
               if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6][0]
        region = region_from_face(part, top, cell_mm=0.5, ceiling_z=42.0)
        # 内环仍被记录（形状没丢），但已经并进可切区域
        self.assertEqual(len(region.islands), 1)
        self.assertGreater(region.area_mm2, 7800.0)     # 全幅 8000；老行为会扣掉 ~707
        self.assertTrue(any("并入" in note for note in region.notes))
        i, j = self._cell(region, 0.0, 0.0)
        self.assertTrue(bool(region.inside[i, j]), "孔心不在可切区域内")
        # 孔列没有几何 → 障碍场全为 -inf → 本层判据恒为"不裁"
        context = context_for(region)
        self.assertFalse(region.has_geometry_obstacle)
        self.assertIsNone(region.obstacle_mask(40.0, context.tool))
        # 刀路真的跨过孔心（未并入时刀线在孔边 15 mm 外就被切断）
        tp = plan_face_mill(context_for(region, cut_mode="zigzag"))
        crossing = self._min_dist_point(self._cut_polylines(tp), 0.0, 0.0)
        self.assertLess(crossing, 12.0, "面铣刀路没有跨过通孔")

    def test_boss_blocks_only_the_levels_below_its_top(self) -> None:
        """凸台顶以上的层整层连通；低于凸台顶的层留刀具半径避让（要求 1/3）。"""

        region = self._boss_region(ceiling=45.0)
        context = context_for(region)
        self.assertTrue(region.has_geometry_obstacle)
        i, j = self._cell(region, 0.0, 0.0)          # 凸台正中
        ring_i, ring_j = self._cell(region, 0.0, 20.0)   # 槽底环带（凸台之外）
        allowed_high = region.obstacle_mask(41.0, context.tool)
        allowed_low = region.obstacle_mask(35.0, context.tool)
        self.assertTrue(bool(allowed_high[i, j]), "层高越过凸台顶后应连通")
        self.assertFalse(bool(allowed_low[i, j]), "层高低于凸台顶时应被遮挡")
        self.assertTrue(bool(allowed_low[ring_i, ring_j]), "遮挡不应波及凸台外的槽底")

        tp = plan_face_mill(context_for(region))
        points = np.vstack([np.asarray(m.points, dtype=float) for m in tp.moves
                            if m.kind is MoveKind.CUT])
        above = points[points[:, 2] >= 40.0 - 1e-6]
        below = points[points[:, 2] < 40.0 - 1e-6]
        self.assertGreater(above.shape[0], 0)
        self.assertGreater(below.shape[0], 0)
        # 凸台顶以上的层：刀线从凸台正上方跨过（整层按连续平面规划）
        above_lines = [p for p in self._cut_polylines(tp) if p[:, 2].max() >= 40.0 - 1e-6]
        self.assertEqual(self._min_dist_rect(above_lines, 16.0, 6.0), 0.0,
                         "凸台顶以上的层没有跨过凸台")
        # 低于凸台顶的层：刀心离凸台边至少 (刀具半径 - 栅格量化)，不允许切进凸台
        clearance = self._min_dist_points_rect(below, *self.PAD_HALF)
        self.assertGreaterEqual(clearance, context.tool_radius - 1.0,
                                f"低于凸台顶的层离凸台只有 {clearance:.3f} mm")

    def test_contour_mill_carves_a_hole_around_the_boss_each_level(self) -> None:
        """轮廓铣逐层重算等距环：低于凸台顶的层绕开凸台（环上出现内边界）。"""

        part = make_part(plate_with_boss(), "boss")
        moat = [f["face_id"] for f in part.features
                if f["horizontal"] and abs(f["plane"][3] - 30.0) < 1e-6][0]
        result = execute_operation(CAMOperationRequest.from_payload({
            "kind": "contour_mill",
            "faces": [moat],
            "cell_mm": 0.5,
            "parameters": BASE_PARAMETERS,
        }, part))
        points = np.vstack([np.asarray(m.points, dtype=float) for m in result.toolpath.moves
                            if m.kind is MoveKind.CUT])
        self.assertGreater(points.shape[0], 0)
        # 部件顶 = 凸台顶 40：所有层都低于凸台顶，每层的环都必须绕开凸台
        self.assertTrue(bool((points[:, 2] < 40.0 + 1e-6).all()))
        clearance = self._min_dist_points_rect(points, *self.PAD_HALF)
        # 判据是一个区间：≥4（栅格量化内仍留出刀具半径）证明没贴着凸台走，
        # ≤8 证明环**确实**绕着凸台缩出了内边界——不裁的老路径是 ~12.7 mm。
        self.assertGreaterEqual(clearance, 4.0, f"轮廓贴着凸台走（距离 {clearance:.3f}）")
        self.assertLessEqual(clearance, 8.0,
                             f"轮廓没有绕凸台缩环（距离 {clearance:.3f}）")

    def test_pocket_floor_keeps_the_no_obstacle_path(self) -> None:
        """兜底：面内没有高出底面的几何时，逐层判据不裁（行为同老路径）。"""

        part = make_part(plate_with_pocket())
        floor_id = [f["face_id"] for f in part.features
                    if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]
        region = region_from_face(part, floor_id, cell_mm=0.5, ceiling_z=45.0,
                                  require_horizontal=False)
        self.assertIsNotNone(region.obstacle_top)      # 障碍场照常建立（全 -inf）
        self.assertFalse(region.has_geometry_obstacle)
        context = context_for(region)
        self.assertIsNone(region.obstacle_mask(region.floor_z, context.tool))
        # 刀路不受影响：仍然一次下刀切完整条
        tp = plan_pocket_mill(context_for(region, cut_mode="contour"))
        plunges = [m for m in tp.moves
                   if m.kind is MoveKind.CUT and "下刀" in m.label]
        self.assertEqual(len(plunges), 1)


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
        # 步距已改为按刀具直径的比例（``stepover_ratio``）；旧 ``stepover_mm`` 只是
        # 兼容垫片（见 test_cam_regressions.StepoverRatioTests），不在参数声明里。
        request = CAMOperationRequest.from_payload(
            {"kind": "face_mill", "faces": [self.top["face_id"]],
             "parameters": {"stepover_ratio": 0.3}},
            self.part,
        )
        self.assertEqual(request.parameters["stepover_ratio"], 0.3)
        self.assertEqual(request.parameters["spindle_rpm"], 3000.0)
        self.assertEqual(request.tool.diameter_mm, 10.0)

    def test_out_of_range_parameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload(
                {"kind": "face_mill", "faces": [self.top["face_id"]],
                 "parameters": {"stepover_ratio": -5}}, self.part,
            )

    def test_catalog_lists_operations_and_parameters(self) -> None:
        catalog = planning_catalog()
        ids = [item["id"] for item in catalog["operations"]]
        self.assertIn("face_mill", ids)
        self.assertIn("pocket_mill", ids)
        keys = {item["key"] for item in catalog["parameters"]}
        for key in ("feed_mm_per_min", "spindle_rpm", "cut_depth_mm", "stepover_ratio",
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
