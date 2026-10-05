"""螺旋刀路策略。

从靠近区域中心处起刀，半径按切宽线性增大，一路连续螺旋向外，直到逼近
区域边界。区域以原点为中心，因此任意角度上"边界允许的最大半径"可用
射线与边界多边形求交得到：

1. 从原点沿角度 theta 发射射线，与边界各边求交，取最远交点得 R(θ)；
2. 允许半径 r_allow(θ) = R(θ) - 刀具足迹半径；
3. 螺旋半径 r(θ) = r_start + stepover · θ / 2π，逐步外扩；
4. 任一角度上 r 超过 r_allow 时螺旋终止——外圈自然贴合边界形状，
   方形区域会得到"圆角方形螺旋"，圆形区域则是标准阿基米德螺旋。

与环切的关系：环切是一圈圈独立的同心环（环间需要连接段），
本策略把同样的径向增量连成一条连续刀路，空行程更少、切削更连贯。
"""

from __future__ import annotations

from math import ceil, cos, pi, sin
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.registry import PLANNERS

_EPS = 1e-9


def _max_radius_along_ray(
    polygon: NDArray[np.float64], cos_t: float, sin_t: float
) -> float:
    """从原点沿方向 (cos_t, sin_t) 的射线与多边形最远交点的半径。

    区域以原点为中心且包含原点，因此射线必与边界相交；返回 -1 表示退化。
    """

    best = -1.0
    count = polygon.shape[0]
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        dx, dy = bx - ax, by - ay
        # 解 origin + t·d = a + s·edge，即 t·d - s·edge = a
        det = cos_t * (-dy) - sin_t * (-dx)
        if abs(det) <= _EPS:
            continue
        t = (ax * (-dy) - ay * (-dx)) / det
        s = (cos_t * ay - sin_t * ax) / det
        if t > _EPS and -_EPS <= s <= 1.0 + _EPS and t > best:
            best = t
    return best


@PLANNERS.register
class SpiralPlanner(Planner):
    """从中心向外的连续螺旋刀路。"""

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋"
    description: ClassVar[str] = "从中心连续向外螺旋，一刀走完，空行程少，适合圆形与方形区域"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两圈螺旋的径向间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="沿螺旋线的点间距"),
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
        footprint = context.tool.footprint_radius_mm

        # 起始半径：刀具足迹半径（中心留出刀具能落下的空当）
        r_start = max(footprint, stepover * 0.5)

        # 预计算角度 → 允许半径 的查找表（角度细化，插值取值）
        table_size = 720
        angles = np.linspace(0.0, 2.0 * pi, table_size, endpoint=False)
        r_allow_table = np.empty(table_size, dtype=np.float64)
        for i, theta in enumerate(angles):
            r_max = _max_radius_along_ray(boundary, float(cos(theta)), float(sin(theta)))
            r_allow_table[i] = r_max - footprint if r_max > 0.0 else -1.0
        if (r_allow_table <= 0.0).all():
            raise ValueError("螺旋未生成任何刀轨：刀具足迹半径相对区域尺寸过大")

        def r_allow_at(theta: float) -> float:
            pos = (theta / (2.0 * pi)) * table_size
            i0 = int(pos) % table_size
            frac = pos - int(pos)
            v0 = r_allow_table[i0]
            v1 = r_allow_table[(i0 + 1) % table_size]
            if v0 < 0.0 or v1 < 0.0:
                return max(v0, v1)
            return v0 + (v1 - v0) * frac

        r_limit = float(r_allow_table.max())
        if r_limit <= r_start:
            raise ValueError("螺旋未生成任何刀轨：区域过小或刀具过大")

        # 螺旋总圈数与总角度
        turns = (r_limit - r_start) / stepover
        theta_total = turns * 2.0 * pi

        # 沿弧长采样：相邻点弧长约 sample_step_mm
        mean_radius = 0.5 * (r_start + r_limit)
        d_theta = sample_step / max(mean_radius, 1e-6)
        theta_total = min(theta_total, 16.0 * pi * 64)  # 上限保护
        sample_count = max(4, int(ceil(theta_total / d_theta)) + 1)

        points: list[tuple[float, float]] = []
        terminated_at = theta_total
        for k in range(sample_count):
            theta = theta_total * k / (sample_count - 1)
            r_spiral = r_start + stepover * theta / (2.0 * pi)
            r_allowed = r_allow_at(theta)
            if r_allowed < 0.0 or r_spiral > r_allowed + _EPS:
                # 超出边界：若已走出足够长度则停在上一点之外再补一个贴边点
                terminated_at = theta
                break
            points.append((r_spiral * cos(theta), r_spiral * sin(theta)))
        else:
            theta = theta_total
            points.append((r_spiral * cos(theta), r_spiral * sin(theta)))

        if len(points) < 2:
            raise ValueError("螺旋未生成任何刀轨：请检查区域尺寸、刀具直径与切宽")

        # 终点补一个贴边收尾点（沿最后角度收到允许半径），保证区域外圈被切到
        theta_end = terminated_at if terminated_at > 0.0 else theta_total
        r_end_allowed = r_allow_at(theta_end)
        if r_end_allowed > 0.0:
            last = points[-1]
            tail = (r_end_allowed * cos(theta_end), r_end_allowed * sin(theta_end))
            if (tail[0] - last[0]) ** 2 + (tail[1] - last[1]) ** 2 > 1e-6:
                points.append(tail)

        planar = np.array(points, dtype=np.float64)
        positions = context.to_positions(planar)

        moves = [
            context.approach_move_down(positions[0]),
            Move(
                MoveKind.CUT,
                positions,
                context.feed_mm_per_min,
                pass_index=0,
                label="连续螺旋",
            ),
            context.retract_move_up(positions[-1]),
        ]
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"连续螺旋：约 {max(1.0, terminated_at / (2.0 * pi)):.1f} 圈，"
                f"切宽 {stepover:g} mm，外圈贴合区域边界",
                f"边界内缩一个刀具足迹半径（R{footprint:g} mm）",
            ),
        )
