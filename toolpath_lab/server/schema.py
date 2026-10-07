"""Request parsing and validation for the HTTP API.

Raw JSON is turned into validated domain objects here and only here, so the strategy layer never
deals with transport details and every endpoint shares the same defaults, validation and messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.payload import coerce_group, split_capability
from toolpath_lab.core.region import RegionShape, build_region
from toolpath_lab.core.tool import Tool, tool_parameters
from toolpath_lab.planning.registry import PLANNERS
from toolpath_lab.server.catalog import DEFAULT_PLANNER_ID, DEFAULT_REGION_ID


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """One validated planning request."""

    tool: Tool
    region: RegionShape
    planner_id: str
    tool_parameters: dict[str, Any] = field(default_factory=dict)
    region_id: str = DEFAULT_REGION_ID
    region_parameters: dict[str, Any] = field(default_factory=dict)
    planner_parameters: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "PlanRequest":
        """Build from a JSON request body, filling in defaults and validating."""

        if payload is not None and not isinstance(payload, Mapping):
            raise ParameterError("请求体必须是 JSON 对象")

        tool_values = tool_parameters().coerce(coerce_group(payload, "tool"))
        tool = Tool.from_parameters(tool_values)

        region_id, region_parameters = split_capability(
            coerce_group(payload, "region"),
            selector="shape",
            default_id=DEFAULT_REGION_ID,
            label="region",
        )
        region = build_region(region_id, region_parameters)

        planner_id, planner_parameters = split_capability(
            coerce_group(payload, "planner"),
            selector="id",
            default_id=DEFAULT_PLANNER_ID,
            label="planner",
        )
        planner_class = PLANNERS.get(planner_id)

        return cls(
            tool=tool,
            region=region,
            planner_id=planner_id,
            tool_parameters=tool_values,
            region_id=region_id,
            region_parameters=region.parameters.coerce(region_parameters),
            planner_parameters=planner_class.parameters.coerce(planner_parameters),
        )

    def to_payload(self) -> dict[str, Any]:
        """The normalised request, echoed back to the UI so it can sync its state."""

        return {
            "tool": dict(self.tool_parameters),
            "region": {"shape": self.region_id, "parameters": dict(self.region_parameters)},
            "planner": {"id": self.planner_id, "parameters": dict(self.planner_parameters)},
        }

    def header_lines(self) -> list[str]:
        """Configuration summary used in the export file header."""

        planner_label = PLANNERS.get(self.planner_id).label
        return [
            f"tool: {self.tool.kind.value} D{self.tool.diameter_mm:g} mm L{self.tool.length_mm:g} mm",
            f"region: {self.region_id} - {_format(self.region_parameters)}",
            f"strategy: {self.planner_id} ({planner_label}) - {_format(self.planner_parameters)}",
        ]


def _format(parameters: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in parameters.items())
