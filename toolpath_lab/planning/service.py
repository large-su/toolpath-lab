"""Strategy lookup and execution -- the entry point shared by scripts and the API."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import TOOL_KIND_LABELS, Tool, ToolKind
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.collision import HolderCheck, check_holder, holder_note, holder_warnings
from toolpath_lab.planning.entry import measure_entry
from toolpath_lab.planning.feeds import apply_corner_slowdown
from toolpath_lab.planning.registry import PLANNERS
from toolpath_lab.planning.stepdown import apply_stepdown


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """One toolpath plus the warnings the caller needs to know about."""

    toolpath: Toolpath
    warnings: tuple[str, ...] = ()
    #: What the tool geometry above the flutes would do in this pocket (always measured).
    holder: HolderCheck | None = None


def get_planner(planner_id: str) -> Planner:
    """Instantiate a registered strategy by id."""

    return PLANNERS.get(planner_id)()


def _cusp_note(context: PlanningContext) -> str:
    """The theoretical ridge between two neighbouring passes, from the tool profile and the stepover.

    This is the classic scallop height of a curved cutter: the middle between two passes is
    `stepover / 2` away from both axes, so `profile_height` there *is* the height of the ridge that
    survives. It is stated before any measurement because it is a property of the tool and the stepover
    alone, and it is the number that says whether a stepover is usable at all: a ridge taller than the
    depth of cut means the two passes never clean the floor between them.
    """

    stepover = context.parameters.get("stepover_mm")
    if not isinstance(stepover, (int, float)) or stepover <= 0.0:
        return "该策略没有切宽参数，残留高度取决于刀具截面轮廓"
    tool = context.tool
    ridge = tool.cusp_height_mm(float(stepover))
    if ridge == float("inf"):
        return (
            f"相邻两刀（切宽 {float(stepover):g} mm）之间完全没有重叠：切削面只外伸到 "
            f"R{tool.reach_radius_mm:g} mm，会整条留下未切带，切宽请降到 "
            f"{2.0 * tool.reach_radius_mm:g} mm 以下"
        )
    if ridge <= 1e-9:
        return (
            f"相邻两刀（切宽 {float(stepover):g} mm）之间有平底重叠，理论残留高度 0（切宽在 "
            f"{2.0 * tool.footprint_radius_mm:g} mm 以内都是平底）"
        )
    return (
        f"相邻两刀（切宽 {float(stepover):g} mm，距刀轴各 {float(stepover) / 2.0:g} mm）之间的"
        f"理论残留高度 h = {ridge:.3f} mm——"
        f"球头刀按 R{context.tool.radius_mm:g}：h = R − √(R² − (s/2)²)，"
        f"比这一层的切深 {abs(context.depth_mm):g} mm 还高就说明两刀之间清不到底，"
        "要更小的切宽"
    )


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
    # How the tool gets down to a layer is a cutting move when it ramps or spirals, and a shallow
    # angle can make it longer than the pass it leads into, so the notes state the length it built
    # (measured on the finished path, which is one entry per layer once the layers are stacked).
    entries = measure_entry(toolpath, region)
    if entries is not None:
        toolpath = replace(toolpath, notes=toolpath.notes + (entries.note(),))
    # Whether the tool *above* the flutes fits into the pocket it just machined: the check adds a note
    # when the shank enters the pocket and still clears the wall, and a warning when it would rub.
    holder = check_holder(toolpath, region, context.tool)
    clearance_note = holder_note(holder)
    if clearance_note is not None:
        toolpath = replace(toolpath, notes=toolpath.notes + (clearance_note,))
    for message in holder_warnings(holder):
        context.warn(message)
    if context.tool.kind is not ToolKind.FLAT:
        # Say out loud how a shaped tool is treated, and how tall the ridge between two passes is
        # before any measurement: the floor itself is swept as the tool's own cross section, so the
        # number below is what the profile leaves standing half a stepover from the axis.
        toolpath = replace(
            toolpath,
            notes=toolpath.notes
            + (
                f"{TOOL_KIND_LABELS[context.tool.kind.value].split()[0]}：贴壁间隙取整段切深的外伸半径 "
                f"R{context.cutting_radius_mm:g} mm，底面足迹半径 {context.tool.footprint_radius_mm:g} mm、"
                f"切削面外伸半径 {context.tool.reach_radius_mm:g} mm；"
                "切除仿真按刀具截面轮廓扫掠（球头是球面、圆鼻是平底加角圆弧），"
                "所以网格上会真的出现弧形沟槽与刀间残留",
                _cusp_note(context),
            ),
        )
    if not region.is_flat_top:
        # A curved blank is where the 2.5D assumption shows: layers are constant Z, so the first ones
        # cut air above the crown. Say so instead of letting the volumes explain it.
        toolpath = replace(
            toolpath,
            notes=toolpath.notes
            + (
                f"毛坯上表面不是平的（{region.label}）：等高分层是**恒定 Z** 的，所以曲面上方的层会先切空气，"
                "直到层深落到曲面之下；切除体积、高度图与三维显示都按这个上表面起算"
                "（随形加工需要三轴联动，本项目不做）",
            ),
        )
    return PlanningOutcome(
        toolpath=toolpath, warnings=tuple(context.warnings), holder=holder
    )
