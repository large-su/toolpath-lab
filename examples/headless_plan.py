"""Smallest example without the UI: plan a toolpath, print the statistics, export NC.

Run it from the repository root:

    python examples/headless_plan.py

It does exactly what the UI does, only without HTTP and 3D display -- which is the point of the
layering: the toolpath algorithms run without the interface.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run directly, sys.path[0] is examples/ rather than the repository root, so toolpath_lab is not on
# the import path. Adding the root makes "python examples/headless_plan.py" work as documented.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from toolpath_lab.core.region import build_region  # noqa: E402
from toolpath_lab.core.tool import Tool, ToolKind  # noqa: E402
from toolpath_lab.export import toolpath_to_gcode  # noqa: E402
from toolpath_lab.planning import run_plan  # noqa: E402
from toolpath_lab.simulation import build_timeline  # noqa: E402


def main() -> None:
    tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
    region = build_region("square", {"side_mm": 80.0})

    outcome = run_plan(
        planner_id="raster",
        tool=tool,
        region=region,
        parameters={
            "mode": "zigzag",
            "stepover_mm": 6.0,
            "direction_deg": 0.0,
            "feed_mm_per_min": 800.0,
        },
    )
    toolpath = outcome.toolpath
    timeline = build_timeline(toolpath)
    statistics = toolpath.statistics()

    print(f"刀具: {tool.kind.value} D{tool.diameter_mm:g} mm, 足迹半径 {tool.footprint_radius_mm:g} mm")
    print(f"刀轨 {statistics['pass_count']} 条, 运动段 {statistics['move_count']}, 刀点 {statistics['point_count']}")
    print(f"切削长度 {statistics['cut_length_mm']:.1f} mm, 快移长度 {statistics['rapid_length_mm']:.1f} mm")
    print(f"预计工时 {statistics['estimated_time_s']:.1f} s（播放时间轴 {timeline.duration_s:.1f} s）")
    for warning in outcome.warnings:
        print(f"警告: {warning}")

    output = Path(__file__).resolve().parent / "toolpath_demo.nc"
    output.write_text(toolpath_to_gcode(toolpath, program_name="TOOLPATH_DEMO"), encoding="utf-8")
    print(f"已写出 {output.name}")


if __name__ == "__main__":
    main()
