"""平面铣（face milling）。

用于"把一块平面区域铣平"：选中的面（通常是毛坯顶面或零件的顶面）给出加工区域，
刀具按步距往复（或单向）走平行扫描线，并按每层切深逐层下降。

与基座的栅格刀路相比，这里的区别是：

- 区域来自**真实零件的边界**（含岛屿），而不是规则形状；
- 每层的刀心区域按"刀具半径 + 侧面余量"向内偏置；
- 支持逐层切深、层间快速转移、可选精修轮廓。

出刀顺序与 NX 的 Face Milling 一致：先下刀到第一条刀线的起点 → 逐条走刀 → 抬刀。
"""

from __future__ import annotations

from math import ceil
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.cam.common import MillingContext, MoveBuilder, depth_levels
from toolpath_lab.cam.boundary import MachiningRegion, offset_outline_polygons
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Toolpath


def plan_face_mill(context: MillingContext, *, notes_prefix: str = "") -> Toolpath:
    """生成平面铣刀路。"""

    region: MachiningRegion = context.region
    if region is None:
        raise PlanningError("平面铣缺少加工区域")

    levels = depth_levels(region.top_z, region.floor_z, context.cut_depth,
                          finish_allowance=context.finish_allowance)
    # BUG-007 修：先前在 levels 为空时塞进 [top_z] 占位，但所有层 depth=0 被 continue，
    # 末尾 builder.finish 抛"没有生成任何刀轨"，前面的 warn 也被异常吞掉——对用户毫无提示。
    # 与 pocket_mill 一致：levels 空就直接抛 PlanningError，让上层弹窗给解释。
    if not levels:
        raise PlanningError(
            f"平面铣没有可切除的深度：区域顶面 {region.top_z:g} mm，面底 {region.floor_z:g} mm，"
            "请检查毛坯顶面是否高于选中面、或减小底面余量"
        )

    mode = str(context.parameters.get("cut_mode", "zigzag"))
    if mode == "contour":
        mode = "zigzag"
        context.warn("平面铣使用往复/单向走刀；已按往复处理")
    angle = float(context.parameters.get("direction_deg", 0.0))
    offset = context.tool_radius + context.stock_allowance
    # 步距夹到刀具直径内，避免两条刀轨之间残留毛坯（详见 effective_stepover）
    stepover = context.effective_stepover
    builder = MoveBuilder(context)
    first_cut = True

    for level_index, target_z in enumerate(levels):
        # 本层要切除的深度：从上一层的高度切到 target_z
        previous_z = float(region.top_z) if level_index == 0 else float(levels[level_index - 1])
        depth = previous_z - target_z
        if depth <= 1e-9:
            continue
        mask = region.offset_mask(offset)
        if not mask.any():
            context.warn(
                f"第 {level_index + 1} 层：刀具半径 {context.tool_radius:g} mm + 余量 "
                f"{context.stock_allowance:g} mm 已超过加工区域，该层被跳过"
            )
            continue

        intervals = _scan_intervals(region, mask, angle, stepover, context)
        if not intervals:
            continue
        _emit_layer(builder, context, intervals, target_z, region, mode,
                    first_cut=first_cut, level_index=level_index)
        first_cut = False

        # BUG-004 修：先前判据 offset ≤ 0.5·R+1e-9 恒假（offset = R+allowance ≥ R），
        # 精修轮廓永远不会被触发。改成"按刀半径 R 偏置后等距区域仍存在"，且
        # 调用方传入 R 而非 R+allowance——把侧面余量留到精修这一刀切掉。
        if bool(context.parameters.get("finish_pass", True)) \
                and region.offset_area_mm2(context.tool_radius) > 0.0:
            _emit_finish_contour(builder, context, region, context.tool_radius, target_z)

    notes = [
        f"{notes_prefix}平面铣：{len(levels)} 层，每层切深 ≤ {context.cut_depth:g} mm，"
        f"步距 {stepover:g} mm，刀具 D{context.tool.diameter_mm:g} mm",
        f"刀心区域按刀具半径 {context.tool_radius:g} mm + 侧面余量 "
        f"{context.stock_allowance:g} mm 向内偏置；底面余量 {context.finish_allowance:g} mm",
    ]
    return builder.finish(planner="face_mill", label="平面铣", notes=notes)


# ------------------------------------------------------------------ 内部
def _scan_intervals(region: MachiningRegion, mask: NDArray[np.bool_], angle: float,
                    stepover: float, context: MillingContext
                    ) -> list[list[tuple[float, float, float]]]:
    """按走刀方向布刀线，返回"每条刀线的若干区间"。"""

    angle_rad = np.radians(angle)
    u_axis = np.array([np.cos(angle_rad), np.sin(angle_rad)], dtype=np.float64)
    v_axis = np.array([-np.sin(angle_rad), np.cos(angle_rad)], dtype=np.float64)

    levels = region.scanline_levels(angle, stepover, mask)
    result: list[list[tuple[float, float, float]]] = []
    for level in levels:
        intervals = region.scanline_intervals(float(level), angle, mask)
        if intervals:
            result.append(intervals)
    if not result:
        return []
    # 按刀线位置排序：往复时交替方向才有意义
    result.sort(key=lambda item: item[0][2] * (1.0 if v_axis[1] >= 0 else -1.0))
    _ = u_axis
    return result


def _to_world(interval: tuple[float, float, float], angle: float) -> NDArray[np.float64]:
    angle_rad = np.radians(angle)
    u_axis = np.array([np.cos(angle_rad), np.sin(angle_rad)], dtype=np.float64)
    v_axis = np.array([-np.sin(angle_rad), np.cos(angle_rad)], dtype=np.float64)
    start = interval[0] * u_axis + interval[2] * v_axis
    end = interval[1] * u_axis + interval[2] * v_axis
    return np.array([[start[0], start[1]], [end[0], end[1]]], dtype=np.float64)


def _emit_layer(builder: MoveBuilder, context: MillingContext,
                passes: list[list[tuple[float, float, float]]], target_z: float,
                region: MachiningRegion, mode: str, *, first_cut: bool,
                level_index: int) -> None:
    """走完一层的所有刀线。"""

    angle = float(context.parameters.get("direction_deg", 0.0))
    for pass_index, intervals in enumerate(passes):
        reverse = mode == "zigzag" and pass_index % 2 == 1
        for interval_index, interval in enumerate(intervals):
            world = _to_world(interval, angle)
            if reverse:
                world = world[::-1]
            points = np.column_stack((world, np.full(2, target_z)))
            if first_cut and pass_index == 0 and interval_index == 0:
                builder.rapid_to_safe(points[0], label="定位到下刀点")
                builder.plunge(points[0], label=f"下刀 Z{target_z:.3f}")
                builder.cut(points, label=f"第 {level_index + 1} 层 第 1 刀")
                continue
            previous = builder.last_point
            if previous is None:
                builder.rapid_to_safe(points[0], label="定位到下刀点")
                builder.plunge(points[0])
                builder.cut(points, label=f"第 {level_index + 1} 层")
                continue
            if mode == "zigzag" and float(np.linalg.norm(previous[:2] - points[0][:2])) < 1e-6:
                builder.cut(points, label=f"第 {level_index + 1} 层 连接刀")
            else:
                builder.rapid_to_safe(points[0], label="层内转移")
                builder.plunge(points[0])
                builder.cut(points, label=f"第 {level_index + 1} 层")
        builder.next_pass()


def _emit_finish_contour(builder: MoveBuilder, context: MillingContext,
                         region: MachiningRegion, offset: float, target_z: float) -> None:
    """沿等距轮廓补一刀精修。"""

    polygons = offset_outline_polygons(region, offset)
    for polygon in polygons:
        if polygon.shape[0] < 3:
            continue
        loop = np.vstack([polygon, polygon[:1]])
        points = np.column_stack((loop, np.full(loop.shape[0], target_z)))
        builder.rapid_to_safe(points[0], label="定位到轮廓起点")
        builder.plunge(points[0], label="下刀（精修）")
        builder.cut(points, label="精修轮廓")


__all__ = ["plan_face_mill"]
