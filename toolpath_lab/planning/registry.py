"""Strategy registry.

Importing toolpath_lab.planning imports the strategy modules, and the class decorator inside each of
them registers the strategy with PLANNERS. A new strategy therefore only needs "one file plus one
import line in __init__.py"; the API and the UI pick it up automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from toolpath_lab.core.registry import Registry

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from toolpath_lab.planning.base import Planner

PLANNERS: "Registry[type[Planner]]" = Registry("planner")


def planner_catalog() -> list[dict[str, Any]]:
    """JSON description of every registered strategy."""

    return PLANNERS.catalog()
