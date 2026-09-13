"""型腔铣（cavity / pocket milling）。

型腔铣的目标是把选中的腔体（含岛屿）按层切除到腔底。首期实现两种走刀方式：

``contour``（默认，环切 contour-parallel）
    按步距做一层层等距环，从腔壁向中心收敛——刀具始终贴着已加工面走，负荷均匀，
    是 NX "Cavity Mill / Follow Periphery" 的做法。
``zigzag``
    每层做平行扫描线，行程短、空刀少，适合开阔的型腔。

区域运算（外轮廓向内偏置、岛屿向外偏置并自动合并）由 :mod:`toolpath_lab.cam.boundary`
的距离场统一处理，因此"腔里有几个岛、形状多复杂"都不需要在刀路代码里特判。
"""

from __future__ import annotations

from math import ceil
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.cam.boundary import MachiningRegion, offset_outline_polygons
from toolpath_lab.cam.common import MillingContext, MoveBuilder, depth_levels
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Toolpath

#: 环切时两层之间的抬刀方式：True 时抬到安全面再定位（更安全，空刀多）。
SAFE_LINK_BETWEEN_RINGS = False


def plan_pocket_mill(context: MillingContext, *, notes_prefix: str = "") -> Toolpath:
    """生成型腔铣刀路。"""

    region: MachiningRegion = context.region
    if region is None:
        raise PlanningError("型腔铣缺少加工区域")

    levels = depth_levels(region.top_z, region.floor_z, context.cut_depth,
                          finish_allowance=context.finish_allowance)
    if not levels:
        context.warn("选中面与毛坯顶面等高，型腔铣没有可切除的深度")
        return _empty_notes_toolpath(context, region, notes_prefix)

    mode = str(context.parameters.get("cut_mode", "contour"))
    if mode not in {"contour", "zigzag", "one_way"}:
        mode = "contour"
    builder = MoveBuilder(context)
    first_cut = True
    base_offset = context.tool_radius + context.stock_allowance
    ring_count = 0

    for level_index, target_z in enumerate(levels):
        previous_z = float(region.top_z) if level_index == 0 else float(levels[level_index - 1])
        depth = previous_z - target_z
        if depth <= 1e-9:
            continue
        if mode == "contour":
            rings = _contour_rings(region, base_offset, context.stepover)
            if not rings:
                context.warn(
                    f"第 {level_index + 1} 层：区域在偏置 {base_offset:g} mm 后为空，已跳过"
                )
                continue
            for ring_index, polygon in enumerate(rings):
                ring_count += 1
                points = np.column_stack((np.vstack([polygon, polygon[:1]]),
                                          np.full(polygon.shape[0] + 1, target_z)))
                _emit_ring(builder, context, points, first_cut, level_index, ring_index)
                first_cut = False
        else:
            mask = region.offset_mask(base_offset)
            if not mask.any():
                context.warn(f"第 {level_index + 1} 层：偏置后区域为空，已跳过")
                continue
            angle = float(context.parameters.get("direction_deg", 0.0))
            passes = _zigzag_passes(region, mask, angle, context.stepover)
            zig = mode == "zigzag"
            for pass_index, world_pass in enumerate(passes):
                if not zig and pass_index % 2 == 1:
                    world_pass = world_pass[::-1]
                elif zig and pass_index % 2 == 1:
                    world_pass = world_pass[::-1]
                points = np.column_stack((world_pass,
                                          np.full(world_pass.shape[0], target_z)))
                _emit_ring(builder, context, points, first_cut, level_index, pass_index)
                first_cut = False

        if bool(context.parameters.get("finish_pass", True)):
            _emit_wall_finish(builder, context, region, base_offset, target_z)

    notes = [
        f"{notes_prefix}型腔铣（{'环切' if mode == 'contour' else '平行扫描'}）："
        f"{len(levels)} 层，每层切深 ≤ {context.cut_depth:g} mm，步距 {context.stepover:g} mm，"
        f"共 {ring_count} 条刀轨",
        f"刀具 D{context.tool.diameter_mm:g} mm；径向余量 {context.stock_allowance:g} mm，"
        f"底面余量 {context.finish_allowance:g} mm；岛屿自动避让",
    ]
    return builder.finish(planner="pocket_mill", label="型腔铣", notes=notes)


def _empty_notes_toolpath(context: MillingContext, region: MachiningRegion,
                          notes_prefix: str) -> Toolpath:
    raise PlanningError(
        f"{notes_prefix}型腔铣没有可切除的深度：区域顶面 {region.top_z:g} mm，"
        f"腔底 {region.floor_z:g} mm"
    )


# ------------------------------------------------------------------ 内部
def _contour_rings(region: MachiningRegion, base_offset: float, stepover: float
                   ) -> list[NDArray[np.float64]]:
    """由外向内逐圈取等距轮廓，直到区域中心。"""

    rings: list[NDArray[np.float64]] = []
    offset = base_offset
    max_distance = float(region.distance.max()) if region.distance.size else 0.0
    guard = 0
    while offset <= max_distance + 1e-9 and guard < 4096:
        guard += 1
        polygons = offset_outline_polygons(region, offset, min_area_mm2=1.0)
        if not polygons:
            break
        rings.extend(polygons)
        offset += max(stepover, region.cell_mm)
    return rings


def _zigzag_passes(region: MachiningRegion, mask: NDArray[np.bool_], angle: float,
                   stepover: float) -> list[NDArray[np.float64]]:
    """平行扫描线，返回每条刀线的世界坐标端点。"""

    angle_rad = np.radians(angle)
    u_axis = np.array([np.cos(angle_rad), np.sin(angle_rad)], dtype=np.float64)
    v_axis = np.array([-np.sin(angle_rad), np.cos(angle_rad)], dtype=np.float64)
    levels = region.scanline_levels(angle, stepover, mask)
    passes: list[NDArray[np.float64]] = []
    for level in levels:
        for interval in region.scanline_intervals(float(level), angle, mask):
            start = interval[0] * u_axis + interval[2] * v_axis
            end = interval[1] * u_axis + interval[2] * v_axis
            passes.append(np.array([[start[0], start[1]], [end[0], end[1]]], dtype=np.float64))
    return passes


def _emit_ring(builder: MoveBuilder, context: MillingContext, points: NDArray[np.float64],
               first_cut: bool, level_index: int, ring_index: int) -> None:
    """走一条闭合环（或一段扫描线）。"""

    label = f"第 {level_index + 1} 层 第 {ring_index + 1} 条刀轨"
    previous = builder.last_point
    if previous is None or first_cut:
        builder.rapid_to_safe(points[0], label="定位到下刀点")
        builder.plunge(points[0], label=f"下刀 Z{points[0][2]:.3f}")
        builder.cut(points, label=label)
        return
    gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
    if SAFE_LINK_BETWEEN_RINGS or gap > 4.0 * context.tool_radius:
        builder.rapid_to_safe(points[0], label="环间转移")
        builder.plunge(points[0])
    elif abs(float(previous[2]) - float(points[0][2])) > 1e-9:
        builder.link(points, label="环间连接")
    builder.cut(points, label=label)


def _emit_wall_finish(builder: MoveBuilder, context: MillingContext,
                      region: MachiningRegion, offset: float, target_z: float) -> None:
    """沿腔壁补一刀精修（把侧面余量留到这一刀）。"""

    polygons = offset_outline_polygons(region, offset)
    for polygon in polygons:
        if polygon.shape[0] < 3:
            continue
        loop = np.vstack([polygon, polygon[:1]])
        points = np.column_stack((loop, np.full(loop.shape[0], target_z)))
        builder.rapid_to_safe(points[0], label="定位到腔壁起点")
        builder.plunge(points[0], label="下刀（壁精修）")
        builder.cut(points, label="精修腔壁")


__all__ = ["plan_pocket_mill"]
