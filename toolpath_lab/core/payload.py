"""Helpers for turning raw request dictionaries into validated domain objects.

The transport layers (HTTP today, a file or a GUI dialog tomorrow) all speak the
same payload shape::

    {
      "tool":    {"kind": "flat", "diameter_mm": 6, ...},
      "region":  {"shape": "rectangle", "parameters": {"width_mm": 80, ...}},
      "surface": {"type": "flat", "parameters": {"tilt_deg": 0}},
      "planner": {"id": "raster", "parameters": {"mode": "zigzag", ...}}
    }

Each capability group may also be given flat, with the selector key inline, so
hand written curl calls stay short.
"""

from __future__ import annotations

from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError


def coerce_group(
    payload: Mapping[str, Any] | None,
    key: str,
    *,
    default: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the nested mapping stored under key, validating its type."""

    source = payload or {}
    group = source.get(key)
    if group is None:
        return dict(default or {})
    if not isinstance(group, Mapping):
        raise ParameterError(f"request field {key!r} must be an object")
    return dict(group)


def split_capability(
    group: Mapping[str, Any],
    *,
    selector: str,
    default_id: str,
    label: str,
) -> tuple[str, dict[str, Any]]:
    """Split a capability group into its identifier and its parameters.

    Accepts both the nested form ({"shape": "circle", "parameters": {...}}) and
    the flat form ({"shape": "circle", "diameter_mm": 60}).
    """

    item_id = str(group.get(selector, default_id))
    nested = group.get("parameters")
    if nested is None:
        parameters = {key: value for key, value in group.items() if key != selector}
    elif isinstance(nested, Mapping):
        parameters = dict(nested)
    else:
        raise ParameterError(f"{label} field 'parameters' must be an object")
    return item_id, parameters
