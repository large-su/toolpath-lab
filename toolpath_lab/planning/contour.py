"""环切（等距轮廓）刀路。

从区域轮廓起刀，沿轮廓逐圈**等距向内偏置**，一圈一刀。与螺旋铣的区别：

===========  ==========================  =========================
             螺旋铣 spiral              环切 contour
===========  ==========================  =========================
圈与圈之间   一条连续曲线，天然相切       各自独立成闭合刀轨
进出刀       全程一次下刀                每刀抬刀到安全面再回来
刀轨重叠     相邻刀轨共享一段圆弧         逐圈等距，不重叠
适用         想让表面连续过渡             想精修一圈轮廓、或留出台阶
===========  ==========================  =========================

几何上与螺旋铣共用 `geometry2d.collect_rings`：同一个"逐圈偏置"的循环，
区别只在于圈与圈之间怎么连接、要不要抬刀。

**当前限制**：`offset_polygon` 每层只返回一条环，因此凹形状偏置到一定深度后会分裂成
多块区域，这里只保留其中面积最大的那块——窄颈（两个凸起之间的细腰）会被跳过。
要覆盖这类区域需要让每层输出多条环（`Toolpath` 的运动段模型本身支持），
改动集中在 `_loops_per_level` 的拆分逻辑。
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import (
    collect_rings,
    resample_ring,
)
from toolpath_lab.planning.registry import PLANNERS


@PLANNERS.register
class ContourPlanner(Planner):
    """沿区域轮廓逐圈等距向内偏置的环切刀路。"""

    id: ClassVar[str] = "contour"
    label: ClassVar[str] = "环切"
    description: ClassVar[str] = "从轮廓逐圈等距向内偏置，每圈一刀；适合精修轮廓或留出台阶"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="沿一圈轮廓取点的间距"),
            spec("link", "刀间连接", K.CHOICE, "rapid", group="刀路", choices=(
                Choice("rapid", "抬刀返回 Rapid"),
                Choice("link", "直接连接 Link"),
            ), help="两刀之间抬到安全面再回来，还是不抬刀直接拉过去"),
            spec("direction", "旋向", K.CHOICE, "ccw", group="刀路", choices=(
                Choice("ccw", "逆时针 CCW"),
                Choice("cw", "顺时针 CW"),
            ), help="绕轮廓的行进方向；与螺旋铣一样全刀路保持一致"),
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
        reverse = str(context.parameters["direction"]) == "cw"
        rapid_between = str(context.parameters["link"]) == "rapid"

        rings, exhausted_at_corner = collect_rings(
            context.boundary,
            context.boundary_offset_mm,
            stepover,
        )
        if not rings:
            raise PlanningError(
                "环切没有生成任何刀轨：刀具直径相对区域尺寸太大，"
                "请减小刀具直径或扩大区域"
            )
        if exhausted_at_corner:
            context.warn(
                "环切在收完一圈之后就停了：再往内偏置时圆角处会自交而无法生成，"
                "中心这块材料没有被切到——可以减小切宽、或换用栅格刀路补切"
            )

        moves: list[Move] = []
        previous: np.ndarray | None = None
        for index, ring in enumerate(rings):
            sampled = resample_ring(ring, sample_step)
            closed = np.vstack([sampled, sampled[:1]])
            if reverse:
                closed = closed[::-1]
            positions = context.to_positions(closed)

            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            else:
                # 与螺旋不同：环切每刀都是一条独立的闭合刀轨，两刀之间默认抬刀，
                # 免得在半空中拉一条长距离的连接刀痕。
                moves.append(
                    context.rapid_between(previous, positions[0])
                    if rapid_between
                    else context.link_move(previous, positions[0])
                )
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
            notes=self._notes(context, len(rings), stepover, reverse, rapid_between),
        )

    @staticmethod
    def _notes(
        context: PlanningContext,
        ring_count: int,
        stepover: float,
        reverse: bool,
        rapid_between: bool,
    ) -> tuple[str, ...]:
        direction = "顺时针 CW" if reverse else "逆时针 CCW"
        link_note = "刀间抬刀返回" if rapid_between else "刀间直接连接"
        return (
            f"环切：共 {ring_count} 环，切宽 {stepover:g} mm，{direction}，{link_note}",
            f"边界偏置 {context.boundary_offset_mm:g} mm；"
            f"安全高度 {context.safe_height_mm:g} mm；"
            "凹形状每层只保留面积最大的一块环，窄颈区域会被跳过",
        )