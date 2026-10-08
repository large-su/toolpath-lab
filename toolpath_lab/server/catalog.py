"""Capability catalogue: everything the UI needs is generated from it.

The catalogue comes from the same ParameterSet declarations that validate requests, so the UI can
never show a control the backend does not understand; conversely, registering a new region shape or
strategy makes it appear on a page refresh -- without touching a single line of JavaScript.
"""

from __future__ import annotations

from typing import Any

from toolpath_lab import __version__
from toolpath_lab.core.region import REGION_SHAPES, region_catalog
from toolpath_lab.core.tool import tool_library, tool_parameters
from toolpath_lab.importers import IMPORT_PARAMETERS
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
    """Capabilities, parameter declarations and defaults."""

    return {
        "version": __version__,
        "tool": {
            "parameters": tool_parameters().to_dicts(),
            "defaults": default_tool_parameters(),
            # Named tools the panel offers as a starting point; picking one only fills the same
            # parameter fields, so nothing about a request depends on the library.
            "library": tool_library(),
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
        # Options of the drawing import; the panel renders the same declaration the endpoint validates.
        "import": {
            "parameters": IMPORT_PARAMETERS.to_dicts(),
            "defaults": IMPORT_PARAMETERS.defaults(),
        },
    }
