"""HTTP 接口的请求解析与校验。

原始 JSON 在这里、也只在这里被转成经过校验的领域对象，于是策略层不必关心传输细节，
所有接口共享同一套默认值、校验与错误信息。
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
    """一次经过校验的规划请求。"""

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
        """由 JSON 请求体构造，补齐默认值并完成校验。"""

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
        """规范化后的请求，回显给界面用于同步状态。"""

        return {
            "tool": dict(self.tool_parameters),
            "region": {"shape": self.region_id, "parameters": dict(self.region_parameters)},
            "planner": {"id": self.planner_id, "parameters": dict(self.planner_parameters)},
        }

    def header_lines(self) -> list[str]:
        """导出文件头部用的配置说明。"""

        planner_label = PLANNERS.get(self.planner_id).label
        return [
            f"tool: {self.tool.kind.value} D{self.tool.diameter_mm:g} mm L{self.tool.length_mm:g} mm",
            f"region: {self.region_id} - {_format(self.region_parameters)}",
            f"strategy: {self.planner_id} ({planner_label}) - {_format(self.planner_parameters)}",
        ]


def _format(parameters: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in parameters.items())
