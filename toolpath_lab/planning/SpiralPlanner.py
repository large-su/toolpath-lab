from __future__ import annotations

from typing import ClassVar

import numpy as np

from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.registry import PLANNERS


@PLANNERS.register
class SpiralPlanner(Planner):
    """从外向内生成一圈一圈螺旋刀路的示例策略。

    这是一个最小可用版本：
    - 取区域的外接圆中心作为螺旋中心；
    - 从最大半径向内按切宽递减；
    - 每一圈都生成一个闭合圆，交替方向，整体看起来像螺旋。

    适合演示接口，便于你后续拓展成更复杂的螺旋、平行线、岛屿规避等策略。
    """

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋(示例)"
    description: ClassVar[str] = "从外向内螺旋走刀"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec(
                "stepover_mm",
                "切宽 ae",
                K.FLOAT,
                6.0,
                minimum=0.5,
                maximum=50.0,
                step=0.5,
                unit="mm",
                group="刀路",
                help="每圈与下一圈之间的半径间距",
            ),
            spec(
                "feed_mm_per_min",
                "进给速度 F",
                K.FLOAT,
                600.0,
                minimum=10.0,
                maximum=10000.0,
                step=50.0,
                unit="mm/min",
                group="刀路",
            ),
            spec(
                "SAFE_HEIGHT_MM",
                "安全高度 H",
                K.FLOAT,
                5.0,
                minimum=0.5,
                maximum=50,
                step=0.5,
                unit="mm",
                group="刀路",
            ),
            spec("RAPID_FEED_MM_PER_MIN", "快速进给速度 V", K.FLOAT, 5000.0, minimum=100.0,
                 maximum=50000.0, step=100.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )

        boundary = context.boundary
        if boundary.shape[0] < 3:
            raise ValueError("区域轮廓点数不足，无法生成螺旋刀路")

        center = boundary.mean(axis=0)
        radii = np.linalg.norm(boundary - center, axis=1)
        max_radius = float(radii.max())
        tool_radius = float(context.tool.footprint_radius_mm)

        # 从外到内收缩，避免刀具半径直接把轮廓切掉。
        start_radius = max(max_radius - tool_radius, tool_radius * 0.5 + 1e-6)
        if start_radius <= 0.0:
            raise ValueError("区域过小，无法生成有效螺旋刀路")

        rings: list[np.ndarray] = []
        radius = start_radius
        while radius >= tool_radius * 0.75:
            # 每一圈按圆周长度控制采样密度，保证采样足够细。
            segment_count = max(24, int(np.ceil(2.0 * np.pi * radius / max(stepover, 1.0))))
            theta = np.linspace(0.0, 2.0 * np.pi, segment_count, endpoint=False)
            ring = center + np.column_stack((np.cos(theta) * radius, np.sin(theta) * radius))
            rings.append(ring)
            radius -= stepover

        if not rings:
            raise ValueError("未生成任何螺旋圈，请检查刀具半径和切宽参数")

        moves: list = []
        previous: np.ndarray | None = None
        for index, ring in enumerate(rings):
            # 让相邻圆圈反向走，视觉上更像螺旋，而不是简单同向圆环。
            if index % 2 == 1:
                ring = ring[::-1]

            positions = context.to_positions(ring)
            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            else:
                moves.append(context.link_move(previous, positions[0]))

            moves.append(context.cut_move(ring, pass_index=index, label=f"第 {index + 1} 圈"))
            previous = positions[-1]

        if previous is None:
            raise ValueError("螺旋刀路为空")

        moves.append(context.retract_move_up(previous))

        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"螺旋走刀：共 {len(rings)} 圈，切宽 {stepover:g} mm",
                f"起始半径 {start_radius:g} mm，刀具半径 {tool_radius:g} mm",
            ),
        )