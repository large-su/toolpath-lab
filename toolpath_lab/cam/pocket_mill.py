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
    # 步距夹到刀具直径内，避免两条刀轨之间残留毛坯（详见 effective_stepover）
    stepover = context.effective_stepover
    ring_count = 0

    for level_index, target_z in enumerate(levels):
        previous_z = float(region.top_z) if level_index == 0 else float(levels[level_index - 1])
        depth = previous_z - target_z
        if depth <= 1e-9:
            continue
        if mode == "contour":
            rings, last_offset = _contour_rings(region, base_offset, stepover)
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
            # BUG-025 修：最后一环到区域最深点的距离若超过刀半径，中心会剩一块
            # 谁都切不到的"内岛"（中心处的小等距轮廓已被 min_area 过滤）。
            # 在最深点补一个小清理环，把内岛吃掉。
            max_distance = float(region.distance.max()) if region.distance.size else 0.0
            if max_distance - last_offset > context.tool_radius + 1e-6:
                cleanup = _cleanup_loop(region, target_z)
                if cleanup is not None:
                    ring_count += 1
                    _emit_ring(builder, context, cleanup, first_cut,
                               level_index, len(rings), label_suffix="（中心清理）")
                    first_cut = False
        else:
            mask = region.offset_mask(base_offset)
            if not mask.any():
                context.warn(f"第 {level_index + 1} 层：偏置后区域为空，已跳过")
                continue
            angle = float(context.parameters.get("direction_deg", 0.0))
            passes = _zigzag_passes(region, mask, angle, stepover)
            # BUG-009 修：先前 one_way / zigzag 两个分支效果相同（都翻折），
            # 单向走刀失效。单向走刀的语义是"每刀抬刀 → 同向落刀"，不翻折；
            # 往复（zigzag）才是"奇数刀翻折 + 邻刀相连"。
            force_safe_link = (mode == "one_way")
            for pass_index, world_pass in enumerate(passes):
                if mode == "zigzag" and pass_index % 2 == 1:
                    world_pass = world_pass[::-1]
                points = np.column_stack((world_pass,
                                          np.full(world_pass.shape[0], target_z)))
                _emit_ring(builder, context, points, first_cut, level_index, pass_index,
                           force_safe_link=force_safe_link)
                first_cut = False

        # BUG-005 修：先前精修用 base_offset (= R + stock_allowance) 偏置——
        # 与开粗第一环原样重走，既没切掉余量又多一圈空程。改成"按刀半径 R 偏置"
        # 开粗按 R+allowance、精修按 R，侧面余量恰好被精修这一刀吃掉。
        if bool(context.parameters.get("finish_pass", True)) \
                and region.offset_area_mm2(context.tool_radius) > 0.0:
            _emit_wall_finish(builder, context, region, context.tool_radius, target_z)

    notes = [
        f"{notes_prefix}型腔铣（{'环切' if mode == 'contour' else '平行扫描'}）："
        f"{len(levels)} 层，每层切深 ≤ {context.cut_depth:g} mm，步距 {stepover:g} mm，"
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
                   ) -> tuple[list[NDArray[np.float64]], float]:
    """由外向内逐圈取等距轮廓，直到区域中心。

    返回 ``(rings, last_offset)``：``last_offset`` 是最后一圈所在的等距层级，
    调用方用它判断"最深点是否已被最后一圈 + 刀半径覆盖"（BUG-025：环切在
    型腔中心留残料——最后一环到最深点的距离可以超过刀半径，而中心处的小
    等距轮廓又被 ``min_area_mm2`` 过滤，谁都不补这一刀）。
    """

    rings: list[NDArray[np.float64]] = []
    offset = base_offset
    max_distance = float(region.distance.max()) if region.distance.size else 0.0
    last_offset = -1.0
    guard = 0
    while offset <= max_distance + 1e-9 and guard < 4096:
        guard += 1
        polygons = offset_outline_polygons(region, offset, min_area_mm2=1.0)
        if not polygons:
            break
        rings.extend(polygons)
        last_offset = offset
        # 间距下限取半格（等距轮廓的分辨极限），不再取整格：
        # 整格下限会在"粗栅格 + 小刀具"时间距超过刀直径，两环之间留下残料。
        offset += max(stepover, 0.5 * region.cell_mm)
    return rings, last_offset


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
               first_cut: bool, level_index: int, ring_index: int, *,
               force_safe_link: bool = False, label_suffix: str = "") -> None:
    """走一条闭合环（或一段扫描线）。"""

    label = f"第 {level_index + 1} 层 第 {ring_index + 1} 条刀轨{label_suffix}"
    previous = builder.last_point
    if previous is None or first_cut:
        builder.rapid_to_safe(points[0], label="定位到下刀点")
        builder.plunge(points[0], label=f"下刀 Z{points[0][2]:.3f}")
        builder.cut(points, label=label)
        return
    gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
    if force_safe_link or SAFE_LINK_BETWEEN_RINGS or gap > 4.0 * context.tool_radius:
        builder.rapid_to_safe(points[0], label="环间转移")
        builder.plunge(points[0])
    elif abs(float(previous[2]) - float(points[0][2])) > 1e-9:
        builder.link(points, label="环间连接")
    builder.cut(points, label=label)


def _cleanup_loop(region: MachiningRegion, target_z: float) -> NDArray[np.float64] | None:
    """在区域最深点构造一个小清理环（BUG-025：环切中心残料）。

    内岛一定在最后一环 + 刀半径之外、且距离最深点不超过一个步距，
    因此以最深点为圆心走一个半格小圆即可全部吃掉。
    """

    if not region.distance.size:
        return None
    di, dj = np.unravel_index(int(np.argmax(region.distance)), region.distance.shape)
    center_x = region.bounds[0] + (di + 0.5) * region.cell_mm
    center_y = region.bounds[1] + (dj + 0.5) * region.cell_mm
    radius = max(0.5 * region.cell_mm, 0.2)
    angles = np.linspace(0.0, 2.0 * np.pi, 24, endpoint=True)
    loop = np.column_stack((center_x + radius * np.cos(angles),
                            center_y + radius * np.sin(angles)))
    return np.column_stack((loop, np.full(loop.shape[0], target_z)))


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
