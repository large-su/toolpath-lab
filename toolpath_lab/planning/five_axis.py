"""五轴曲面刀路。

该策略在现有栅格刀路的 XYZ 接触点上增加刀轴方向：先取曲面法向，再沿
走刀方向和横向切向量施加可调前倾/侧倾角。这样可以在不改变三轴区域裁剪
逻辑的前提下生成连续的 5-axis tool-axis 数据，供播放和 A/B 轴 G-code 导出。
"""

from __future__ import annotations

from dataclasses import replace
from math import radians, tan
from typing import Any, ClassVar, Iterable, Mapping

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import axis_angle_deg, cumulative_lengths, slerp_axis, unit
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.surface import FlatSurface
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.crosshatch import _passes_for_direction
from toolpath_lab.planning.registry import PLANNERS


def _surface_axes(
    context: PlanningContext,
    points_xy: np.ndarray,
    *,
    lead_deg: float,
    side_tilt_deg: float,
) -> np.ndarray:
    """计算每个接触点的刀轴，正方向为刀尖指向刀柄。"""

    points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    normals = context.surface.normal_at(points)
    heights = context.surface.height_at(points)
    tangents = np.empty_like(normals)
    if len(points) == 1:
        tangents[:] = np.array([1.0, 0.0, 0.0])
    else:
        for index in range(len(points)):
            if index == 0:
                delta = points[1] - points[0]
                dz = heights[1] - heights[0]
            elif index == len(points) - 1:
                delta = points[-1] - points[-2]
                dz = heights[-1] - heights[-2]
            else:
                delta = points[index + 1] - points[index - 1]
                dz = heights[index + 1] - heights[index - 1]
            tangent = np.array([delta[0], delta[1], dz], dtype=np.float64)
            tangent = tangent - normals[index] * float(np.dot(tangent, normals[index]))
            tangents[index] = unit(tangent if np.linalg.norm(tangent) > 1e-9 else [1, 0, 0])
    sides = np.cross(normals, tangents)
    sides = sides / np.maximum(np.linalg.norm(sides, axis=1)[:, None], 1e-12)
    lead = tan(radians(lead_deg))
    side_tilt = tan(radians(side_tilt_deg))
    axes = normals + lead * tangents + side_tilt * sides
    return axes / np.maximum(np.linalg.norm(axes, axis=1)[:, None], 1e-12)


def _smooth_axes(points_xy: np.ndarray, axes: np.ndarray, span_mm: float,
                 max_deviation_deg: float) -> np.ndarray:
    """独立于采样密度的对称距离滤波，限制相对原刀轴的偏离。"""

    if len(axes) < 3 or span_mm <= 0.0 or max_deviation_deg <= 0.0:
        return axes.copy()
    distances = cumulative_lengths(points_xy)
    widths = np.maximum(np.gradient(distances), 1e-9)
    radius, sigma = span_mm / 2.0, span_mm / 4.0
    result = []
    for i, distance in enumerate(distances):
        lo = int(np.searchsorted(distances, distance - radius, side="left"))
        hi = int(np.searchsorted(distances, distance + radius, side="right"))
        weights = np.exp(-0.5 * ((distances[lo:hi] - distance) / sigma) ** 2) * widths[lo:hi]
        candidate = np.sum(axes[lo:hi] * weights[:, None], axis=0)
        if np.linalg.norm(candidate) < 1e-9:
            candidate = axes[i]
        candidate = unit(candidate)
        deviation = axis_angle_deg(axes[i], candidate)
        candidate = slerp_axis(axes[i], candidate, min(1.0, max_deviation_deg / max(deviation, 1e-12)))
        # 不允许滤波引入原先不存在的向下刀轴。
        result.append(axes[i] if axes[i, 2] > 0.0 and candidate[2] <= 0.0 else candidate)
    return np.asarray(result)


def _peak_gradient(points: np.ndarray, axes: np.ndarray) -> float:
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    dots = np.sum(axes[:-1] * axes[1:], axis=1)
    angles = np.degrees(np.arccos(np.clip(dots, -1.0, 1.0)))
    return float(np.max(angles / np.maximum(lengths, 1e-9))) if len(lengths) else 0.0


def orient_surface_passes(
    context: PlanningContext,
    passes: Iterable[tuple[np.ndarray, int, str]],
    *,
    planner_id: str,
    planner_label: str,
    notes: tuple[str, ...],
    metadata: Mapping[str, Any] | None = None,
    always_retract: bool = False,
) -> Toolpath:
    """给已采样的名义曲面扫描线赋予五轴姿态；固定步距与自适应策略共用。"""

    lead_deg = float(context.parameters["lead_deg"])
    side_tilt_deg = float(context.parameters["side_tilt_deg"])
    smooth = bool(context.parameters["smooth_orientation"])
    span = float(context.parameters["smoothing_span_mm"])
    deviation_limit = float(context.parameters["max_axis_deviation_deg"])
    speed_limit = float(context.parameters["max_angular_speed_deg_s"])
    if isinstance(context.surface, FlatSurface):
        context.warn("当前是平面加工面，曲面法向处处相同，因此同一刀路的刀具姿态会保持一致；切换到自由曲面可观察连续姿态变化")
    moves: list[Move] = []
    previous: np.ndarray | None = None
    previous_axis: np.ndarray | None = None
    raw_peak, smooth_peak, actual_deviation = 0.0, 0.0, 0.0
    for points_xy, pass_index, label in passes:
        raw_axes = _surface_axes(context, points_xy, lead_deg=lead_deg, side_tilt_deg=side_tilt_deg)
        axes = _smooth_axes(points_xy, raw_axes, span, deviation_limit) if smooth else raw_axes
        raw_peak = max(raw_peak, _peak_gradient(points_xy, raw_axes))
        smooth_peak = max(smooth_peak, _peak_gradient(points_xy, axes))
        actual_deviation = max(actual_deviation, max(axis_angle_deg(a, b) for a, b in zip(raw_axes, axes)))
        positions = context.to_positions(points_xy, tool_axes=axes, compensate_tool=True)
        if previous is None:
            moves.append(context.approach_move_down(positions[0], axes[0]))
        else:
            connector = context.rapid_between if smooth or always_retract else context.link_move
            moves.append(connector(previous, positions[0], previous_axis, axes[0]))
        cut = context.cut_move(points_xy, pass_index=pass_index, label=label, tool_axes=axes,
                               compensate_tool=True, sampled=True)
        moves.append(replace(cut, preserve_vertices=True) if always_retract else cut)
        previous, previous_axis = positions[-1], axes[-1]
    if previous is None or previous_axis is None:
        raise PlanningError("五轴加工没有有效切削刀路")
    moves.append(context.retract_move_up(previous, previous_axis))
    combined_metadata = dict(metadata or {})
    if smooth:
        moves = [replace(move, angular_speed_deg_s=speed_limit, preserve_vertices=True) for move in moves]
        limited_segments, peak_speed = 0, 0.0
        for move in moves:
            linear_times = np.linalg.norm(np.diff(move.points, axis=0), axis=1) / move.feed_mm_per_min * 60.0
            times = move.segment_durations_s
            limited_segments += int(np.count_nonzero(times > linear_times + 1e-8))
            for a, b, time in zip(move.tool_axes[:-1], move.tool_axes[1:], times):
                if time > 1e-9:
                    peak_speed = max(peak_speed, axis_angle_deg(a, b) / time)
        combined_metadata["orientation_smoothing"] = {
            "enabled": True, "span_mm": span, "max_deviation_deg": deviation_limit,
            "actual_max_deviation_deg": actual_deviation, "max_angular_speed_deg_s": speed_limit,
            "peak_angular_speed_deg_s": peak_speed, "limited_segment_count": limited_segments,
            "raw_peak_gradient_deg_mm": raw_peak, "smoothed_peak_gradient_deg_mm": smooth_peak,
        }
        context.warn("姿态平滑仅限制教学刀轴的偏差和仿真角速度，不等同机床旋转轴限位或自动避碰；请保留刀身检测")
    return Toolpath(tuple(moves), planner_id, planner_label, notes + (
        f"刀轴沿曲面法向，前倾 {lead_deg:g}°，侧倾 {side_tilt_deg:g}°",
        "自由曲面按逐刀点重算法向；平底/圆鼻刀按局部倾角增加保守接触间隙",
        "刀轴姿态以 A=方位角、B=倾角形式导出；请在仿真或机床后处理器中校验轴限位",
    ) + ((f"姿态平滑范围 {span:g} mm，最大偏差 {deviation_limit:g}°，仿真刀轴角速度不超过 {speed_limit:g}°/s；换行先抬刀转向",) if smooth else ()), combined_metadata)


@PLANNERS.register
class FiveAxisPlanner(Planner):
    """带前倾/侧倾刀轴姿态的往复栅格五轴策略。"""

    id: ClassVar[str] = "five_axis"
    label: ClassVar[str] = "五轴曲面刀路"
    description: ClassVar[str] = "沿曲面法向并叠加前倾/侧倾角，输出连续刀轴姿态"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="五轴刀路"),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="五轴刀路"),
            spec("lead_deg", "前倾角", K.FLOAT, 12.0, minimum=-45.0, maximum=45.0,
                 step=1.0, unit="°", group="五轴刀路",
                 help="沿走刀方向倾斜刀轴，正值朝走刀方向前倾"),
            spec("side_tilt_deg", "侧倾角", K.FLOAT, 0.0, minimum=-30.0, maximum=30.0,
                 step=1.0, unit="°", group="五轴刀路",
                 help="沿扫描线法向侧倾刀轴"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 500.0,
                 minimum=10.0, maximum=10000.0, step=50.0, unit="mm/min", group="五轴刀路"),
            spec("smooth_orientation", "五轴姿态平滑", K.BOOL, True, group="姿态平滑",
                 help="距离加权平滑刀轴，限制偏离原姿态，并在安全高度转向；关闭可对比原始刀轴"),
            spec("smoothing_span_mm", "平滑范围", K.FLOAT, 6.0, minimum=0.5, maximum=30.0,
                 step=0.5, unit="mm", group="姿态平滑", visible_if={"smooth_orientation": "true"},
                 help="每个刀点周围参与滤波的走刀距离；越大越平缓，但更偏离原姿态"),
            spec("max_axis_deviation_deg", "最大姿态偏差", K.FLOAT, 5.0, minimum=0.0, maximum=20.0,
                 step=0.5, unit="°", group="姿态平滑", visible_if={"smooth_orientation": "true"},
                 help="平滑刀轴相对原法向+前倾/侧倾刀轴的最大夹角；0 表示不改变切削刀轴"),
            spec("max_angular_speed_deg_s", "刀轴角速度上限", K.FLOAT, 30.0, minimum=1.0, maximum=180.0,
                 step=1.0, unit="°/s", group="姿态平滑", visible_if={"smooth_orientation": "true"},
                 help="仿真中刀轴转向过快时延长该小段时间；不是实际机床 A/B 轴速度限制"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(float(context.parameters["stepover_mm"]), "切宽 stepover_mm")
        direction = float(context.parameters["direction_deg"])
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，两刀之间会留下未切除的残余"
            )

        passes = _passes_for_direction(
            context.boundary,
            direction_deg=direction,
            stepover=stepover,
            offset=context.tool.footprint_radius_mm,
        )
        if not passes:
            raise PlanningError("五轴刀路没有生成有效扫描线，请检查区域和刀具尺寸")

        sampled_passes = []
        for index, (start, end, level, frame) in enumerate(passes):
            reverse = index % 2 == 1
            planar = np.array(
                [[end, level], [start, level]] if reverse else [[start, level], [end, level]],
                dtype=np.float64,
            )
            points_xy = planar @ frame.T
            # 先把扫描线加密，再逐刀点重算曲面法向和刀轴，避免整条刀路
            # 只使用起点/终点姿态而在中间“保持不变”。
            points_xy = context.sample_cut_points(points_xy)
            sampled_passes.append((points_xy, index, f"五轴第 {index + 1} 刀"))
        return orient_surface_passes(context, sampled_passes, planner_id=self.id, planner_label=self.label,
                                     notes=(f"五轴栅格：{len(passes)} 刀，切宽 {stepover:g} mm，走刀方向 {direction:g}°",))
