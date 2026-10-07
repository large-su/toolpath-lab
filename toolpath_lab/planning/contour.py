"""环切（等距轮廓）刀路策略。

从区域轮廓开始，逐圈向内做等距偏置，形成一圈一圈的环切刀路——这是型腔粗加工
最典型的策略之一（对应 UG 的 Cavity Mill / Contour 思路）：刀具沿轮廓环行，
每环向内收一个切宽，直到覆盖整个区域。

实现要点（与栅格策略共用同一套基座契约）：

1. 用多边形等距偏置（offset_polygon）从边界逐层生成内环；
2. 每层按采样步长把环重采样成离散点（保证圆弧处刀轨平滑）；
3. 相邻两环交替走行方向（外→内、内→外），减少抬刀；
4. 偏置到环面积过小（刀具已覆盖中心）时停止。

当前限制：每个偏置层只保留一条环，凹形状的窄颈区域会在偏置超过局部内切半径时
提前结束（凸区域——方形、圆形——完全不受影响）。
"""

from __future__ import annotations

from math import atan2, ceil, cos, pi, sin
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area
from toolpath_lab.planning.registry import PLANNERS

_EPS = 1e-9
_MIN_RING_AREA_MM2 = 0.5


# --------------------------------------------------------------------------
# 环切专用的多边形等距偏置几何
# --------------------------------------------------------------------------
def _inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """每条边的单位左法向（逆时针多边形时为内法向）。"""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def _distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)[:, None, :]
    start = polygon[None, :, :]
    end = np.roll(polygon, -1, axis=0)[None, :, :]
    edge = end - start
    squared = np.maximum(np.sum(edge * edge, axis=2), _EPS)
    t = np.clip(np.sum((pts - start) * edge, axis=2) / squared, 0.0, 1.0)
    closest = start + t[..., None] * edge
    return np.linalg.norm(pts - closest, axis=2).min(axis=1)


def offset_polygon(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    max_miter: float = 4.0,
) -> NDArray[np.float64] | None:
    """逆时针多边形向内偏置 distance（负值向外），退化为空时返回 None。

    凸角用斜接（miter）交点，凹角插入圆弧接头——多边形内缩在凹角处本来就是圆弧；
    最后用"到原始边界的距离 >= 偏置量"过滤掉自交产生的顶点。
    """

    poly = ensure_ccw(polygon)
    if abs(distance) <= _EPS:
        return poly

    normals = _inward_normals(poly)
    previous_normal = np.roll(normals, 1, axis=0)
    previous_edge = poly - np.roll(poly, 1, axis=0)
    next_edge = np.roll(poly, -1, axis=0) - poly

    result: list[tuple[float, float]] = []
    for index in range(poly.shape[0]):
        point = poly[index]
        n_prev, n_next = previous_normal[index], normals[index]
        turn = float(
            previous_edge[index, 0] * next_edge[index, 1]
            - previous_edge[index, 1] * next_edge[index, 0]
        )
        if abs(turn) <= _EPS:
            moved = point + distance * n_next
            result.append((float(moved[0]), float(moved[1])))
            continue

        dot = float(np.clip(n_prev @ n_next, -1.0, 1.0))
        if (turn > 0.0) == (distance > 0.0) and dot > -0.999:
            moved = point + distance * (n_prev + n_next) / (1.0 + dot)
            if float(np.linalg.norm(moved - point)) <= max_miter * abs(distance):
                result.append((float(moved[0]), float(moved[1])))
                continue

        start_angle = atan2(float(n_prev[1]), float(n_prev[0]))
        end_angle = atan2(float(n_next[1]), float(n_next[0]))
        delta = (end_angle - start_angle + pi) % (2.0 * pi) - pi
        segments = max(1, int(ceil(abs(delta) * abs(distance) / max(chord_mm, 1e-6))))
        for step in range(segments + 1):
            angle = start_angle + delta * step / segments
            result.append(
                (
                    float(point[0] + distance * cos(angle)),
                    float(point[1] + distance * sin(angle)),
                )
            )

    candidate = np.array(result, dtype=np.float64)
    tolerance = max(1e-6, 1e-6 * abs(distance))
    filtered = candidate[_distance_to_boundary(candidate, poly) >= abs(distance) - tolerance]
    if filtered.shape[0] < 3:
        return None
    gaps = np.linalg.norm(np.diff(np.vstack([filtered, filtered[:1]]), axis=0), axis=1)
    filtered = filtered[np.concatenate(([True], gaps[:-1] > _EPS))]
    if filtered.shape[0] < 3 or signed_area(filtered) <= 0.0:
        return None
    return filtered


def resample_ring(polygon: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """按等弧长重采样一个闭合环（不重复首点）。"""

    ring = np.vstack([polygon, polygon[:1]])
    steps = np.linalg.norm(np.diff(ring, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(steps)))
    total = float(cumulative[-1])
    if total <= _EPS:
        return np.asarray(polygon, dtype=np.float64)
    count = max(3, int(ceil(total / max(step_mm, 1e-6))))
    targets = np.linspace(0.0, total, count, endpoint=False)
    return np.column_stack(
        (
            np.interp(targets, cumulative, ring[:, 0]),
            np.interp(targets, cumulative, ring[:, 1]),
        )
    )


# --------------------------------------------------------------------------
# 策略本体
# --------------------------------------------------------------------------
@PLANNERS.register
class ContourPlanner(Planner):
    """沿区域轮廓逐圈向内偏置的环切刀路。"""

    id: ClassVar[str] = "contour"
    label: ClassVar[str] = "环切刀路"
    description: ClassVar[str] = "从轮廓逐圈向内等距偏置，型腔粗加工典型策略，适合圆形类区域"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="环上离散点的间距，越小刀轨越平滑"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        boundary = context.boundary
        distance = context.tool.footprint_radius_mm

        rings: list[NDArray[np.float64]] = []
        while True:
            ring = offset_polygon(boundary, distance)
            if ring is None or abs(signed_area(ring)) < _MIN_RING_AREA_MM2:
                break
            rings.append(ring)
            distance += stepover
        if not rings:
            context.warn("没有生成任何环：刀具足迹半径相对区域尺寸过大")
            raise PlanningError("环切未生成任何刀轨：请检查区域尺寸与刀具直径是否匹配")

        moves: list[Move] = []
        previous: np.ndarray | None = None
        for index, ring in enumerate(rings):
            sampled = resample_ring(ring, sample_step)
            closed = np.vstack([sampled, sampled[:1]])
            if index % 2 == 1:
                closed = closed[::-1]
            positions = context.to_positions(closed)
            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            else:
                moves.append(context.link_move(previous, positions[0]))
            moves.append(
                Move(
                    MoveKind.CUT,
                    positions,
                    context.feed_mm_per_min,
                    pass_index=index,
                    label=f"第 {index + 1} 环",
                )
            )
            previous = positions[-1]
        moves.append(context.retract_move_up(previous))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(f"环切：共 {len(rings)} 环，切宽 {stepover:g} mm，"
                   f"刀具足迹半径 {context.tool.footprint_radius_mm:g} mm",),
        )
