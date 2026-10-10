"""螺旋刀路（新增策略）。

栅格刀路的路网是"平行线"，环切（contour 示例插件）是"一圈一圈、圈间用连接进给"。本策略
补上第三种路网形态——**连续渐近的螺旋**，它的价值很直白：

- 全路径只有一个切入点、没有抬刀，切削力与热负载平稳，特别适合精加工与薄壁件；
- 圆形区域内它是真正的阿基米德螺线，任意两圈的间距都等于切宽（等切削负载）；
- 因为不需要在圈与圈之间停下来重新切入，空行程与接刀痕都比环切少。

两种分支
--------
- **圆形区域**：解析式阿基米德螺线 ``r(θ) = pitch·θ/(2π)``，单段切削、零快移；
  pitch = r_max / n，n = ceil(r_max / 切宽)，因此实际切宽 **不会超过**设定值。
- **其他形状**：等距偏置环 + 圈间切向连接（不抬刀）。多边形内偏置在窄颈处会断开，
  与 contour 示例插件同样的限制，已在文档中说明。

参数
----
stepover_mm   切宽 ae：相邻两圈的间距上限（圆形区域为实际间距）
sample_step_mm 采样步长：沿路径离散的弦长上限
direction      旋向：逆时针（顺铣）/ 顺时针（逆铣）
feed_mm_per_min 切削进给
"""

from __future__ import annotations

from math import ceil, cos, pi, sin
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import CircleRegion
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import offset_polygon, resample_ring, signed_area
from toolpath_lab.planning.registry import PLANNERS

#: 环面积小于该值时认为已经缩到中心，停止布环。
_MIN_RING_AREA_MM2 = 0.5
#: 圆形区域螺旋的最小离散点数。
_MIN_SPIRAL_SAMPLES = 24


@PLANNERS.register
class SpiralPlanner(Planner):
    """由内向外连续渐近的螺旋刀路。"""

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋刀路"
    description: ClassVar[str] = (
        "由内向外连续渐近：圆形区域为阿基米德螺线（单段切削、零抬刀），其他形状为等距环 + 切向连接"
    )
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路",
                 help="相邻两圈的间距上限；圆形区域的实际间距不会超过它"),
            spec("direction", "旋向", K.CHOICE, "ccw", group="刀路", choices=(
                Choice("ccw", "逆时针 CCW（顺铣）"),
                Choice("cw", "顺时针 CW（逆铣）"),
            )),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路"),
            spec("finish_pass", "外圈清边", K.BOOL, True, group="刀路",
                 help="螺线到达最大半径后再补一整圈：单条螺线的最后一圈半径是渐变的，"
                      "不补这一圈时靠外的环带会漏切（材料切除仿真可以直接看到）"),
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
        sign = 1.0 if str(context.parameters["direction"]) == "ccw" else -1.0

        if isinstance(context.region, CircleRegion):
            return self._plan_circle(context, stepover, sample_step, sign)
        return self._plan_rings(context, stepover, sample_step, sign)

    # -- 圆形：解析阿基米德螺线 --------------------------------------------
    def _plan_circle(
        self, context: PlanningContext, stepover: float, sample_step: float, sign: float
    ) -> Toolpath:
        outer_radius = float(context.region.diameter_mm) / 2.0
        offset = context.tool.footprint_radius_mm
        r_max = outer_radius - offset
        if r_max <= 1e-6:
            raise PlanningError(
                f"刀具足迹半径 {offset:g} mm 已经覆盖整个 Ø{outer_radius * 2:g} mm 圆形区域，"
                "无法生成螺旋刀路"
            )

        turns = max(1, int(ceil(r_max / stepover - 1e-9)))
        pitch = r_max / turns
        theta_max = 2.0 * pi * turns
        finish = bool(context.parameters.get("finish_pass", True))
        # 螺线本身的最后一圈半径是渐变的：只有在一个方位角上真正到达 r_max，
        # 因此靠外的环带会漏切。补一整圈（半径恒为 r_max）把边界切净——见 notes。
        tail = 2.0 * pi if finish else 0.0
        # 螺线长度 L ≈ pitch·θ²/(4π)，外圈整圆另算；据此给出满足弦长限制的采样数。
        approx_length = pitch * theta_max * theta_max / (4.0 * pi) + r_max * tail
        samples = max(_MIN_SPIRAL_SAMPLES, int(ceil(approx_length / sample_step)) + 1)
        theta = np.linspace(0.0, theta_max + tail, samples, dtype=np.float64)
        radii = np.where(theta <= theta_max, pitch * theta / (2.0 * pi), r_max)
        angles = sign * theta
        planar = np.column_stack((radii * np.cos(angles), radii * np.sin(angles)))

        positions = context.to_positions(planar)
        moves = [
            context.approach_move_down(positions[0]),
            Move(MoveKind.CUT, positions, context.feed_mm_per_min, pass_index=0,
                 label="螺旋切削"),
            context.retract_move_up(positions[-1]),
        ]
        notes = [
            f"阿基米德螺线：{turns} 圈，实际切宽 {pitch:g} mm（≤ 设定 {stepover:g} mm），"
            f"由内向外{'逆' if sign > 0 else '顺'}时针",
            "单段切削、无抬刀——这是螺旋刀路相对往复/环切最大的区别",
        ]
        if finish:
            notes.append("末段补一整圈外圈清边（半径恒为 r_max）：单条螺线的最后一圈半径渐变，"
                         "不补会漏切最外环带")
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=tuple(notes),
        )

    # -- 其他形状：等距环 + 切向连接 ---------------------------------------
    def _plan_rings(
        self, context: PlanningContext, stepover: float, sample_step: float, sign: float
    ) -> Toolpath:
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
            raise PlanningError(
                f"刀具足迹半径 {context.tool.footprint_radius_mm:g} mm 相对区域过大，"
                "无法生成螺旋（环切）刀路"
            )

        moves: list[Move] = []
        previous: np.ndarray | None = None
        for index, ring in enumerate(rings):
            sampled = resample_ring(ring, sample_step)
            if (index % 2 == 1) == (sign < 0.0):
                sampled = sampled[::-1]
            positions = context.to_positions(np.vstack([sampled, sampled[:1]]))
            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            else:
                moves.append(context.link_move(previous, positions[0]))
            moves.append(
                Move(MoveKind.CUT, positions, context.feed_mm_per_min,
                     pass_index=index, label=f"第 {index + 1} 圈")
            )
            previous = positions[-1]
        moves.append(context.retract_move_up(previous))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"非圆区域走等距环分支：{len(rings)} 圈，切宽 {stepover:g} mm，圈间不抬刀",
                "圆形区域可获得解析螺线（单段切削）；多边形窄颈处偏置环会提前结束",
            ),
        )
