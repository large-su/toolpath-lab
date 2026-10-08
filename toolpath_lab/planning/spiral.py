"""Continuous Archimedean spiral with a complete outer boundary pass."""
from __future__ import annotations

from math import ceil, cos, hypot, pi, sin, sqrt
from typing import ClassVar
import numpy as np
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import Choice, ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import CircleRegion
from toolpath_lab.core.tool import ToolKind
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.registry import PLANNERS

MAX_POINTS = 100_000
CHORD_TOLERANCE_MM = 0.01


def spiral_points(radius: float, pitch: float, step: float) -> np.ndarray:
    """Sample an inward spiral; a second-derivative bound limits chord error."""
    b = pitch / (2 * pi)
    # A lower bound on arc length catches excessive requests before allocation.
    if (pi * radius * radius / pitch + 2 * pi * radius) / step > MAX_POINTS - 8:
        raise PlanningError("刀点数量超过 100000，请增大采样步长或切宽，或减小区域尺寸")
    angle_step = min(pi / 36, step / radius, sqrt(8 * CHORD_TOLERANCE_MM / radius))
    count = max(4, ceil(2 * pi / angle_step))
    if count + 2 > MAX_POINTS:
        raise PlanningError("刀点数量超过 100000，请调整区域尺寸或采样参数")
    angles = np.linspace(0, 2 * pi, count + 1)
    points = [(radius * cos(a), radius * sin(a)) for a in angles]
    points[-1] = points[0]
    theta = 0.0
    end = radius / b
    while theta < end:
        r = max(0.0, radius - b * theta)
        delta = min(pi / 36, step / hypot(r, b),
                    sqrt(8 * CHORD_TOLERANCE_MM / hypot(r, 2 * b)))
        theta = min(end, theta + delta)
        r = max(0.0, radius - b * theta)
        points.append((r * cos(theta), r * sin(theta)))
        if len(points) > MAX_POINTS - 4:
            raise PlanningError("刀点数量超过 100000，请增大采样步长或切宽")
    points[-1] = (0.0, 0.0)
    return np.asarray(points, dtype=np.float64)


@PLANNERS.register
class SpiralPlanner(Planner):
    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "圆形连续螺旋"
    description: ClassVar[str] = "圆形平面阿基米德螺旋，含完整外圈，圈间不抬刀"
    parameters: ClassVar[ParameterSet] = ParameterSet((
        spec("radial_direction", "径向方向", K.CHOICE, "inward", group="刀路",
             choices=(Choice("inward", "由外向内"), Choice("outward", "由内向外"))),
        spec("stepover_mm", "切宽 ae", K.FLOAT, 3.0, minimum=0.1, maximum=100,
             step=0.1, unit="mm", group="刀路", help="每圈径向间距，不得大于刀具直径"),
        spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 800.0, minimum=10, maximum=10000,
             step=50, unit="mm/min", group="刀路"),
        spec("sample_step_mm", "采样步长", K.FLOAT, 0.5, minimum=0.01, maximum=5,
             step=0.1, unit="mm", group="刀路", help="最大采样弧长；弦误差同时限制为 0.01 mm"),
    ))

    def plan(self, context: PlanningContext) -> Toolpath:
        if not isinstance(context.region, CircleRegion):
            raise PlanningError("连续螺旋仅支持圆形区域，请将区域形状切换为圆形")
        if context.tool.kind is not ToolKind.FLAT:
            raise PlanningError("连续螺旋目前仅支持平底刀")
        pitch = self.require_positive(float(context.parameters["stepover_mm"]), "切宽")
        step = self.require_positive(float(context.parameters["sample_step_mm"]), "采样步长")
        if pitch > context.tool.diameter_mm + 1e-12:
            raise PlanningError("切宽不能大于刀具直径，否则相邻圈之间会漏扫")
        radius = context.region.diameter_mm / 2 - context.tool.radius_mm
        if radius <= 1e-9:
            raise PlanningError("刀具直径必须小于圆形区域直径，才能生成非退化螺旋")
        points = spiral_points(radius, pitch, step)
        if context.parameters["radial_direction"] == "outward":
            points = points[::-1].copy()
        positions = context.to_positions(points)
        return Toolpath(moves=(context.approach_move_down(positions[0]),
                              context.cut_move(points, pass_index=0, label="外圈与连续螺旋"),
                              context.retract_move_up(positions[-1])),
                        planner=self.id, planner_label=self.label,
                        notes=(f"螺旋径向间距 {pitch:g} mm，包含完整外圈与中心端点",
                               "圆形按解析圆边界偏置刀具半径；折线弦误差不超过 0.01 mm",
                               "位置连续；不包含材料切除、加减速或切削力模型"))
