"""跟随周边（等距轮廓环切）刀路。

做法只有三步：

1. 把区域轮廓按刀具足迹半径内缩一圈，得到第一环——刀正好贴住轮廓；
2. 每一环再往里推进一个切宽，直到偏置结果退化为空；此时区域已经被切满；
3. 所有环按同一个绕向走完，环间沿同一条缝用一段切削进给径向过渡、不抬刀。

每环的偏置交给 geometry2d.offset_polygon：凸角保持尖角、凹角补圆弧，
所以方形的环是同心方、圆形的环是同心圆。

两个参数各管一件事，互不影响：

- `direction` 决定**先切哪一环**，两个方向生成的刀轨完全相同：向内（由外向内）第一刀沿轮廓、
  最后一刀落在料心；向外（由内向外）反过来，下刀点在料心、切屑往外排；
- `winding` 决定**每一环沿哪个方向绕**，全套环保持一致：逆时针（默认，与区域轮廓同向）
  或顺时针。参照方向是俯视，也就是从上往下看 XY 平面。

绕向不只是习惯：右旋刀具（主轴 M03，俯视顺时针转）加工内腔时，材料留在刀的左侧才是顺铣，
对应俯视**顺时针**绕行；逆时针绕行是逆铣。

终止条件是"偏置退化"，所以最后一环到中心的余量必然小于一个切宽；切宽大于刀具直径时，
两环之间与中心会留下切不到的区域，这时会返回一条提醒。
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
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.planning.base import (
    LAYER_PARAMETERS,
    SURFACE_PARAMETERS,
    Planner,
    PlanningContext,
)
from toolpath_lab.planning.geometry2d import offset_polygon, resample_ring, signed_area
from toolpath_lab.planning.registry import PLANNERS

#: 环数上限：切宽极小时防止病态输入把规划拖死（正常参数远达不到）。
_MAX_RINGS = 5000
#: 面积小于这个值的环视为已经退化（mm²）。
_MIN_RING_AREA_MM2 = 0.5

_DIRECTION_LABELS = {"inward": "向内（由外向内）", "outward": "向外（由内向外）"}
_WINDING_LABELS = {"ccw": "逆时针", "cw": "顺时针"}


@PLANNERS.register
class FollowPeripheryPlanner(Planner):
    """沿区域轮廓逐圈等距偏置，直到区域切满。"""

    id: ClassVar[str] = "follow_periphery"
    label: ClassVar[str] = "跟随周边"
    description: ClassVar[str] = "沿轮廓逐圈环切：由外向内或由内向外，一圈比一圈靠里，直到切满"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("direction", "走刀顺序", K.CHOICE, "inward", group="刀路", choices=(
                Choice("inward", "向内（由外向内）"),
                Choice("outward", "向外（由内向外）"),
            ), help="两个方向的刀轨完全相同，只是先从轮廓环还是先从最内环下刀"),
            spec("winding", "绕向", K.CHOICE, "ccw", group="刀路", choices=(
                Choice("ccw", "逆时针 CCW"),
                Choice("cw", "顺时针 CW"),
            ), help="俯视（从上往下看）为准，每一环同向绕行；逆时针与区域轮廓同向"),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="轮廓与凹角圆弧的离散精度"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        ) + SURFACE_PARAMETERS + LAYER_PARAMETERS
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        # 先逐层把毛坯铣掉（每层是水平面，环的范围收窄到该层还有料的地方），
        # 最后再沿加工面走一遍——这一遍就是精加工，也是不分层时的全部刀路。
        levels = context.layer_levels()
        moves: list[Move] = []
        index = 0
        produced = 0
        for z in levels:
            level_moves, index = self._ring_moves(context.at_level(z), index)
            if level_moves:
                produced += 1
            moves.extend(level_moves)
        if produced < len(levels):
            context.warn(
                f"有 {len(levels) - produced} 层在那个高度上剩下的料比刀还窄（放不下刀具），已跳过"
            )
        finish_moves, index = self._ring_moves(context, index)
        moves.extend(finish_moves)
        if index == 0:
            raise PlanningError(
                f"刀具足迹半径 {context.tool.footprint_radius_mm:g} mm 相对区域尺寸过大，"
                "跟随周边生成不了任何一环"
            )

        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(context, index, produced),
        )

    # -- 内部步骤 ----------------------------------------------------------
    def _ring_moves(
        self, context: PlanningContext, first_index: int
    ) -> tuple[list[Move], int]:
        """在给定上下文上跑一遍完整的环切（分层时是某一层，最后是沿加工面的一遍）。

        返回（这一段运动, 下一段要用的序号）。这一层没有料（范围退化）时返回空。
        """

        direction = str(context.parameters["direction"])
        winding = str(context.parameters["winding"])
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        self._warn_if_stepover_too_large(context, stepover)

        rings = self._rings(context, stepover)
        if not rings:
            return [], first_index
        if direction == "outward":
            rings.reverse()
        clockwise = winding == "cw"

        moves: list[Move] = []
        previous: NDArray[np.float64] | None = None
        for step, ring in enumerate(rings):
            index = first_index + step
            sampled = resample_ring(ring, sample_step)
            closed = np.vstack([sampled, sampled[:1]])
            # 每一环同向绕行：偏置出来的环本来就是逆时针，要顺时针就把整环反过来。
            # 反过来之后起点仍是同一个顶点，所以环间过渡还是沿同一条缝径向走一个切宽。
            if clockwise:
                closed = closed[::-1]
            positions = context.to_positions(closed)
            if previous is None:
                moves.extend(
                    context.entry_moves(closed[0], closed[1], pass_index=index)
                )
            else:
                moves.append(context.link_move(previous, positions[0]))
            moves.append(
                context.cut_move(closed, pass_index=index, label=f"第 {index + 1} 环")
            )
            previous = positions[-1]
        if previous is not None:
            moves.append(context.retract_move_up(previous))
        return moves, first_index + len(rings)

    @staticmethod
    def _rings(context: PlanningContext, stepover: float) -> list[NDArray[np.float64]]:
        """从刀路范围（斜坡"只加工斜面段"时比轮廓窄）内缩一个足迹半径开始，每环再推进一个切宽。"""

        boundary = context.machining_boundary
        rings: list[NDArray[np.float64]] = []
        if boundary.shape[0] < 3:
            return rings  # 这一层已经没有料了
        distance = context.tool.footprint_radius_mm
        while len(rings) < _MAX_RINGS:
            ring = offset_polygon(boundary, distance)
            if ring is None or abs(signed_area(ring)) < _MIN_RING_AREA_MM2:
                break
            rings.append(ring)
            distance += stepover
        if len(rings) == _MAX_RINGS:
            context.warn(f"环数达到上限 {_MAX_RINGS}，请检查切宽是否过小")
        return rings

    @staticmethod
    def _warn_if_stepover_too_large(context: PlanningContext, stepover: float) -> None:
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，"
                "两环之间与中心会留下未切除的残余"
            )

    @staticmethod
    def _notes(
        context: PlanningContext,
        ring_count: int,
        layer_count: int,
    ) -> tuple[str, ...]:
        direction = str(context.parameters["direction"])
        winding = str(context.parameters["winding"])
        stepover = float(context.parameters["stepover_mm"])
        sample_step = float(context.parameters["sample_step_mm"])
        notes = [
            f"{_DIRECTION_LABELS[direction]}走刀，{_WINDING_LABELS[winding]}绕行，"
            f"共 {ring_count} 环，切宽 {stepover:g} mm，采样步长 {sample_step:g} mm",
            f"边界内缩一个刀具足迹半径（本刀 {context.tool.footprint_radius_mm:g} mm），"
            "安全高度 5 mm、快移 5000 mm/min 为固定值",
        ]
        if layer_count:
            _, high = context.surface_z_range
            notes.append(
                f"分层粗加工：每层 {context.layer_depth_mm:g} mm、共 {layer_count} 层"
                f"（毛坯顶面在 {high + context.stock_margin_mm:g} mm），"
                "每层只切该高度上还有料的范围，最后沿加工面精加工一遍"
            )
        return tuple(notes)
