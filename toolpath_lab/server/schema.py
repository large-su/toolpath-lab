"""HTTP 接口的请求解析与校验。

原始 JSON 在这里、也只在这里被转成经过校验的领域对象，于是策略层不必关心传输细节，
所有接口共享同一套默认值、校验与错误信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.payload import coerce_group, split_capability
from toolpath_lab.core.region import RegionShape, build_polygon_region, build_region
from toolpath_lab.core.surface import SurfaceShape, build_surface
from toolpath_lab.core.tool import Tool, tool_parameters
from toolpath_lab.planning.registry import PLANNERS
from toolpath_lab.planning.roughing import roughing_parameters
from toolpath_lab.server.catalog import DEFAULT_PLANNER_ID, DEFAULT_REGION_ID, DEFAULT_SURFACE_ID


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """一次经过校验的规划请求。"""

    tool: Tool
    region: RegionShape
    surface: SurfaceShape
    planner_id: str
    tool_parameters: dict[str, Any] = field(default_factory=dict)
    region_id: str = DEFAULT_REGION_ID
    region_parameters: dict[str, Any] = field(default_factory=dict)
    surface_id: str = DEFAULT_SURFACE_ID
    surface_parameters: dict[str, Any] = field(default_factory=dict)
    planner_parameters: dict[str, Any] = field(default_factory=dict)
    roughing_parameters: dict[str, Any] = field(default_factory=dict)
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
        if region_id == "polygon":
            raw_boundary = region_parameters.get("boundary")
            if not isinstance(raw_boundary, (list, tuple)):
                raise ParameterError("导入模型区域必须提供 boundary 边界点")
            region = build_polygon_region(raw_boundary)
            region_parameters = region.to_params()
        else:
            region = build_region(region_id, region_parameters)

        surface_id, surface_parameters = split_capability(
            coerce_group(payload, "surface"),
            selector="type",
            default_id=DEFAULT_SURFACE_ID,
            label="surface",
        )
        surface = build_surface(surface_id, surface_parameters)

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
            surface=surface,
            planner_id=planner_id,
            tool_parameters=tool_values,
            region_id=region_id,
            region_parameters=region.parameters.coerce(region_parameters),
            surface_id=surface_id,
            surface_parameters=surface.parameters.coerce(surface_parameters),
            planner_parameters=planner_class.parameters.coerce(planner_parameters),
            roughing_parameters=roughing_parameters().coerce(coerce_group(payload, "roughing")),
        )

    def to_payload(self) -> dict[str, Any]:
        """规范化后的请求，回显给界面用于同步状态。"""

        return {
            "tool": dict(self.tool_parameters),
            "region": {"shape": self.region_id, "parameters": dict(self.region_parameters)},
            "surface": {"type": self.surface_id, "parameters": dict(self.surface_parameters)},
            "planner": {"id": self.planner_id, "parameters": dict(self.planner_parameters)},
            "roughing": dict(self.roughing_parameters),
        }

    def header_lines(self) -> list[str]:
        """导出文件头部用的配置说明。"""

        planner_label = PLANNERS.get(self.planner_id).label
        tool_line = (
            f"tool: {self.tool.kind.value} D{self.tool.diameter_mm:g} mm "
            f"L{self.tool.length_mm:g} mm"
        )
        if self.tool.kind.value == "bull":
            tool_line += f" Rn{self.tool.corner_radius_mm:g} mm"
        return [
            tool_line,
            f"region: {self.region_id} - {_format(self.region_parameters)}",
            f"surface: {self.surface_id} - {_format(self.surface_parameters)}",
            f"strategy: {self.planner_id} ({planner_label}) - {_format(self.planner_parameters)}",
            f"roughing: {_format(self.roughing_parameters)}",
        ]


def _format(parameters: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in parameters.items())
