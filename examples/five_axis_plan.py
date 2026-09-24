"""无界面演示：生成带前倾/侧倾刀轴姿态的五轴自由曲面刀路。"""

from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan


def main() -> None:
    outcome = run_plan(
        planner_id="five_axis",
        tool=Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=40.0),
        region=build_region("ellipse", {"major_mm": 120.0, "minor_mm": 80.0}),
        surface=build_surface("freeform", {
            "amplitude_mm": 4.0,
            "wavelength_x_mm": 80.0,
            "wavelength_y_mm": 60.0,
        }),
        parameters={
            "stepover_mm": 8.0,
            "lead_deg": 12.0,
            "side_tilt_deg": 4.0,
            "feed_mm_per_min": 500.0,
        },
    )
    toolpath = outcome.toolpath
    print("五轴刀路统计：", toolpath.statistics())
    print("姿态模式：", "five_axis" if toolpath.is_oriented else "three_axis")
    print("首个刀轴：", toolpath.moves[1].tool_axes[0].round(4).tolist())
    print("G-code 首行姿态示例：")
    print(next(line for line in toolpath_to_gcode(toolpath).splitlines() if " A" in line and line.startswith("G")))


if __name__ == "__main__":
    main()
