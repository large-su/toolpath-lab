"""不用界面的最小示例：算一条刀路、打印统计、导出 NC。

在项目根目录运行：

    python examples/headless_plan.py

它做的事情和界面完全一样，只是绕过了 HTTP 与三维显示——这正是分层带来的好处：
刀路算法可以脱离界面单独跑。
"""

from __future__ import annotations

from pathlib import Path

from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan
from toolpath_lab.simulation import build_timeline


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
