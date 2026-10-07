"""Strategy lookup and execution -- the entry point shared by scripts and the API."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import TOOL_KIND_LABELS, Tool, ToolKind
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.feeds import apply_corner_slowdown
from toolpath_lab.planning.registry import PLANNERS
from toolpath_lab.planning.stepdown import apply_stepdown


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
    # Then stack the single plane into layers, also shared and also off by default.
    toolpath = apply_stepdown(
        toolpath, depth_mm=context.depth_mm, stepdown_mm=context.stepdown_mm
    )
    if context.tool.kind is not ToolKind.FLAT:
        # Say out loud how a shaped tool is treated: the offset uses the wall clearance over the whole
        # cut, while coverage still sweeps the (much smaller) flat contact on the floor.
        toolpath = replace(
            toolpath,
            notes=toolpath.notes
            + (
                f"{TOOL_KIND_LABELS[context.tool.kind.value].split()[0]}：贴壁间隙取整段切深的外伸半径 "
                f"R{context.cutting_radius_mm:g} mm，底面足迹半径 {context.tool.footprint_radius_mm:g} mm；"
                "覆盖率按足迹圆面算（球头刀在平底模型下足迹是一个点，圆鼻刀是 R−Rc 的圆环面），"
                "实际表面的残留高度取决于切宽，本模型不做表面仿真",
            ),
        )
    return PlanningOutcome(toolpath=toolpath, warnings=tuple(context.warnings))
