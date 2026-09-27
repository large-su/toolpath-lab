"""P0/P1 bug 回归测试。

每个测试对应一份 bug fix 后的回归断言，确保修复不被日后重构悄悄退化。
为保持 **无 OCP 依赖**（cadquery-ocp 不一定可用），统一手工构造 MachiningRegion、
PartModel、FaceRecord、TessellatedModel 等中立数据结构。
"""

from __future__ import annotations

import unittest
from math import cos as do_cos, sin as do_sin

import numpy as np

from toolpath_lab.cam.boundary import (
    DEFAULT_CELL_MM,
    _distance_to_contour,
    build_region,
    offset_outline_polygons,
    region_from_face,
)
from toolpath_lab.cam.common import MillingContext
from toolpath_lab.cam.face_mill import plan_face_mill
from toolpath_lab.cam.pocket_mill import plan_pocket_mill
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.part import PartBounds, PartModel
from toolpath_lab.core.tessellation import FaceRecord, TessellatedModel
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.surfacing.waterline import (
    WaterlineRequest,
    _layer_heights,
    _moves_from_levels,
)


# ----------------------------------------------------------------- 共享夹具
def _tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=30.0)


def _params() -> dict[str, object]:
    """一套完整 CAM 参数，确保 planner 不至于因缺键抛 ParameterError。"""
    return {
        "spindle_rpm": 3000.0,
        "feed_mm_per_min": 900.0,
        "plunge_feed_mm_per_min": 300.0,
        "rapid_feed_mm_per_min": 5000.0,
        "stepover_ratio": 0.4,
        "cut_depth_mm": 2.0,
        "stock_allowance_mm": 0.0,
        "finish_allowance_mm": 0.0,
        "cut_mode": "zigzag",
        "direction_deg": 0.0,
        "finish_pass": True,
        "safe_height_mm": 5.0,
        "clearance_mm": 1.0,
        "spindle_direction": "cw",
        "coolant": "flood",
    }


def _square_region(side: float = 60.0, *, cell_mm: float = DEFAULT_CELL_MM,
                   top_z: float = 1.0, floor_z: float = 0.0):
    outline = np.array(
        [[-side / 2, -side / 2], [side / 2, -side / 2],
         [side / 2, side / 2], [-side / 2, side / 2]],
        dtype=np.float64,
    )
    return build_region(outline, [], top_z=top_z, floor_z=floor_z, cell_mm=cell_mm)


def _make_part_with_face(*, normal: tuple[float, float, float],
                         plane: tuple[float, float, float, float],
                         loop_xy: np.ndarray) -> PartModel:
    """手工构造一个仅有一个面、给定法向与平面的 PartModel（仅供 region_from_face 测试）。"""

    # 平面 n·p = d 上把 loop_xy 抬到 plane 对应的 Z：选 loop 的平均 X/Y 代入 n·p=d
    nx, ny, nz, d = plane
    sample = loop_xy.mean(axis=0)
    z = (d - nx * sample[0] - ny * sample[1]) / nz if nz else 0.0
    positions = np.column_stack([loop_xy, np.full(loop_xy.shape[0], float(z))])
    indices = np.array([[0, 1, 2]], dtype=np.int64)
    normal_arr = np.asarray(normal, dtype=np.float64)
    face_loop_3d = np.column_stack(
        [loop_xy, np.full(loop_xy.shape[0], float(z))]
    )
    face = FaceRecord(
        id=1,
        surface_kind="plane",
        triangle_start=0,
        triangle_count=1,
        area_mm2=float(abs(np.linalg.norm(loop_xy[1] - loop_xy[0])) *
                         np.linalg.norm(loop_xy[2] - loop_xy[1])),
        normal=tuple(float(v) for v in normal),
        boundary_points=loop_xy.shape[0],
        loop_count=1,
        is_planar=True,
        plane=plane,
        bounds=(float(loop_xy[:, 0].min()), float(loop_xy[:, 1].min()), float(z),
                float(loop_xy[:, 0].max()), float(loop_xy[:, 1].max()), float(z)),
        loops=(face_loop_3d,),
    )
    mesh = TessellatedModel(
        positions=positions,
        indices=indices,
        normals=np.tile(normal_arr, (1, 1)),
        face_of_triangle=np.array([1], dtype=np.int64),
        faces=[face],
        vertices=positions,
        source_name="regression_fixture",
    )
    return PartModel(
        model_id="reg",
        name="regression_fixture",
        mesh=mesh,
        bounds=PartBounds(
            float(loop_xy[:, 0].min()), float(loop_xy[:, 1].min()), float(z),
            float(loop_xy[:, 0].max()), float(loop_xy[:, 1].max()), float(z),
        ),
        features=[],
    )


# ================================================================== BUG-001
class WaterlineSafeLinkTests(unittest.TestCase):
    """BUG-001：等高铣层间抬刀段不应斜向穿刀。"""

    def _two_layers(self) -> list[MoveKind] | list:
        ring_low = np.array(
            [[0.0, 0.0, 1.0], [4.0, 0.0, 1.0], [4.0, 4.0, 1.0], [0.0, 4.0, 1.0]],
            dtype=np.float64,
        )
        ring_high = np.array(
            [[0.5, 0.5, 5.0], [3.5, 0.5, 5.0], [3.5, 3.5, 5.0], [0.5, 3.5, 5.0]],
            dtype=np.float64,
        )
        request = WaterlineRequest(include_safe_links=True, safe_height_mm=2.0)
        return _moves_from_levels(
            [(1.0, (ring_low,)), (5.0, (ring_high,))], request, safe_z=10.0
        )

    def test_first_rapid_uses_step_shape_via_safe_z(self) -> None:
        moves = self._two_layers()
        first_rapid = next(m for m in moves if m.kind is MoveKind.RAPID)
        zs = [float(pt[2]) for pt in first_rapid.points]
        self.assertEqual(zs[0], 1.0)            # 从切削深度出发
        self.assertEqual(zs[1], 10.0)           # 抬到安全面
        self.assertEqual(zs[-2], 10.0)          # 横移到起点上方
        self.assertEqual(zs[-1], 1.0)           # 下刀到新环起点

    def test_layer_link_does_not_start_with_diagonal_through_part(self) -> None:
        moves = self._two_layers()
        rapids = [m for m in moves if m.kind is MoveKind.RAPID]
        # 第二段 RAPID：上一环终点 (0,4,1) → 新环起点 (0.5,0.5,5)
        second = rapids[1]
        for k in range(len(second.points) - 1):
            z1, z2 = float(second.points[k][2]), float(second.points[k + 1][2])
            # 不允许相邻两点直接跨越 1↔5
            self.assertFalse(
                {round(z1, 6), round(z2, 6)} == {1.0, 5.0},
                "BUG-001 复发：层间抬刀段直冲穿过切削层",
            )


# ================================================================== BUG-002
class BoundaryOffsetAlignmentTests(unittest.TestCase):
    """BUG-002：等距轮廓整体偏移半格。"""

    def test_outer_outline_uses_cell_corners(self) -> None:
        outline = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
                           dtype=np.float64)
        region = build_region(outline, [], top_z=1.0, floor_z=0.0, cell_mm=1.0)
        polys = offset_outline_polygons(region, 0.0, min_area_mm2=0.5)
        self.assertEqual(len(polys), 1)
        polygon = polys[0]
        # 世界坐标应是 outline 的角点 (0..4)，不能有 0.5..4.5 的半格偏移
        self.assertAlmostEqual(float(polygon[:, 0].min()), 0.0, places=6)
        self.assertAlmostEqual(float(polygon[:, 0].max()), 4.0, places=6)
        self.assertAlmostEqual(float(polygon[:, 1].min()), 0.0, places=6)
        self.assertAlmostEqual(float(polygon[:, 1].max()), 4.0, places=6)


# ================================================================== BUG-003
class RegionFromFacePlanarityTests(unittest.TestCase):
    """BUG-003：斜面不应被 region_from_face 接受。"""

    def test_inclined_face_is_rejected(self) -> None:
        loop_xy = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
                           dtype=np.float64)
        # 任意斜面：法向 (0, sin10°, cos10°) —— nz ≈ 0.985 < 1
        normal = (0.0, do_sin(np.deg2rad(10.0)), do_cos(np.deg2rad(10.0)))
        plane = (normal[0], normal[1], normal[2], 0.0)
        part = _make_part_with_face(normal=normal, plane=plane, loop_xy=loop_xy)
        with self.assertRaises(PlanningError) as cm:
            region_from_face(part, 1)
        self.assertIn("近水平面", str(cm.exception))

    def test_horizontal_face_is_accepted(self) -> None:
        loop_xy = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
                           dtype=np.float64)
        normal = (0.0, 0.0, 1.0)
        plane = (0.0, 0.0, 1.0, 0.0)
        part = _make_part_with_face(normal=normal, plane=plane, loop_xy=loop_xy)
        region = region_from_face(part, 1, ceiling_z=1.0)
        self.assertAlmostEqual(region.floor_z, 0.0)
        self.assertAlmostEqual(region.top_z, 1.0)


# ================================================================== BUG-004
class FaceMillFinishPassTests(unittest.TestCase):
    """BUG-004：平面铣精修轮廓条件恒假。"""

    def test_finish_contour_is_emitted_when_region_nonempty(self) -> None:
        region = _square_region(side=60.0, cell_mm=1.0, top_z=1.0, floor_z=0.0)
        params = _params()
        params["cut_depth_mm"] = 1.0
        ctx = MillingContext(
            tool=_tool(diameter=6.0),
            top_z=1.0,
            floor_z=0.0,
            parameters=params,
            region=region,
        )
        toolpath = plan_face_mill(ctx, notes_prefix="")
        labels = [move.label for move in toolpath.moves if move.label]
        self.assertTrue(
            any("精修轮廓" in lbl for lbl in labels),
            f"精修轮廓未触发：labels = {labels}",
        )

    def test_finish_contour_skipped_when_too_large_tool(self) -> None:
        # 区域 10×10 mm，刀具 D12 → R=6 > 区域半宽 5，偏置后整片区域被裁光。
        # 此时既没有开粗也没有精修轮廓——plan_face_mill 应在 builder.finish 抛 PlanningError，
        # 且异常信息不应暗示"是精修轮廓的问题"。
        region = _square_region(side=10.0, cell_mm=1.0, top_z=1.0, floor_z=0.0)
        params = _params()
        params["cut_depth_mm"] = 1.0
        ctx = MillingContext(
            tool=_tool(diameter=12.0),
            top_z=1.0,
            floor_z=0.0,
            parameters=params,
            region=region,
        )
        with self.assertRaises(PlanningError) as cm:
            plan_face_mill(ctx, notes_prefix="")
        self.assertNotIn("精修轮廓", str(cm.exception))


# ================================================================== BUG-005
class PocketMillFinishPassTests(unittest.TestCase):
    """BUG-005：型腔铣精修环应当按刀半径偏置，把侧面余量切掉。"""

    def test_finish_ring_is_inside_the_rough_ring(self) -> None:
        region = _square_region(side=60.0, cell_mm=1.5, top_z=2.0, floor_z=0.0)
        params = _params()
        params["stock_allowance_mm"] = 1.0
        params["cut_depth_mm"] = 2.0
        params["cut_mode"] = "contour"
        params["finish_pass"] = True
        ctx = MillingContext(
            tool=_tool(diameter=6.0),
            top_z=2.0,
            floor_z=0.0,
            parameters=params,
            region=region,
        )
        toolpath = plan_pocket_mill(ctx, notes_prefix="")
        cut_moves = [m for m in toolpath.moves if m.kind is MoveKind.CUT]
        rough = [m for m in cut_moves if m.label and "刀轨" in m.label
                 and "精修" not in m.label]
        finish = [m for m in cut_moves if m.label and "精修" in m.label]
        self.assertGreater(len(rough), 0, "没有开粗刀轨")
        self.assertGreater(len(finish), 0, "精修腔壁未触发")
        # 精修环外缘 x_min 应比开粗第一环**更靠内**（更负）
        # R = 3, allowance = 1 → 开粗第一环外缘 -30+4 = -26；精修环外缘 -30+3 = -27。
        rough_x_min = float(min(m.points[:, 0].min() for m in rough))
        finish_x_min = float(min(m.points[:, 0].min() for m in finish))
        self.assertLess(
            finish_x_min, rough_x_min,
            f"BUG-005 复发：精修环外缘 {finish_x_min} 没比开粗环"
            f" {rough_x_min} 更靠内（侧面余量没切掉）",
        )


# ================================================================== BUG-006
class WaterlineLayerHeightTests(unittest.TestCase):
    """BUG-006：等高铣底部一层未加工。"""

    def test_bottom_layer_is_close_to_floor(self) -> None:
        heights = _layer_heights(20.0, 0.0, 2.0, "top_down")
        # 末层必须切到底面附近，不能留 > 1 step 的余量
        self.assertLessEqual(
            heights[-1], 1.0,
            f"BUG-006 复发：底部 {heights[-1] - 0.0} mm 未加工",
        )
        # 且不应把表面切在 z_bottom 本身（会退化）
        self.assertGreater(heights[-1], 0.0)

    def test_short_depth_still_emits_at_least_one_layer(self) -> None:
        heights = _layer_heights(1.0, 0.0, 2.0, "top_down")
        # 短深度（< step）也至少产生 1 个有效层高，且末层贴底
        self.assertGreaterEqual(len(heights), 1)
        self.assertLessEqual(heights[-1], 1.0)
        self.assertGreater(heights[-1], 0.0)


# ================================================================== BUG-007
class FaceMillZeroDepthTests(unittest.TestCase):
    """BUG-007：face_mill 零深度被吞的异常——应抛 PlanningError 并给出解释。"""

    def test_no_depth_raises_planning_error(self) -> None:
        region = _square_region(side=60.0, cell_mm=1.0, top_z=1.0, floor_z=1.0)
        params = _params()
        ctx = MillingContext(
            tool=_tool(diameter=6.0),
            top_z=1.0,
            floor_z=1.0,
            parameters=params,
            region=region,
        )
        with self.assertRaises(PlanningError) as cm:
            plan_face_mill(ctx, notes_prefix="")
        self.assertIn("没有可切除的深度", str(cm.exception))


# ================================================================== BUG-008
class ScanlineBridgeTests(unittest.TestCase):
    """BUG-008：scanline 1.6*cell 阈值把窄岛/孔桥接。"""

    def test_row_gap_is_split_into_two_intervals(self) -> None:
        # 20 mm × 4 mm 区域，cell=1，沿 X 走刀（angle=0）。
        # 在 col=6 这一行（py=4.5）上设两段 True：x=2..5 与 x=7..10，中间 x=6 断开 1 格。
        # BUG-008 修前原 1.6*cell 阈值在某些角度会因 u 折叠而桥接——按 (row, col)
        # 邻接判据后，row=5→row=7 差 2 > 1 必然断开，**任何角度**都不会再桥接。
        outline = np.array([[0.0, 0.0], [20.0, 0.0], [20.0, 4.0], [0.0, 4.0]],
                           dtype=np.float64)
        region = build_region(outline, [], top_z=1.0, floor_z=0.0, cell_mm=1.0)
        inside = np.zeros(region.inside.shape, dtype=bool)
        _, y_dim = inside.shape
        col = min(6, y_dim - 1)
        inside[2:6, col] = True
        inside[7:11, col] = True
        region.inside = inside
        distance = _distance_to_contour(
            inside, np.asarray(outline, dtype=np.float64),
            region.bounds[0], region.bounds[1], 1.0,
        )
        region.distance = np.where(inside, distance, 0.0)
        target_level = region.bounds[1] + (col + 0.5) * 1.0
        intervals = region.scanline_intervals(target_level, 0.0)
        self.assertEqual(
            len(intervals), 2,
            f"BUG-008 复发：row 中间 1 cell 间隙未被识别为断开，得到 {len(intervals)} 段",
        )

    def test_row_gap_split_at_cell_size_two(self) -> None:
        # cell=2 mm，band=1 mm。
        # 在 col=2（py=1）上设 row=2..8 True + row=10..16 True（row 8..10 之间留 1 格空）。
        # 原 1.6*cell=3.2 阈值：row 间距 = 2 mm < 3.2 mm → 视为合并 → 1 段（BUG-008 复发）。
        # 修复后的 4-邻接判据：row 8 → row 10 差 2 > 1 → 断开 → 2 段。
        outline = np.array([[0.0, 0.0], [20.0, 0.0], [20.0, 4.0], [0.0, 4.0]],
                           dtype=np.float64)
        region = build_region(outline, [], top_z=1.0, floor_z=0.0, cell_mm=2.0)
        inside = np.zeros(region.inside.shape, dtype=bool)
        _, y_dim = inside.shape
        col = min(2, y_dim - 1)
        inside[2:9, col] = True
        inside[10:17, col] = True
        region.inside = inside
        distance = _distance_to_contour(
            inside, np.asarray(outline, dtype=np.float64),
            region.bounds[0], region.bounds[1], 2.0,
        )
        region.distance = np.where(inside, distance, 0.0)
        target_level = region.bounds[1] + (col + 0.5) * 2.0
        intervals = region.scanline_intervals(target_level, 0.0)
        self.assertEqual(
            len(intervals), 2,
            f"BUG-008 复发：cell=2 mm 时 row 隔 1 格（row 间距 < 1.6*cell）"
            f" 仍被桥接，得到 {len(intervals)} 段",
        )


# ----------------------------------------------------------------- BUG-024
# 步距大于刀具直径时，两条刀轨之间存在刀具扫不到的条带，切除仿真会留下
# 全高残料。修复后改用 ``stepover_ratio``（直径比例）——比例天然不会超直径，
# 换刀具时自动跟随；旧 ``stepover_mm`` 仍可作为高级覆写但会写 deprecation 警告。
class StepoverRatioTests(unittest.TestCase):
    """步距按比例换算 + 与刀具联动。"""

    def test_ratio_scales_with_tool_diameter(self) -> None:
        small = MillingContext(
            tool=_tool(diameter=4.0), top_z=1.0, floor_z=0.0,
            parameters=_params(), region=_square_region(),
        )
        large = MillingContext(
            tool=_tool(diameter=12.0), top_z=1.0, floor_z=0.0,
            parameters=_params(), region=_square_region(),
        )
        self.assertAlmostEqual(small.effective_stepover, 0.4 * 4.0, places=9)
        self.assertAlmostEqual(large.effective_stepover, 0.4 * 12.0, places=9)
        # 旧别名 stepover 也同步
        self.assertAlmostEqual(small.stepover, small.effective_stepover, places=9)

    def test_ratio_clamped_to_safe_upper_bound(self) -> None:
        # 即使外部传了 ratio=0.99（理论上能漏切），也会被夹到 0.95
        params = _params()
        params["stepover_ratio"] = 0.99
        ctx = MillingContext(tool=_tool(diameter=10.0), top_z=1.0,
                             floor_z=0.0, parameters=params, region=_square_region())
        self.assertAlmostEqual(ctx.effective_stepover, 0.95 * 10.0, places=9)

    def test_legacy_stepover_mm_still_works_with_warning(self) -> None:
        # 旧字段仍可用，但会写 deprecation 提示（兼容垫片）
        params = _params()
        del params["stepover_ratio"]
        params["stepover_mm"] = 4.0
        ctx = MillingContext(tool=_tool(diameter=10.0), top_z=1.0,
                             floor_z=0.0, parameters=params, region=_square_region())
        self.assertAlmostEqual(ctx.effective_stepover, 4.0, places=9)
        self.assertTrue(
            any("stepover_ratio" in w and "废弃" in w for w in ctx.warnings),
            f"旧 stepover_mm 应触发 deprecation 警告，实际 warnings={ctx.warnings!r}",
        )


class _SimCoverageMixin:
    """跑一遍规划 + 切除仿真，断言型腔内部没有全高残料。"""

    def _assert_full_coverage(self, *, planner: str, mode: str,
                              diameter: float,
                              stepover_ratio: float | None = None,
                              side: float = 60.0, cell_mm: float = 1.0) -> None:
        from toolpath_lab.core.stock import RectangularStock
        from toolpath_lab.simulation.cut_sim import simulate_toolpath

        region = _square_region(side=side, cell_mm=cell_mm, top_z=2.0, floor_z=0.0)
        params = _params()
        if stepover_ratio is not None:
            params["stepover_ratio"] = stepover_ratio
        else:
            # 默认即可（0.4 × D）
            pass
        params["cut_depth_mm"] = 2.0
        params["cut_mode"] = mode
        tool = _tool(diameter=diameter)
        context = MillingContext(tool=tool, top_z=2.0, floor_z=0.0,
                                 parameters=params, region=region)
        plan = (plan_pocket_mill if planner == "pocket" else plan_face_mill)(
            context, notes_prefix="")
        stock = RectangularStock(bounds=PartBounds(
            -side / 2 - 10, -side / 2 - 10, -2.0,
            side / 2 + 10, side / 2 + 10, 2.0))
        simulation = simulate_toolpath(plan, stock, tool_radius=tool.radius_mm,
                                       cell_mm=0.5, max_frames=4)
        height = simulation.final.height
        x0, y0, cell = simulation.final.x0, simulation.final.y0, simulation.final.cell_mm
        ii = np.nonzero(height.reshape(-1) > 1.0)[0]
        rows, cols = np.unravel_index(ii, height.shape)
        xs = x0 + (rows + 0.5) * cell
        ys = y0 + (cols + 0.5) * cell
        inner = (np.abs(xs) < side / 2 - 2.0) & (np.abs(ys) < side / 2 - 2.0)
        self.assertEqual(
            int(inner.sum()), 0,
            f"BUG-024 复发：{planner}/{mode} D{diameter:g} ratio="
            f"{params.get('stepover_ratio', 0.4):g} 在型腔内部留下 "
            f"{int(inner.sum())} 个全高残料格点",
        )


class StepoverCoverageTests(_SimCoverageMixin, unittest.TestCase):
    """用比例步距（默认 0.4 D）+ 不同刀具/模式跑规划 + 仿真，覆盖全零残留。"""

    def test_pocket_contour_small_tool(self) -> None:
        # D4 + 0.4 = 1.6mm：小于直径，仿真必须零残留
        self._assert_full_coverage(planner="pocket", mode="contour",
                                   diameter=4.0, stepover_ratio=0.4)

    def test_pocket_zigzag_small_tool(self) -> None:
        self._assert_full_coverage(planner="pocket", mode="zigzag",
                                   diameter=4.0, stepover_ratio=0.4)

    def test_pocket_zigzag_aggressive(self) -> None:
        # 0.95 接近上限：仿真必须仍能切净（测试算法鲁棒性）
        self._assert_full_coverage(planner="pocket", mode="zigzag",
                                   diameter=6.0, stepover_ratio=0.95)

    def test_face_mill_large_tool(self) -> None:
        self._assert_full_coverage(planner="face", mode="zigzag",
                                   diameter=10.0, stepover_ratio=0.5)

    def test_pocket_one_way(self) -> None:
        self._assert_full_coverage(planner="pocket", mode="one_way",
                                   diameter=6.0, stepover_ratio=0.5)


class AdaptiveGridResolutionTests(unittest.TestCase):
    """仿真栅格自适应密度：默认比旧版 (220) 更细。"""

    def test_adaptive_cell_is_denser_for_typical_stock(self) -> None:
        from toolpath_lab.cam.boundary import (
            adaptive_cell_mm, TARGET_CELLS_PER_AXIS,
        )
        # 100 mm 零件：旧 220 → 0.45 mm/cell；新 360 → 0.28 mm/cell
        cell = adaptive_cell_mm(100.0)
        self.assertLessEqual(cell, 100.0 / TARGET_CELLS_PER_AXIS + 1e-9,
                             f"adaptive_cell_mm(100) = {cell} 未按 {TARGET_CELLS_PER_AXIS} 反推")
        # 必须显著细于旧版的 1/cell(220)
        old_cell = 100.0 / 220.0
        self.assertLess(cell, old_cell - 0.05,
                        f"新 cell={cell} 应比旧 cell={old_cell:.3f} 至少小 0.05 mm")

    def test_adaptive_cell_scales_with_extent(self) -> None:
        from toolpath_lab.cam.boundary import adaptive_cell_mm
        # 大零件 cell 不能比小零件更细
        self.assertGreaterEqual(adaptive_cell_mm(200.0), adaptive_cell_mm(80.0))


# ------------------------------------------------------------------ 清边铣
class _EdgeClearFixture:
    """构造"零件 + 外扩毛坯"的最小 PartModel / Stock 组合（无 OCP 依赖）。"""

    @staticmethod
    def _part(x0: float, y0: float, x1: float, y1: float, z_top: float) -> PartModel:
        positions = np.array(
            [[x0, y0, z_top], [x1, y0, z_top], [x1, y1, z_top]], dtype=np.float64,
        )
        mesh = TessellatedModel(
            positions=positions,
            indices=np.array([[0, 1, 2]], dtype=np.int64),
            normals=np.zeros((1, 3), dtype=np.float64),
            face_of_triangle=np.array([1], dtype=np.int64),
            faces=[],
            vertices=positions,
            source_name="edge-clear-fixture",
        )
        return PartModel(
            model_id="edge-clear", name="edge-clear-fixture",
            mesh=mesh,
            bounds=PartBounds(x0, y0, 0.0, x1, y1, z_top),
            features=[],
        )

    @classmethod
    def request(cls, *, offset: float = 5.0, diameter: float = 8.0):
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation
        from toolpath_lab.core.stock import build_stock

        part = cls._part(-30.0, -30.0, 30.0, 30.0, 20.0)
        stock = build_stock("rectangular", part, {
            "offset_x_mm": offset, "offset_y_mm": offset, "offset_z_mm": 2.0,
        })
        values = _params()
        values["cut_mode"] = "contour"
        request = CAMOperationRequest(
            kind="edge_clear", part=part, face_ids=(), parameters=values,
            tool=_tool(diameter=diameter), stock=stock,
        )
        return part, stock, request, execute_operation(request)


class EdgeClearTests(_EdgeClearFixture, unittest.TestCase):
    """清边铣：毛坯外圈残留的专用工序（复用型腔铣规划器）。"""

    def test_covers_the_margin_between_stock_and_part(self) -> None:
        _, stock, _, result = self.request(offset=5.0)
        cut = [move for move in result.toolpath.moves if move.kind is MoveKind.CUT]
        self.assertGreater(len(cut), 0, "清边刀路不应为空")
        points = np.vstack([move.points for move in cut])
        # 刀心必须到达零件包围盒之外（即外圈环带内）
        outside = (
            (points[:, 0] < -30.0 - 1e-6) | (points[:, 0] > 30.0 + 1e-6)
            | (points[:, 1] < -30.0 - 1e-6) | (points[:, 1] > 30.0 + 1e-6)
        )
        self.assertTrue(bool(outside.any()),
                        "清边刀心没有进入零件包围盒之外的环带：外圈切不到")
        # 同时必须覆盖到毛坯外框附近（与毛坯边距离 < 刀半径 + 半格）
        stock_bounds = stock.bounds
        near_stock_edge = (
            (np.abs(points[:, 0] - stock_bounds.x_min) < 6.0)
            | (np.abs(points[:, 0] - stock_bounds.x_max) < 6.0)
        )
        self.assertTrue(bool(near_stock_edge.any()),
                        "清边刀路没有贴到毛坯外框：外圈内侧有残留")

    def test_tool_center_never_enters_the_part_island(self) -> None:
        # 刀具中心不许越过零件包围盒（允许半格栅格容差）
        _, _, request, result = self.request(offset=5.0)
        cell = request.cell_mm
        cut = [move for move in result.toolpath.moves if move.kind is MoveKind.CUT]
        points = np.vstack([move.points for move in cut])
        tolerance = cell * 0.6
        inside_x = (points[:, 0] > -30.0 + tolerance) & (points[:, 0] < 30.0 - tolerance)
        inside_y = (points[:, 1] > -30.0 + tolerance) & (points[:, 1] < 30.0 - tolerance)
        self.assertFalse(bool((inside_x & inside_y).any()),
                        "清边刀心进入零件包围盒内部：会过切零件")

    def test_missing_stock_raises(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation
        part = self._part(-30.0, -30.0, 30.0, 30.0, 20.0)
        request = CAMOperationRequest(
            kind="edge_clear", part=part, face_ids=(),
            parameters=_params(), tool=_tool(), stock=None,
        )
        with self.assertRaises(PlanningError):
            execute_operation(request)

    def test_margin_smaller_than_tool_radius_still_cuts(self) -> None:
        # 毛坯只比零件大 1 mm、刀具 D8（R=4）：刀具中心允许悬在毛坯外空切，
        # 只要切削刃不越过零件轮廓就应该能清完外圈
        _, _, _, result = self.request(offset=1.0, diameter=8.0)
        cut = [move for move in result.toolpath.moves if move.kind is MoveKind.CUT]
        self.assertGreater(len(cut), 0,
                           "窄外圈（< 刀半径）也应能清边：中心可以走出毛坯空切")
        # 每一圈到零件包围盒的距离必须 ≥ R - 半格（栅格容差）
        cell = 1.0
        points = np.vstack([move.points for move in cut])
        dx = np.maximum(np.maximum(-30.0 - points[:, 0], points[:, 0] - 30.0), 0.0)
        dy = np.maximum(np.maximum(-30.0 - points[:, 1], points[:, 1] - 30.0), 0.0)
        dist_to_part = np.hypot(dx, dy)
        self.assertGreaterEqual(float(dist_to_part.min()), 4.0 - cell * 0.6,
                                "刀具切削刃越过零件轮廓：过切")

    def test_zero_margin_raises(self) -> None:
        # 毛坯与零件轮廓重合：没有外圈可清，应给出可读错误
        with self.assertRaises(PlanningError) as caught:
            self.request(offset=0.0)
        self.assertIn("清边铣无需执行", str(caught.exception))

    def test_kind_registered_in_catalog(self) -> None:
        from toolpath_lab.cam.service import planning_catalog
        catalog = planning_catalog()
        ids = [entry["id"] for entry in catalog["operations"]]
        self.assertIn("edge_clear", ids)
        entry = next(item for item in catalog["operations"] if item["id"] == "edge_clear")
        self.assertFalse(entry["needs_faces"], "清边铣不应要求选面")


class SimulationStepSizeTests(unittest.TestCase):
    """仿真步长收紧：max_frames 默认 180，子步间距 0.5 × cell_mm。"""

    def test_default_max_frames_at_least_180(self) -> None:
        from toolpath_lab.simulation.cut_sim import DEFAULT_MAX_FRAMES
        self.assertGreaterEqual(
            DEFAULT_MAX_FRAMES, 180,
            "默认 max_frames 太小，慢放会卡 / 帧间距过大",
        )

    def test_steps_cell_ratio_subdivides_cell(self) -> None:
        from toolpath_lab.simulation.cut_sim import STEPS_CELL_RATIO
        # 0.5 是设计值：让长刀路上相邻子步不出现台阶
        self.assertLess(STEPS_CELL_RATIO, 1.0)
        self.assertGreaterEqual(STEPS_CELL_RATIO, 0.25)
        self.assertLessEqual(STEPS_CELL_RATIO, 0.8)

    def test_finer_substeps_yield_more_frames(self) -> None:
        """用 STEPS_CELL_RATIO 收紧子步后，同 max_frames 下应产生更多帧。"""

        from toolpath_lab.simulation.cut_sim import simulate_toolpath
        from toolpath_lab.core.path import Move, MoveKind, Toolpath
        from toolpath_lab.core.stock import RectangularStock, PartBounds

        stock = RectangularStock(bounds=PartBounds(-60, -60, 0, 60, 60, 5))
        points = np.column_stack([
            np.linspace(-50.0, 50.0, 201), np.zeros(201), np.full(201, 4.0),
        ])
        toolpath = Toolpath(
            moves=(Move(MoveKind.CUT, points, 800.0),),
            planner="test", planner_label="t",
        )
        sim = simulate_toolpath(toolpath, stock, tool_radius=3.0,
                                cell_mm=0.5, max_frames=180)
        self.assertGreaterEqual(
            len(sim.frames), 100,
            f"细化子步应产生 ≥ 100 帧，实测 {len(sim.frames)}",
        )


if __name__ == "__main__":
    unittest.main()