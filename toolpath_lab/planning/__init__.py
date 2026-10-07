"""Toolpath strategies.

Importing this package registers every strategy; the API and the UI read their list from the registry.
To add a strategy, drop a module implementing Planner under planning/ and import it here.
"""

from toolpath_lab.planning.base import (
    RAPID_FEED_MM_PER_MIN,
    SAFE_HEIGHT_MM,
    Planner,
    PlanningContext,
)
from toolpath_lab.planning.coverage import (
    Coverage,
    coverage_warnings,
    measure_coverage,
)
from toolpath_lab.planning.registry import PLANNERS, planner_catalog
from toolpath_lab.planning.service import PlanningOutcome, get_planner, run_plan

# Import order = the order strategies appear in the UI; these lines are the registration.
from toolpath_lab.planning import raster as _raster  # noqa: F401
from toolpath_lab.planning import contour as _contour  # noqa: F401
from toolpath_lab.planning import adaptive as _adaptive  # noqa: F401

__all__ = [
    "PLANNERS",
    "RAPID_FEED_MM_PER_MIN",
    "SAFE_HEIGHT_MM",
    "Coverage",
    "Planner",
    "PlanningContext",
    "PlanningOutcome",
    "coverage_warnings",
    "get_planner",
    "measure_coverage",
    "planner_catalog",
    "run_plan",
]
