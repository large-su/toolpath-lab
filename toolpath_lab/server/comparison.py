"""Compare strategies under one validated circular machining setup."""
import csv
import io
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.region import CircleRegion
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan

FIELDS = ("strategy", "cut_length_mm", "link_length_mm", "rapid_length_mm",
          "total_length_mm", "estimated_time_s", "move_count", "point_count")


def compare_strategies(request: PlanRequest) -> dict:
    if not isinstance(request.region, CircleRegion):
        raise PlanningError("策略对比需要圆形区域，请先切换形状")
    p = request.planner_parameters
    if p["stepover_mm"] < 0.5:
        raise PlanningError("策略对比的切宽需至少为 0.5 mm，以满足现有栅格策略参数范围")
    common = {"stepover_mm": p["stepover_mm"], "feed_mm_per_min": p["feed_mm_per_min"]}
    rows = []
    for strategy, planner, options in (
        ("螺旋", "spiral", {"radial_direction": p.get("radial_direction", "inward"),
                           "sample_step_mm": p.get("sample_step_mm", 0.5)}),
        ("往复栅格", "raster", {"mode": "zigzag", "direction_deg": p.get("direction_deg", 0)}),
        ("单向栅格", "raster", {"mode": "one_way", "direction_deg": p.get("direction_deg", 0)}),
    ):
        payload = request.to_payload()
        payload["planner"] = {"id": planner, "parameters": {**common, **options}}
        result = execute_plan(PlanRequest.from_payload(payload), with_timeline=False)
        rows.append({"strategy": strategy, **result.toolpath.statistics()})
    return {"ok": True, "request": request.to_payload(), "rows": rows,
            "note": "恒定进给估算；策略覆盖与边界处理不同，工时不代表切削质量或真实机床效率。"}


def comparison_csv(result: dict) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result["rows"])
    return "\ufeff" + output.getvalue()
