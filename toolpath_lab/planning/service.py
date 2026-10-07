"""Strategy lookup and execution -- the entry point shared by scripts and the API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.feeds import apply_corner_slowdown
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
    # Corner feed reduction is a motion post-process shared by every strategy (including third party
    # plugins), so it happens here rather than inside each plan(). Off by default.
    toolpath = apply_corner_slowdown(
        toolpath,
        corner_angle_deg=context.corner_angle_deg,
        corner_feed_ratio=context.corner_feed_ratio,
    )
    return PlanningOutcome(toolpath=toolpath, warnings=tuple(context.warnings))
