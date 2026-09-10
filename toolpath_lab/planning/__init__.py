"""刀路策略。

导入本包即完成所有策略的注册，接口与界面上的列表就是从注册表读出来的。
新增策略：在 planning/ 下加一个模块实现 Planner，在这里导入一行即可。
"""

from toolpath_lab.planning.base import (
    RAPID_FEED_MM_PER_MIN,
    SAFE_HEIGHT_MM,
    Planner,
    PlanningContext,
)
from toolpath_lab.planning.registry import PLANNERS, planner_catalog
from toolpath_lab.planning.service import PlanningOutcome, get_planner, run_plan

# 导入顺序 = 界面上策略的排列顺序，这一行就是"注册"。
from toolpath_lab.planning import raster as _raster  # noqa: F401

__all__ = [
    "PLANNERS",
    "RAPID_FEED_MM_PER_MIN",
    "SAFE_HEIGHT_MM",
    "Planner",
    "PlanningContext",
    "PlanningOutcome",
    "get_planner",
    "planner_catalog",
    "run_plan",
]
