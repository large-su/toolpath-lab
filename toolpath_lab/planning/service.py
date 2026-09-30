"""策略查找与执行——脚本与接口共用的入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.registry import PLANNERS


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """一条刀路，以及调用方需要知道的提醒。"""

    toolpath: Toolpath
    warnings: tuple[str, ...] = ()


def get_planner(planner_id: str) -> Planner:
    """按 id 实例化一个已注册的策略。"""

    return PLANNERS.get(planner_id)()


def run_plan(
    *,
    planner_id: str,
    tool: Tool,
    region: RegionShape,
    parameters: Mapping[str, Any] | None = None,
) -> PlanningOutcome:
    """为一份刀具/区域/参数组合生成刀路。"""

    planner = get_planner(planner_id)
    validated = planner.parameters.coerce(parameters)
    context = PlanningContext(tool=tool, region=region, parameters=validated)
    toolpath = planner.plan(context)
    return PlanningOutcome(toolpath=toolpath, warnings=tuple(context.warnings))
