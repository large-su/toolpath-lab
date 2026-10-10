"""对比用的默认候选与自适应切宽建议（新增功能）。

两件事：

1. `default_candidates()` —— 一次典型的"策略比武"名单：往复、单向、螺旋，以及
   在注册表里存在时的环切（examples/plugins/contour_planner.py 启用后自动纳入）；
2. `suggest_stepover()` —— 由刀具与轴向切深反推"能切净"的切宽上限，用于给对比
   生成合理的默认参数（这来自本文档与 PPT 中推导的公式，见下）。

为什么需要 suggest_stepover
---------------------------
在平面加工中，相邻两条刀线之间留下的残余脊线宽度，取决于刀具**咬入半径** r_cut：

- 平底刀：r_cut = R，理论上切宽 ≤ 2R 就能切净（实际取 0.7~0.8·2R 保证重叠）；
- 球头刀：r_cut = sqrt(2·R·ap − ap²)，ap 很小时咬入半径很小，切宽必须跟着变小，
  否则会在中间留下整条未切带——这是"球头刀不能用来铣平面"的定量解释；
- 圆鼻刀：介于两者之间。

所以建议值取：切宽 ≤ overlap · 2 · r_cut，其中 overlap 默认 0.75。
"""

from __future__ import annotations

from typing import Any

from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.registry import PLANNERS

#: 相邻刀线的重叠系数：切宽 = overlap × 2 × 咬入半径。
DEFAULT_OVERLAP = 0.75


def suggest_stepover(
    tool: Tool, axial_depth_mm: float, *, overlap: float = DEFAULT_OVERLAP, minimum_mm: float = 0.5
) -> float:
    """由刀具与轴向切深给出"能切净"的切宽建议值（mm）。"""

    reach = tool.cutting_footprint_radius_mm(axial_depth_mm)
    return max(minimum_mm, round(overlap * 2.0 * reach, 1))


def default_candidates(
    *, stepover_mm: float | None = None, feed_mm_per_min: float | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """默认对比名单：往复 / 单向 / 螺旋（+ 环切，如果已注册）。"""

    raster: dict[str, Any] = {"mode": "zigzag"}
    one_way: dict[str, Any] = {"mode": "one_way"}
    spiral: dict[str, Any] = {"direction": "ccw"}
    if stepover_mm is not None:
        raster["stepover_mm"] = stepover_mm
        one_way["stepover_mm"] = stepover_mm
        spiral["stepover_mm"] = stepover_mm
    if feed_mm_per_min is not None:
        for parameters in (raster, one_way, spiral):
            parameters["feed_mm_per_min"] = feed_mm_per_min

    candidates: list[tuple[str, dict[str, Any]]] = [
        ("raster", raster),
        ("raster", one_way),
        ("spiral", spiral),
    ]
    if "contour" in PLANNERS:
        contour: dict[str, Any] = {}
        if stepover_mm is not None:
            contour["stepover_mm"] = stepover_mm
        candidates.append(("contour", contour))
    return [item for item in candidates if item[0] in PLANNERS]
