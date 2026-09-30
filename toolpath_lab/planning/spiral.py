"""螺旋刀路（等距螺旋）。

与"环切"的区别：环切是**一圈一条闭合环**、环与环之间用连接段过渡；螺旋把整个区域
切成**一条连续曲线**——从回转中心出发，每转一圈半径增加约一个切宽，直到贴住边界。
少了环间的抬刀/连接，切削是连续的，所以更适合圆形类回转区域的精加工。

实现要点（这也是"如何写第二个策略"的范例）：

1. 先把轮廓向内偏置一个刀具足迹半径，得到**刀心可行区域**；刀心落在里面，
   刀就不会切出轮廓，这一步和环切共用 planning/offset.py；
2. 取可行区域的面积质心作为回转中心；
3. 从中心沿各个角度打射线，得到该方向"离边界还有多远" ρ(θ)；
4. 令 r(θ) = ρ(θ) · s(θ)，其中 s 从 0 线性升到 1、总转角 Θ = 2π·圈数。
   因为 0 <= s <= 1，所以 r <= ρ 恒成立 —— 整条螺旋都自然落在可行区域内部；
5. 按弧长重采样成折线，输出**一段**切削进给，再补上首尾的下刀与抬刀。

`圈数 = ρ 的平均值 / 切宽`，于是相邻两圈的径向间距约为一个切宽 ae。
区域越接近回转体（ρ 沿周向越均匀），等距性越好；方形、椭圆也可以用，只是圈间距会
沿周向略变，实现里会给出提醒。
"""

from __future__ import annotations

from math import ceil, pi
from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import polygon_centroid, ray_boundary_radius
from toolpath_lab.planning.offset import offset_polygon, resample_polyline
from toolpath_lab.planning.registry import PLANNERS

#: 测量 ρ(θ) 的角度分辨率：720 段 ≈ 每 0.5° 一条射线。
_ANGULAR_SAMPLES = 720
#: 螺旋本身每圈取多少个采样点（1° 一个）。
_SAMPLES_PER_TURN = 360
#: 采样数上限，防止极细切宽把内存撑爆；弧长重采样还会再降一次。
_MAX_SAMPLES = 200_000
#: ρ_max / ρ_min 超过它就提醒"区域不够圆，圈间距会不均"。
_CIRCULARITY_WARN = 2.0
#: 圈数超过它就提醒"刀路很长"。
_TURNS_WARN = 200


@PLANNERS.register
class SpiralPlanner(Planner):
    """从内向外（或从外向内）的等距螺旋。"""

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋刀路"
    description: ClassVar[str] = "一条连续等距螺旋，从回转中心向外（或反向）盘出，适合圆形类区域"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("direction", "螺旋走向", K.CHOICE, "outward", group="刀路", choices=(
                Choice("outward", "从内向外 Outward"),
                Choice("inward", "从外向内 Inward"),
            ), help="从中心盘出，还是从边界盘入；两者的刀轨相同、走刀顺序相反"),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两圈的径向间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="相邻两个刀点的弧长间距"),
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
        outward = str(context.parameters["direction"]) == "outward"
        self._warn_if_stepover_too_large(context, stepover)

        offset = context.tool.footprint_radius_mm
        tool_area = offset_polygon(context.boundary, offset)
        if tool_area is None:
            raise PlanningError(
                f"刀具足迹半径 {offset:g} mm 相对区域过大，向内偏置后已没有可加工区域"
            )

        center = polygon_centroid(tool_area)
        angles = np.linspace(0.0, 2.0 * pi, _ANGULAR_SAMPLES + 1)
        radius = ray_boundary_radius(tool_area, center, angles)
        if not np.all(np.isfinite(radius)):
            raise PlanningError(
                "区域相对回转中心不是星形（存在射线打不到边界），无法生成螺旋；"
                "请改用栅格刀路"
            )
        radius_min = float(radius.min())
        radius_max = float(radius.max())
        radius_mean = float(radius.mean())
        if radius_mean <= 0.0:
            raise PlanningError("偏置后区域的回转半径为零，无法生成螺旋")
        if radius_max > _CIRCULARITY_WARN * max(radius_min, 1e-9):
            context.warn(
                f"区域相对回转中心不够对称（半径 {radius_min:.1f}~{radius_max:.1f} mm），"
                "螺旋的圈间距会沿周向变化；圆形区域的等距性最好"
            )

        turns = max(1.0, float(ceil(radius_mean / stepover)))
        if turns > _TURNS_WARN:
            context.warn(f"螺旋约 {turns:g} 圈，刀路很长，建议增大切宽或缩小区域")

        theta_total = 2.0 * pi * turns
        sample_count = int(
            min(_MAX_SAMPLES, max(2, ceil(turns * _SAMPLES_PER_TURN) + 1))
        )
        theta = np.linspace(0.0, theta_total, sample_count)
        # r(θ) = ρ(θ) · s(θ)：ρ 是周期性的边界半径，s 从 0 线性升到 1。
        radial = np.interp(np.mod(theta, 2.0 * pi), angles, radius)
        scale = theta / theta_total
        directions = np.column_stack((np.cos(theta), np.sin(theta)))
        points = center[None, :] + (radial * scale)[:, None] * directions
        if not outward:
            points = points[::-1]

        sampled = resample_polyline(points, sample_step)
        if sampled.shape[0] < 2:
            raise PlanningError("螺旋采样后点数不足，请增大切宽或减小采样步长")

        positions = context.to_positions(sampled)
        cut_length = float(np.linalg.norm(np.diff(positions, axis=0), axis=1).sum())
        moves = (
            context.approach_move_down(positions[0]),
            context.cut_move(
                sampled, pass_index=0,
                label=f"{'从内向外' if outward else '从外向内'}螺旋",
            ),
            context.retract_move_up(positions[-1]),
        )
        return Toolpath(
            moves=moves,
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"螺旋走刀（{'从内向外' if outward else '从外向内'}），约 {turns:g} 圈，"
                f"切宽 {stepover:g} mm，刀心轨迹 {cut_length:.0f} mm",
                f"边界内缩一个刀具半径（本刀 R{offset:g} mm），"
                "安全高度 5 mm、快移 5000 mm/min 为固定值",
            ),
        )

    # -- 内部步骤 ----------------------------------------------------------
    @staticmethod
    def _warn_if_stepover_too_large(context: PlanningContext, stepover: float) -> None:
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，"
                "相邻两圈之间会留下未切除的残余"
            )
