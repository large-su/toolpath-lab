"""无界面演示：在自由曲面上规划自适应等残留高度刀路。"""

from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan


def main() -> None:
    outcome = run_plan(
        planner_id="adaptive_scallop",
        tool=Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=30.0),
        region=build_region("square", {"side_mm": 80.0}),
        surface=build_surface("freeform", {
            "amplitude_mm": 10.0,
            "wavelength_x_mm": 80.0,
            "wavelength_y_mm": 40.0,
        }),
        parameters={
            "target_scallop_mm": 0.2,
            "min_stepover_mm": 0.8,
            "max_stepover_mm": 6.0,
            "direction_deg": 0.0,
            "feed_mm_per_min": 600.0,
        },
    )
    print("自适应等残留高度刀路统计：")
    for key, value in outcome.toolpath.statistics().items():
        print(f"  {key}: {value}")
    print("备注：")
    for note in outcome.toolpath.notes:
        print(f"  - {note}")


if __name__ == "__main__":
    main()
