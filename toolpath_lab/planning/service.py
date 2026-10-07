"""Strategy lookup and execution -- the entry point shared by scripts and the API."""

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
    """One toolpath plus the warnings the caller needs to know about."""

    toolpath: Toolpath
    warnings: tuple[str, ...] = ()


def get_planner(planner_id: str) -> Planner:
    """Instantiate a registered strategy by id."""

    return PLANNERS.get(planner_id)()


def run_plan(
    *,
    planner_id: str,
    tool: Tool,
    region: RegionShape,
    parameters: Mapping[str, Any] | None = None,
) -> PlanningOutcome:
    """Generate a toolpath for one tool / region / parameter combination."""

    planner = get_planner(planner_id)
    validated = planner.parameters.coerce(parameters)
    context = PlanningContext(tool=tool, region=region, parameters=validated)
    toolpath = planner.plan(context)
    return PlanningOutcome(toolpath=toolpath, warnings=tuple(context.warnings))
