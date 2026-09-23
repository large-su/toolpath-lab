"""无界面运行交叉栅格刀路的示例。"""

from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan


outcome = run_plan(
    planner_id="crosshatch",
    tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
    region=build_region("square", {"side_mm": 80.0}),
    parameters={
        "stepover_mm": 6.0,
        "direction_deg": 0.0,
        "cross_angle_deg": 90.0,
        "feed_mm_per_min": 600.0,
    },
)

print("交叉栅格刀路统计：")
for key, value in outcome.toolpath.statistics().items():
    print(f"  {key}: {value}")
print("备注：")
for note in outcome.toolpath.notes:
    print(f"  - {note}")
