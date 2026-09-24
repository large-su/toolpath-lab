"""无界面演示：在解析自由曲面上规划交叉栅格刀路。"""

from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan


def main() -> None:
    outcome = run_plan(
        planner_id="crosshatch",
        tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
        region=build_region("ellipse", {"major_mm": 120.0, "minor_mm": 80.0}),
        surface=build_surface("freeform", {
            "amplitude_mm": 4.0,
            "wavelength_x_mm": 80.0,
            "wavelength_y_mm": 60.0,
        }),
        parameters={"stepover_mm": 6.0, "cross_angle_deg": 90.0},
    )
    print(outcome.toolpath.statistics())
    first_cut = next(move for move in outcome.toolpath.moves if move.kind.value == "cut")
    print("第一刀三维采样点数:", len(first_cut.points))
    print("第一刀 Z 范围:", first_cut.points[:, 2].min(), first_cut.points[:, 2].max())


if __name__ == "__main__":
    main()
