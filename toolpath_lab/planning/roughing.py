"""可选的前置分层清料：按高度场保护包络保留目标曲面，最后接原精加工。"""

from __future__ import annotations

from dataclasses import replace
from math import ceil, pi

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.planning.base import PlanningContext, RAPID_FEED_MM_PER_MIN, SAFE_HEIGHT_MM
from toolpath_lab.simulation.stock import stock_spec_for

MAX_LAYERS = 128
MAX_ROUGH_POINTS = 250_000


def roughing_parameters() -> ParameterSet:
    return ParameterSet((
        spec("enabled", "先分层粗加工", K.BOOL, False, group="粗加工",
             help="先逐层清除上方余料，再执行当前选择的精加工策略"),
        spec("depth_mm", "每层切深", K.FLOAT, 2.0, minimum=0.1, maximum=20.0,
             step=0.1, unit="mm", group="粗加工", visible_if={"enabled": "true"},
             help="上限会按教学刀具切削段长度限制；实际切深显示在统计中"),
        spec("allowance_mm", "精加工余量", K.FLOAT, 0.5, minimum=0.0, maximum=5.0,
             step=0.1, unit="mm", group="粗加工", visible_if={"enabled": "true"},
             help="粗加工保留的竖直余量；曲面保护包络也会保留部分侧向余料"),
    ))


def _protective_heights(context: PlanningContext, points_xy: np.ndarray, allowance: float) -> np.ndarray:
    """按当前圆形削料足迹采样邻域最高点，避免中心低处一刀削掉旁边凸起。"""

    radius = context.tool.radius_mm
    probes = [np.zeros(2)]
    for scale, count in ((0.5, 12), (1.0, 24)):
        for angle in np.linspace(0.0, 2.0 * pi, count, endpoint=False):
            probes.append(radius * scale * np.array([np.cos(angle), np.sin(angle)]))
    offsets = np.asarray(probes)
    heights = context.surface.height_at((points_xy[:, None, :] + offsets[None, :, :]).reshape(-1, 2))
    return heights.reshape(len(points_xy), len(offsets)).max(axis=1) + allowance


def prepend_roughing(context: PlanningContext, finish: Toolpath, settings: dict) -> Toolpath:
    """默认关闭；开启时前置三轴分层栅格，不改变精加工点和目标曲面。"""

    if not settings["enabled"]:
        return finish
    # 延迟导入已注册策略的几何助手，不改变 planning 包的策略注册顺序。
    from toolpath_lab.planning.crosshatch import _passes_for_direction
    requested_depth = float(settings["depth_mm"])
    allowance = float(settings["allowance_mm"])
    # 留出刃长余量，防止下一刀进入尚未加工的一侧时银色刀身碰到上一层顶面。
    depth = min(requested_depth, context.tool.cutting_length_mm * 0.4)
    if depth < requested_depth - 1e-9:
        context.warn(f"粗加工每层切深由 {requested_depth:g} mm 限制到 {depth:g} mm，以保留刀身间隙")
    stock = stock_spec_for(context.region, context.surface, context.tool)
    bottom = context.surface.height_bounds()[0] + allowance
    span = max(stock.initial_top_z_mm - bottom, 0.0)
    count = int(ceil(span / depth - 1e-9))
    if count > MAX_LAYERS:
        raise PlanningError(f"粗加工需要 {count} 层，超过 {MAX_LAYERS} 层上限；请增大切深/刀具尺寸或减小曲面高度范围")
    if not count:
        context.warn("精加工余量不小于毛坯余料，未添加粗加工层；请减小精加工余量")
        return replace(finish, metadata={**finish.metadata, "roughing": {
            "enabled": True, "layer_count": 0, "depth_mm": depth, "allowance_mm": allowance,
            "pass_count": 0, "finish_start_move_index": 0, "layers": [],
        }})
    levels = [max(stock.initial_top_z_mm - depth * (i + 1), bottom) for i in range(count)]
    direction = float(context.parameters.get("direction_deg", 0.0))
    # 明显小于刀具直径的步距，覆盖剩余毛坯；粗加工不沿用可能过大的精加工切宽。
    stepover = min(float(context.parameters.get("stepover_mm", context.tool.diameter_mm)),
                   context.tool.diameter_mm * 0.35)
    spacing = max(0.25, min(float(context.surface.sampling_spacing_mm), stock.resolution_mm / 2))
    patterns = []
    point_count = 0
    for index, (start, end, level, frame) in enumerate(_passes_for_direction(
        context.boundary, direction_deg=direction, stepover=stepover, offset=0.0,
    )):
        ends = np.array([[end, level], [start, level]] if index % 2 else [[start, level], [end, level]]) @ frame.T
        samples = max(1, int(ceil(np.linalg.norm(ends[1] - ends[0]) / spacing)))
        point_count += samples + 1
        if point_count * max(count, 1) > MAX_ROUGH_POINTS:
            raise PlanningError("粗加工刀点过多；请减小区域/曲面高度范围或使用更大的刀具")
        xy = np.linspace(ends[0], ends[1], samples + 1)
        patterns.append((xy, _protective_heights(context, xy, allowance)))
    if not patterns:
        raise PlanningError("粗加工区域没有有效扫描线")
    safe_z = max(context.safe_z_mm, stock.initial_top_z_mm + SAFE_HEIGHT_MM,
                 context.surface.height_bounds()[1] + allowance + SAFE_HEIGHT_MM)
    feed = context.feed_mm_per_min
    moves: list[Move] = []
    layers = []
    previous = None
    pass_index = 0
    for layer_index, height in enumerate(levels):
        layer_start = len(moves)
        for xy, protective in patterns:
            positions = np.column_stack((xy, np.maximum(height, protective)))
            above = np.array([positions[0, 0], positions[0, 1], safe_z])
            if previous is not None:
                moves.append(retract_move(previous, above, safe_z, RAPID_FEED_MM_PER_MIN))
            # 接近材料时使用切削进给；不把深切入毛坯的进刀标成 G0。
            moves.append(Move(MoveKind.CUT, np.vstack((above, positions[0])), feed,
                              label=f"粗加工第 {layer_index + 1} 层进刀", preserve_vertices=True))
            moves.append(Move(MoveKind.CUT, positions, feed, pass_index=pass_index,
                              label=f"粗加工第 {layer_index + 1} 层 · 第 {pass_index + 1} 刀",
                              preserve_vertices=True))
            previous = positions[-1]
            pass_index += 1
        layers.append({"index": layer_index + 1, "z_mm": float(height),
                       "start_move_index": layer_start, "end_move_index": len(moves) - 1})
    if previous is not None:
        first = finish.moves[0]
        transfer = retract_move(previous, first.points[0], safe_z, RAPID_FEED_MM_PER_MIN,
                                end_tool_axis=first.tool_axes[0])
        moves.append(replace(transfer, angular_speed_deg_s=first.angular_speed_deg_s))
    finish_start = len(moves)
    moves.extend(replace(move, pass_index=move.pass_index + pass_index if move.pass_index >= 0 else -1)
                 for move in finish.moves)
    context.warn("分层粗加工是高度场教学近似，不能保证所有曲面的刀身避碰；请保留碰撞检测进行验证")
    metadata = dict(finish.metadata)
    metadata["roughing"] = {
        "enabled": True, "layer_count": count, "requested_depth_mm": requested_depth,
        "depth_mm": depth, "allowance_mm": allowance, "stepover_mm": stepover,
        "pass_count": pass_index, "finish_start_move_index": finish_start, "layers": layers,
        "initial_top_z_mm": stock.initial_top_z_mm,
    }
    return Toolpath(tuple(moves), finish.planner, "分层粗加工 + " + finish.planner_label,
                    (f"先分层粗加工：{count} 层，每层至多 {depth:g} mm，精加工余量 {allowance:g} mm",
                     "粗加工竖直刀轴、受控进给下刀、刀间安全抬刀；随后执行原精加工") + finish.notes,
                    metadata)
