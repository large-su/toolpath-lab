"""能力目录：界面需要的一切都由它生成。

目录来自与请求校验同一份 ParameterSet 声明，所以界面不可能出现后端不认识的控件；
反过来，新注册一个区域形状或策略，刷新页面就会自动出现——不需要改一行 JavaScript。
"""

from __future__ import annotations

from typing import Any

from toolpath_lab import __version__
from toolpath_lab.core.region import REGION_SHAPES, region_catalog
from toolpath_lab.core.tool import tool_parameters
from toolpath_lab.planning import RAPID_FEED_MM_PER_MIN, SAFE_HEIGHT_MM
from toolpath_lab.planning.registry import PLANNERS, planner_catalog

DEFAULT_REGION_ID = "square"
DEFAULT_PLANNER_ID = "raster"


def _defaults(registry: Any, item_id: str) -> dict[str, Any]:
    return registry.get(item_id).parameters.defaults()


def default_tool_parameters() -> dict[str, Any]:
    return tool_parameters().defaults()


def default_region_parameters(region_id: str = DEFAULT_REGION_ID) -> dict[str, Any]:
    return _defaults(REGION_SHAPES, region_id)


def default_planner_parameters(planner_id: str = DEFAULT_PLANNER_ID) -> dict[str, Any]:
    return _defaults(PLANNERS, planner_id)


def catalog_payload() -> dict[str, Any]:
    """能力、参数声明与默认值。"""

    return {
        "version": __version__,
        "tool": {
            "parameters": tool_parameters().to_dicts(),
            "defaults": default_tool_parameters(),
        },
        "regions": {
            "shapes": region_catalog(),
            "default_id": DEFAULT_REGION_ID,
            "defaults": default_region_parameters(),
        },
        "planners": {
            "list": planner_catalog(),
            "default_id": DEFAULT_PLANNER_ID,
            "defaults": default_planner_parameters(),
        },
        # 这些量在主程序里是固定的，界面上只做展示；想变成参数就在 planning/base.py 里改。
        "fixed": {
            "safe_height_mm": SAFE_HEIGHT_MM,
            "rapid_feed_mm_per_min": RAPID_FEED_MM_PER_MIN,
        },
    }
