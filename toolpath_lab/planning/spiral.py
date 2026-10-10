"""螺旋铣刀路。

从区域轮廓外缘起刀，沿轮廓一圈圈等距向内收缩，形成一条**连续不断刀**的螺旋线，
走到中心残料圆（如果开了 `allow_leave_pill`）或材料用尽为止。

和栅格刀路对比：

===========  ==========================  =========================
             栅格（raster）              螺旋（spiral）
===========  ==========================  =========================
刀路形态     互不相连的平行直线          一条连续的螺旋线
进出刀       每刀都要重新下刀            全程只有一次下刀、一次抬刀
方向一致性   往复模式奇偶刀反向          全程同一个绕向
表面质量     相邻刀轨首尾相接有搭接痕    刀轨之间天然相切，过渡平顺
===========  ==========================  =========================

代价是：中心区域会留下一小块残料（除非关掉"留残料圆"，让刀路一直收到材料用尽），
而且螺旋的走刀方向是固定的单一绕向，不能像往复那样在相邻刀轨间反向。

实现上直接复用 `geometry2d.offset_polygon` 做逐圈偏置，把每一圈的起点与上一圈的终点
用一段"连接"运动衔接——因为两圈之间只差一个切宽，连接段极短，不会明显多切一刀。
进给速度可以随半径线性变化：外圈走快、内圈走慢，这样径向切深才是恒定的。
"""

from __future__ import annotations

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
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import (
    collect_rings,
    resample_ring,
)
from toolpath_lab.planning.registry import PLANNERS

#: 起始圈内缩再让开一点，避免刀正好压在轮廓上打滑。
_EPS = 1e-9


@PLANNERS.register
class SpiralPlanner(Planner):
    """沿区域轮廓连续向内收缩的螺旋刀路。"""

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋铣"
    description: ClassVar[str] = "从轮廓起刀连续向内收缩，全程不断刀；进给可随半径线性变化"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两圈轮廓的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="沿螺旋线取点的间距"),
            spec("direction", "旋向", K.CHOICE, "ccw", group="刀路", choices=(
                Choice("ccw", "逆时针 CCW"),
                Choice("cw", "顺时针 CW"),
            ), help="螺旋的绕行方向；全刀路保持一致"),
            spec("allow_leave_pill", "中心留残料圆", K.BOOL, False, group="刀路",
                 help="勾选则留一个残料圆不切到中心；不勾选则一直切到材料用尽"),
            spec("feed_mm_per_min", "外圈进给 F", K.FLOAT, 800.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路",
                 help="螺旋起始处的进给速度"),
            spec("ramp_feed", "进给随半径变化", K.BOOL, True, group="刀路",
                 help="勾选则内圈按半径线性减速到「中心进给」，径向切深恒定"),
            spec("center_feed_mm_per_min", "中心进给 F′", K.FLOAT, 300.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路",
                 help="仅在勾选「进给随半径变化」时生效",
                 visible_if={"ramp_feed": "true"}),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        direction = str(context.parameters["direction"])
        leave_pill = bool(context.parameters["allow_leave_pill"])
        ramp = bool(context.parameters["ramp_feed"])
        outer_feed = self.require_positive(
            float(context.parameters["feed_mm_per_min"]), "外圈进给 feed_mm_per_min"
        )
        inner_feed = float(context.parameters["center_feed_mm_per_min"])
        if not ramp:
            inner_feed = outer_feed
        else:
            self.require_positive(inner_feed, "中心进给 center_feed_mm_per_min")
            if inner_feed > outer_feed:
                context.warn(
                    f"中心进给 {inner_feed:g} 高于外圈进给 {outer_feed:g}，"
                    "将变成越往内越快"
                )

        rings, exhausted_at_corner = self._rings(context, stepover, leave_pill)
        if not rings:
            raise PlanningError(
                "螺旋铣没有生成任何刀轨：刀具直径相对区域尺寸太大，"
                "请减小刀具直径或扩大区域"
            )
        if exhausted_at_corner:
            context.warn(
                "螺旋在收完一圈之后就停了：再往内偏置时多边形的圆角处会自交而无法生成。"
                "中心这块材料没有被切到——可以减小切宽、或换用栅格刀路补切"
            )

        # 偏置得到的环是逆时针的。闭合环反序遍历（[::-1]）得到的仍是同一条环，
        # 只是绕向相反——首点会变成原来的末点，两者是同一个位置，因此不需要重新接点。
        reverse = direction == "cw"
        # 各圈的半径，用来插值进给。
        outer_radius = self._mean_radius(rings[0])
        inner_radius = self._mean_radius(rings[-1])

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
                # 上一圈终点 -> 这一圈起点：只差一个切宽，用连接进给连过去。
                moves.append(context.link_move(previous, positions[0]))

            radius = self._mean_radius(ring)
            moves.append(
                Move(
                    MoveKind.CUT,
                    positions,
                    self._feed_at(radius, outer_radius, inner_radius, outer_feed, inner_feed),
                    pass_index=index,
                    label=f"第 {index + 1} 圈",
                )
            )
            previous = positions[-1]
        moves.append(context.retract_move_up(previous))

        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(context, len(rings), stepover, ramp, outer_feed, inner_feed),
        )

    # -- 内部步骤 ----------------------------------------------------------
    def _rings(
        self, context: PlanningContext, stepover: float, leave_pill: bool
    ) -> tuple[list[NDArray[np.float64]], bool]:
        return collect_rings(
            context.boundary,
            context.boundary_offset_mm,
            stepover,
            max_rings=2 if leave_pill else None,
        )

    @staticmethod
    def _mean_radius(ring: NDArray[np.float64]) -> float:
        return float(np.linalg.norm(ring, axis=1).mean())

    @staticmethod
    def _feed_at(
        radius: float,
        outer_radius: float,
        inner_radius: float,
        outer_feed: float,
        inner_feed: float,
    ) -> float:
        """按当前圈半径在线性区间上插值进给。"""

        span = outer_radius - inner_radius
        if span <= _EPS:
            return outer_feed
        t = (outer_radius - radius) / span
        return float(outer_feed + (inner_feed - outer_feed) * t)

    @staticmethod
    def _notes(
        context: PlanningContext,
        ring_count: int,
        stepover: float,
        ramp: bool,
        outer_feed: float,
        inner_feed: float,
    ) -> tuple[str, ...]:
        direction = "逆时针 CCW" if context.parameters["direction"] == "ccw" else "顺时针 CW"
        feed_note = (
            f"进给由外圈 {outer_feed:g} 线性降到中心 {inner_feed:g} mm/min（径向切深恒定）"
            if ramp
            else f"进给固定 {outer_feed:g} mm/min"
        )
        return (
            f"螺旋铣：共 {ring_count} 圈，切宽 {stepover:g} mm，{direction}，全程一次下刀",
            f"{feed_note}；边界偏置 {context.boundary_offset_mm:g} mm；"
            f"安全高度 {context.safe_height_mm:g} mm",
        )
