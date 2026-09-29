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

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.cam.common import (MillingContext, MoveBuilder, depth_levels,
                                     in_level_transfer, stepped_levels)
from toolpath_lab.cam.boundary import MachiningRegion, offset_outline_polygons
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Toolpath


@dataclass(slots=True)
class _FaceJob:
    """一个加工面（区域）的平面铣规划现场（层列表之外的全部准备）。"""

    context: MillingContext
    region: MachiningRegion
    prefix: str
    mode: str
    angle: float
    offset: float
    stepover: float
    floor_target: float
    levels: list[float] = field(default_factory=list)


def _prepare_face(context: MillingContext, notes_prefix: str) -> _FaceJob:
    """层循环之外的全部准备：走刀方式、扫描方向、偏置与步距。"""

    region = context.region
    if region is None:
        raise PlanningError(f"{notes_prefix}平面铣缺少加工区域")

    mode = str(context.parameters.get("cut_mode", "zigzag"))
    if mode == "contour":
        mode = "zigzag"
        context.warn("平面铣使用往复/单向走刀；已按往复处理")
    angle = float(context.parameters.get("direction_deg", 0.0))
    offset = context.tool_radius + context.stock_allowance
    # 步距夹到刀具直径内，避免两条刀轨之间残留毛坯（详见 effective_stepover）
    stepover = context.effective_stepover
    floor_target = float(region.floor_z) + context.finish_allowance
    return _FaceJob(context=context, region=region, prefix=notes_prefix, mode=mode,
                    angle=angle, offset=offset, stepover=stepover, floor_target=floor_target)


def _cut_face_level(builder: MoveBuilder, job: _FaceJob, level_index: int,
                    target_z: float, previous_z: float, first_cut: bool) -> bool:
    """切这个区域的某一层（往复扫描 + 每层轮廓精修），返回新的 ``first_cut``。"""

    region = job.region
    context = job.context
    # 本层要切除的深度：从上一层的高度切到 target_z
    depth = previous_z - target_z
    if depth <= 1e-9:
        return first_cut
    mask = region.offset_mask(job.offset)
    if not mask.any():
        context.warn(
            f"第 {level_index + 1} 层：刀具半径 {context.tool_radius:g} mm + 余量 "
            f"{context.stock_allowance:g} mm 已超过加工区域，该层被跳过"
        )
        return first_cut

    intervals = _scan_intervals(region, mask, job.angle, job.stepover, context)
    if not intervals:
        return first_cut
    _emit_layer(builder, context, intervals, target_z, region, job.mode,
                first_cut=first_cut, level_index=level_index, label_prefix=job.prefix)
    first_cut = False

    # BUG-004 修：先前判据 offset ≤ 0.5·R+1e-9 恒假（offset = R+allowance ≥ R），
    # 精修轮廓永远不会被触发。改成"按刀半径 R 偏置后等距区域仍存在"，且
    # 调用方传入 R 而非 R+allowance——把侧面余量留到精修这一刀切掉。
    if bool(context.parameters.get("finish_pass", True)) \
            and region.offset_area_mm2(context.tool_radius) > 0.0:
        _emit_finish_contour(builder, context, region, context.tool_radius, target_z,
                             label_prefix=job.prefix)
    return first_cut


def plan_face_mill(context: MillingContext, *, notes_prefix: str = "") -> Toolpath:
    """生成平面铣刀路。"""

    job = _prepare_face(context, notes_prefix)
    region = job.region
    levels = depth_levels(region.top_z, job.floor_target, context.cut_depth)
    # BUG-007 修：先前在 levels 为空时塞进 [top_z] 占位，但所有层 depth=0 被 continue，
    # 末尾 builder.finish 抛"没有生成任何刀轨"，前面的 warn 也被异常吞掉——对用户毫无提示。
    # 与 pocket_mill 一致：levels 空就直接抛 PlanningError，让上层弹窗给解释。
    if not levels:
        raise PlanningError(
            f"{notes_prefix}平面铣没有可切除的深度：区域顶面 {region.top_z:g} mm，面底 {region.floor_z:g} mm，"
            "请检查毛坯顶面是否高于选中面、或减小底面余量"
        )
    job.levels = levels

    builder = MoveBuilder(context)
    first_cut = True
    for level_index, target_z in enumerate(job.levels):
        previous_z = float(region.top_z) if level_index == 0 else float(job.levels[level_index - 1])
        first_cut = _cut_face_level(builder, job, level_index, target_z, previous_z, first_cut)

    notes = [
        f"{notes_prefix}平面铣：{len(job.levels)} 层，每层切深 ≤ {context.cut_depth:g} mm，"
        f"步距 {job.stepover:g} mm，刀具 D{context.tool.diameter_mm:g} mm",
        f"刀心区域按刀具半径 {context.tool_radius:g} mm + 侧面余量 "
        f"{context.stock_allowance:g} mm 向内偏置；底面余量 {context.finish_allowance:g} mm",
    ]
    return builder.finish(planner="face_mill", label="平面铣", notes=notes)


def plan_face_mill_multi(items: Sequence[tuple[MillingContext, str]], *,
                         order: str = "level_first") -> Toolpath:
    """多加工面（多区域）的统一层调度（UG/NX 的层优先 / 深度优先）。

    层高用绝对网格（:func:`~toolpath_lab.cam.common.stepped_levels`）在区域间对齐：
    层优先=每层把各面在该高度的加工做完再下降；深度优先=一个面铣完再换下一个。
    两种顺序只差遍历顺序，层高与每层刀路完全一致；单个面时两者等价。
    """

    jobs = [_prepare_face(context, prefix) for context, prefix in items]
    global_top = max(float(job.region.top_z) for job in jobs)
    layers = stepped_levels(global_top, [job.floor_target for job in jobs],
                            float(jobs[0].context.cut_depth))
    for job in jobs:
        top = float(job.region.top_z)
        job.levels = [z for z in layers if job.floor_target - 1e-9 <= z <= top - 1e-9]
        if not job.levels:
            raise PlanningError(
                f"{job.prefix}平面铣没有可切除的深度：区域顶面 {top:g} mm，"
                f"面底 {job.region.floor_z:g} mm，请检查毛坯顶面是否高于选中面、或减小底面余量"
            )

    # 共享一个 MoveBuilder，安全高度取全局最高顶面。
    lead = jobs[0].context
    dispatch = MillingContext(
        tool=lead.tool,
        top_z=global_top,
        floor_z=min(float(job.region.floor_z) for job in jobs),
        parameters=lead.parameters,
        region=None,
    )
    builder = MoveBuilder(dispatch)
    first_cut = True

    def previous_z_of(job: _FaceJob, level_index: int) -> float:
        return float(job.region.top_z) if level_index == 0 \
            else float(job.levels[level_index - 1])

    if str(order) == "depth_first":
        for job in jobs:
            for level_index, target_z in enumerate(job.levels):
                first_cut = _cut_face_level(builder, job, level_index, target_z,
                                            previous_z_of(job, level_index), first_cut)
    else:
        # 未知取值一律按层优先（参数校验在上游，这里是防御性回退，与 cut_mode 同风格）。
        index_of = [{z: index for index, z in enumerate(job.levels)} for job in jobs]
        all_levels = sorted({z for job in jobs for z in job.levels}, reverse=True)
        for target_z in all_levels:
            for job, mapping in zip(jobs, index_of):
                level_index = mapping.get(target_z)
                if level_index is None:
                    continue
                first_cut = _cut_face_level(builder, job, level_index, target_z,
                                            previous_z_of(job, level_index), first_cut)

    notes: list[str] = []
    for job in jobs:
        context = job.context
        notes.extend([
            f"{job.prefix}平面铣：{len(job.levels)} 层，每层切深 ≤ {context.cut_depth:g} mm，"
            f"步距 {job.stepover:g} mm，刀具 D{context.tool.diameter_mm:g} mm",
            f"{job.prefix}刀心区域按刀具半径 {context.tool_radius:g} mm + 侧面余量 "
            f"{context.stock_allowance:g} mm 向内偏置；底面余量 {context.finish_allowance:g} mm",
        ])
    if str(order) == "depth_first":
        notes.append(
            f"切削顺序：深度优先——单个区域从上到下切完再换下一个（共 {len(jobs)} 个加工面）"
        )
    else:
        notes.append(
            f"切削顺序：层优先——各区域在同一高度合并、逐层下切（共 {len(jobs)} 个加工面）"
        )
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
                level_index: int, label_prefix: str = "") -> None:
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
                builder.cut(points, label=f"{label_prefix}第 {level_index + 1} 层 第 1 刀")
                continue
            previous = builder.last_point
            if previous is None:
                builder.rapid_to_safe(points[0], label="定位到下刀点")
                builder.plunge(points[0])
                builder.cut(points, label=f"{label_prefix}第 {level_index + 1} 层")
                continue
            gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
            z_gap = abs(float(previous[2]) - float(points[0][2]))
            if mode == "zigzag" and gap < 1e-6:
                builder.cut(points, label=f"{label_prefix}第 {level_index + 1} 层 连接刀")
            elif mode != "one_way" and in_level_transfer(
                    region, context.tool_radius, previous[:2], points[0][:2]):
                # 往复的相邻刀线（以及层间的第一刀）在层内直接平移/斜降连过去，
                # 一次下刀切完整层；one_way 的语义仍是"每刀抬刀、同向落刀"。
                if gap > 1e-9 or z_gap > 1e-9:
                    builder.link(np.vstack([previous, points[0]]),
                                 label=f"{label_prefix}层内转移")
                builder.cut(points, label=f"{label_prefix}第 {level_index + 1} 层")
            else:
                builder.rapid_to_safe(points[0], label="层内转移")
                builder.plunge(points[0])
                builder.cut(points, label=f"{label_prefix}第 {level_index + 1} 层")
        builder.next_pass()


def _emit_finish_contour(builder: MoveBuilder, context: MillingContext,
                         region: MachiningRegion, offset: float, target_z: float, *,
                         label_prefix: str = "") -> None:
    """沿等距轮廓补一刀精修（去起点的平移优先走层内，出界才抬刀）。"""

    polygons = offset_outline_polygons(region, offset)
    previous = builder.last_point
    for polygon in polygons:
        if polygon.shape[0] < 3:
            continue
        loop = np.vstack([polygon, polygon[:1]])
        points = np.column_stack((loop, np.full(loop.shape[0], target_z)))
        if previous is not None and in_level_transfer(
                region, context.tool_radius, previous[:2], points[0][:2]):
            gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
            z_gap = abs(float(previous[2]) - float(points[0][2]))
            if gap > 1e-9 or z_gap > 1e-9:
                builder.link(np.vstack([previous, points[0]]),
                             label=f"{label_prefix}层内转移")
        else:
            builder.rapid_to_safe(points[0], label=f"{label_prefix}定位到轮廓起点")
            builder.plunge(points[0], label=f"{label_prefix}下刀（精修）")
        builder.cut(points, label=f"{label_prefix}精修轮廓")
        previous = builder.last_point


__all__ = ["plan_face_mill", "plan_face_mill_multi"]
