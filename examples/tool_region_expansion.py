"""演示球头/圆鼻刀与椭圆区域的组合规划。"""

from __future__ import annotations

from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan


def main() -> None:
    outcome = run_plan(
        planner_id="crosshatch",
        tool=Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=40.0, nose_radius_mm=2.0),
        region=build_region(
            "ellipse",
            {"major_mm": 120.0, "minor_mm": 80.0, "rotation_deg": 25.0},
        ),
        parameters={
            "stepover_mm": 5.0,
            "direction_deg": 0.0,
            "cross_angle_deg": 90.0,
            "feed_mm_per_min": 600.0,
        },
    )
    print(outcome.toolpath.statistics())
    print("\n".join(outcome.toolpath.notes))


if __name__ == "__main__":
    main()
