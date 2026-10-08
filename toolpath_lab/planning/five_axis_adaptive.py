"""球头刀教学组合：自适应扫描线 + 共用五轴姿态和平滑。

保留自适应名义刀点与步距，不把估算目标当作经过刀具扫掠验证的真实残留上限。
"""
from __future__ import annotations

from typing import ClassVar

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import ParameterSet
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.tool import ToolKind
from toolpath_lab.planning.adaptive_scallop import AdaptiveScallopPlanner
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.five_axis import FiveAxisPlanner, orient_surface_passes
from toolpath_lab.planning.registry import PLANNERS


@PLANNERS.register
class FiveAxisAdaptivePlanner(Planner):
    id: ClassVar[str] = "five_axis_adaptive"
    label: ClassVar[str] = "五轴自适应等残留高度"
    description: ClassVar[str] = "仅球头刀：按目标残留估算步距，逐点五轴姿态，可平滑与前置粗加工"
    parameters: ClassVar[ParameterSet] = AdaptiveScallopPlanner.parameters + ParameterSet(tuple(
        item for item in FiveAxisPlanner.parameters
        if item.key not in {spec.key for spec in AdaptiveScallopPlanner.parameters}
        and item.key != "stepover_mm"
    ))

    def plan(self, context: PlanningContext) -> Toolpath:
        if context.tool.kind is not ToolKind.BALL:
            raise PlanningError("五轴自适应等残留高度目前仅支持球头刀，请把刀具类型切换为球头刀；平底/圆鼻刀请使用原有策略")
        adaptive = AdaptiveScallopPlanner().plan(context)
        target = float(context.parameters["target_scallop_mm"])
        theoretical = AdaptiveScallopPlanner._nominal_stepover(context.tool.radius_mm, target)
        if float(context.parameters["min_stepover_mm"]) > theoretical + 1e-9:
            context.warn("最小步距大于球头刀目标残留对应的理论步距，可能无法达到目标残留；请减小最小步距")
        context.warn("五轴等残留采用球头刀高度场与曲率步距的教学估算，未验证倾斜刀具扫掠的真实残留高度，不能保证严格等残留或无碰撞")
        passes = [(move.points[:, :2], move.pass_index, f"五轴等残留 · 第 {move.pass_index + 1} 刀")
                  for move in adaptive.moves if move.kind is MoveKind.CUT]
        metadata = {**adaptive.metadata, "five_axis_adaptive": {
            "supported_tool": "ball", "estimate_only": True,
            "spacing_model": "ball_scallop_curvature_heightfield",
            "nominal_unclamped_stepover_mm": theoretical,
            "safe_retract_between_passes": True,
        }}
        return orient_surface_passes(context, passes, planner_id=self.id, planner_label=self.label,
                                     notes=adaptive.notes + ("组合策略：自适应刀路间距 + 逐点五轴刀轴；刀间始终抬刀后转向",),
                                     metadata=metadata, always_retract=True)
