"""示例插件：环切（等距轮廓）策略。

这是一个完整、可直接使用的策略实现：它只依赖公开接口——等距偏置与重采样来自
planning.geometry2d，因此本文件可以当作编写其它策略的模板。

主程序内置的"跟随周边"（toolpath_lab/planning/follow_periphery.py）是这类策略的正式版，
多了一个"向内 / 向外"的走刀顺序参数；本文件保留一份最小实现，演示"放一个文件就是一个策略"。

**启用方式**（三步）：

1. 把本文件复制到 toolpath_lab/planning/contour.py；
2. 在 toolpath_lab/planning/__init__.py 里加一行：

       from toolpath_lab.planning import contour as _contour  # noqa: F401

3. 重启程序——界面"刀路"分组里就会出现"环切(示例插件)"，参数控件自动生成。

**当前限制**：偏置量超过局部内切半径时，环会断开；本实现每个偏置层只保留一条环，
因此凹形状的窄颈区域会提前结束。需要覆盖这类区域时，可改为每层输出多条环
（Toolpath 的运动段模型本身支持）。
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import offset_polygon, resample_ring, signed_area
from toolpath_lab.planning.registry import PLANNERS

_MIN_RING_AREA_MM2 = 0.5


# --------------------------------------------------------------------------
# 策略本体
# --------------------------------------------------------------------------
@PLANNERS.register
class ContourPlanner(Planner):
    """沿区域轮廓逐圈向内偏置的环切刀路。"""

    id: ClassVar[str] = "contour"
    label: ClassVar[str] = "环切(示例插件)"
    description: ClassVar[str] = "从轮廓逐圈向内偏置，适合圆形类区域；这是「新增一个策略」的完整示例"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路"),
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
            raise ValueError("环切未生成任何刀轨")

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
            notes=(f"环切：共 {len(rings)} 环，切宽 {stepover:g} mm",),
        )


def describe_plugin() -> dict[str, Any]:
    """给好奇的人看的自检信息。"""

    return {"id": ContourPlanner.id, "label": ContourPlanner.label,
            "parameters": [item.key for item in ContourPlanner.parameters]}
