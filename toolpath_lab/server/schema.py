"""HTTP 接口的请求解析与校验。

原始 JSON 在这里、也只在这里被转成经过校验的领域对象，于是策略层不必关心传输细节，
所有接口共享同一套默认值、校验与错误信息。

请求体长这样：

    {
      "tool":    {"kind": "flat", "diameter_mm": 6, "length_mm": 30},
      "model":   {"id": "m-1a2b3c4d"},              # 可选：导入过的模型
      "region":  {"shape": "model", "parameters": {"outline": "hull"}},
      "surface": {"kind": "model", "parameters": {"resolution_mm": 1.0}},
      "stock":   {"kind": "model", "parameters": {"margin_top_mm": 2}},
      "planner": {"id": "raster", "parameters": {"stepover_mm": 6, "depth_per_pass_mm": 2}}
    }

model.id 会被解析成模型库里的对象，供「模型轮廓」区域、「导入模型」加工面与
「模型包容体」毛坯使用；毛坯提供顶面高度，planner 的切深才能算出分几层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.mesh import ModelLibrary, StoredModel
from toolpath_lab.core.payload import coerce_group, split_capability
from toolpath_lab.core.region import RegionShape, build_region
from toolpath_lab.core.stock import STOCKS, Stock, build_stock
from toolpath_lab.core.surface import SURFACES, Surface, build_surface
from toolpath_lab.core.tool import Tool, tool_parameters
from toolpath_lab.planning.registry import PLANNERS
from toolpath_lab.server.catalog import (
    DEFAULT_PLANNER_ID,
    DEFAULT_REGION_ID,
    DEFAULT_STOCK_ID,
    DEFAULT_SURFACE_ID,
)


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
    surface: Surface = field(default_factory=lambda: build_surface(DEFAULT_SURFACE_ID))
    surface_id: str = DEFAULT_SURFACE_ID
    surface_parameters: dict[str, Any] = field(default_factory=dict)
    stock: Stock = field(default_factory=lambda: build_stock(DEFAULT_STOCK_ID))
    stock_id: str = DEFAULT_STOCK_ID
    stock_parameters: dict[str, Any] = field(default_factory=dict)
    model: StoredModel | None = None
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        models: ModelLibrary | None = None,
    ) -> "PlanRequest":
        """由 JSON 请求体构造，补齐默认值并完成校验。"""

        if payload is not None and not isinstance(payload, Mapping):
            raise ParameterError("请求体必须是 JSON 对象")

        model = _resolve_model(coerce_group(payload, "model"), models)

        tool_values = tool_parameters().coerce(coerce_group(payload, "tool"))
        tool = Tool.from_parameters(tool_values)

        region_id, region_parameters = split_capability(
            coerce_group(payload, "region"),
            selector="shape",
            default_id=DEFAULT_REGION_ID,
            label="region",
        )
        region = build_region(region_id, region_parameters, model=model)

        surface_id, surface_parameters = split_capability(
            coerce_group(payload, "surface"),
            selector="kind",
            default_id=DEFAULT_SURFACE_ID,
            label="surface",
        )
        surface_class = SURFACES.get(surface_id)
        surface = build_surface(surface_id, surface_parameters, model=model)

        stock_id, stock_parameters = split_capability(
            coerce_group(payload, "stock"),
            selector="kind",
            default_id=DEFAULT_STOCK_ID,
            label="stock",
        )
        stock_class = STOCKS.get(stock_id)
        stock = build_stock(stock_id, stock_parameters, model=model)

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
            surface=surface,
            surface_id=surface_id,
            surface_parameters=surface_class.parameters.coerce(surface_parameters),
            stock=stock,
            stock_id=stock_id,
            stock_parameters=stock_class.parameters.coerce(stock_parameters),
            model=model,
        )

    def to_payload(self) -> dict[str, Any]:
        """规范化后的请求，回显给界面用于同步状态。"""

        return {
            "tool": dict(self.tool_parameters),
            "model": {"id": self.model.id if self.model is not None else ""},
            "region": {"shape": self.region_id, "parameters": dict(self.region_parameters)},
            "surface": {"kind": self.surface_id, "parameters": dict(self.surface_parameters)},
            "stock": {"kind": self.stock_id, "parameters": dict(self.stock_parameters)},
            "planner": {"id": self.planner_id, "parameters": dict(self.planner_parameters)},
        }

    def header_lines(self) -> list[str]:
        """导出文件头部用的配置说明。"""

        planner_label = PLANNERS.get(self.planner_id).label
        lines = [
            f"tool: {self.tool.kind.value} D{self.tool.diameter_mm:g} mm L{self.tool.length_mm:g} mm",
            f"region: {self.region_id} - {_format(self.region_parameters)}",
            f"surface: {self.surface_id} - {_format(self.surface_parameters)}",
            f"stock: {self.stock_id} - {_format(self.stock_parameters)}",
            f"strategy: {self.planner_id} ({planner_label}) - {_format(self.planner_parameters)}",
        ]
        if self.model is not None:
            lines.insert(2, f"model: {self.model.name} ({self.model.id})")
        return lines


def _resolve_model(
    group: Mapping[str, Any], library: ModelLibrary | None
) -> StoredModel | None:
    """把 model 分组解析成已导入的模型；空 id 表示不使用模型。"""

    model_id = str(group.get("id") or "").strip()
    if not model_id:
        return None
    if library is None:
        raise ParameterError(
            f"请求引用了模型 {model_id!r}，但这次调用没有携带模型库；"
            "请通过 HTTP 接口上传模型"
        )
    return library.get(model_id)


def _format(parameters: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in parameters.items())
