"""能力目录：界面需要的一切都由它生成。

目录来自与请求校验同一份 ParameterSet 声明，所以界面不可能出现后端不认识的控件；
反过来，新注册一个区域形状、策略、毛坯类型或加工类型，刷新页面就会自动出现——
不需要改一行 JavaScript。

除了原有基座的区域/刀路目录，这里追加 CAM 部分（毛坯、加工类型、加工参数、
后处理参数）与运维信息（数据目录、体积上限）。
"""

from __future__ import annotations

from typing import Any

from toolpath_lab import __version__
from toolpath_lab.brep import ALLOWED_SUFFIXES, DEFAULT_MAX_BYTES
from toolpath_lab.cam.service import planning_catalog
from toolpath_lab.core.region import REGION_SHAPES, region_catalog
from toolpath_lab.core.stock import stock_catalog
from toolpath_lab.core.tool import tool_parameters
from toolpath_lab.planning import RAPID_FEED_MM_PER_MIN, SAFE_HEIGHT_MM
from toolpath_lab.planning.registry import PLANNERS, planner_catalog

DEFAULT_REGION_ID = "square"
DEFAULT_PLANNER_ID = "raster"
DEFAULT_STOCK_ID = "rectangular"


def default_operation_parameters() -> dict[str, Any]:
    """新建工程/工序时使用的默认加工参数（界面第一次打开就有值）。

    做成函数而不是模块级常量：``planning_catalog()`` 会读取参数声明，
    放在导入期执行会让模块之间存在不必要的先后依赖。
    """

    return dict(planning_catalog()["defaults"])


def _defaults(registry: Any, item_id: str) -> dict[str, Any]:
    return registry.get(item_id).parameters.defaults()


def default_tool_parameters() -> dict[str, Any]:
    return tool_parameters().defaults()


def default_region_parameters(region_id: str = DEFAULT_REGION_ID) -> dict[str, Any]:
    return _defaults(REGION_SHAPES, region_id)


def default_planner_parameters(planner_id: str = DEFAULT_PLANNER_ID) -> dict[str, Any]:
    return _defaults(PLANNERS, planner_id)


def default_stock_parameters(stock_id: str = DEFAULT_STOCK_ID) -> dict[str, Any]:
    from toolpath_lab.core.stock import STOCK_TYPES

    return STOCK_TYPES.get(stock_id).parameters.defaults()


def catalog_payload() -> dict[str, Any]:
    """能力、参数声明与默认值。"""

    cam = planning_catalog()
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
        # CAM 部分：毛坯、工序、加工参数与后处理
        "stock": {
            "shapes": stock_catalog(),
            "default_id": DEFAULT_STOCK_ID,
            "defaults": default_stock_parameters(),
        },
        "cam": cam,
        "import": {
            "suffixes": list(ALLOWED_SUFFIXES),
            "max_bytes": DEFAULT_MAX_BYTES,
        },
        # 这些量在主程序里是固定的，界面上只做展示；想变成参数就在 planning/base.py 里改。
        "fixed": {
            "safe_height_mm": SAFE_HEIGHT_MM,
            "rapid_feed_mm_per_min": RAPID_FEED_MM_PER_MIN,
        },
    }


__all__ = [
    "DEFAULT_PLANNER_ID",
    "DEFAULT_REGION_ID",
    "DEFAULT_STOCK_ID",
    "catalog_payload",
    "default_operation_parameters",
    "default_planner_parameters",
    "default_region_parameters",
    "default_stock_parameters",
    "default_tool_parameters",
]
