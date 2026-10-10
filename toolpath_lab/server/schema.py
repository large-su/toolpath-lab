"""HTTP 接口的请求解析与校验。

原始 JSON 在这里、也只在这里被转成经过校验的领域对象，于是策略层不必关心传输细节，
所有接口共享同一套默认值、校验与错误信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.payload import coerce_group, split_capability
from toolpath_lab.core.region import RegionShape, build_region
from toolpath_lab.core.tool import Tool, tool_parameters
from toolpath_lab.evaluation.compare import DEFAULT_WEIGHTS
from toolpath_lab.evaluation.defaults import default_candidates
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


@dataclass(frozen=True, slots=True)
class EvaluateRequest:
    """一次多策略对比请求（新增接口 /api/evaluate）。

    请求体与 /api/plan 同构，额外给出候选策略名单与仿真参数：

        {
          "tool":  {"kind": "flat", "diameter_mm": 10},
          "region": {"shape": "square", "parameters": {"side_mm": 80}},
          "candidates": [{"id": "raster", "parameters": {"mode": "zigzag"}},
                         {"id": "spiral", "parameters": {"stepover_mm": 6}}],
          "simulation": {"resolution_mm": 1.0, "axial_depth_mm": 1.0},
          "weights": [0.5, 0.3, 0.2]
        }

    `candidates` 缺省时使用 evaluation.defaults 里的常用名单；权重之和必须为 1。
    """

    tool: Tool
    region: RegionShape
    candidates: tuple[tuple[str, dict[str, Any]], ...]
    resolution_mm: float = 1.0
    axial_depth_mm: float = 1.0
    weights: tuple[float, float, float] = DEFAULT_WEIGHTS

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "EvaluateRequest":
        if payload is not None and not isinstance(payload, Mapping):
            raise ParameterError("请求体必须是 JSON 对象")

        tool = Tool.from_parameters(tool_parameters().coerce(coerce_group(payload, "tool")))
        region_id, region_parameters = split_capability(
            coerce_group(payload, "region"),
            selector="shape",
            default_id=DEFAULT_REGION_ID,
            label="region",
        )
        region = build_region(region_id, region_parameters)

        raw_candidates = (payload or {}).get("candidates")
        candidates: list[tuple[str, dict[str, Any]]] = []
        if raw_candidates is None:
            candidates = default_candidates()
        else:
            if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
                raise ParameterError("candidates 必须是数组")
            for item in raw_candidates:
                if isinstance(item, str):
                    planner_id, parameters = item, {}
                elif isinstance(item, Mapping):
                    planner_id = str(item.get("id", ""))
                    parameters = item.get("parameters") or {}
                else:
                    raise ParameterError("candidates 的元素必须是策略 id 或 {id, parameters}")
                if not planner_id:
                    raise ParameterError("candidates 的元素缺少 id")
                planner_class = PLANNERS.get(planner_id)
                candidates.append((planner_id, planner_class.parameters.coerce(parameters)))
            if not candidates:
                raise ParameterError("candidates 不能为空数组")

        simulation = coerce_group(payload, "simulation")
        resolution = float(simulation.get("resolution_mm", 1.0))
        depth = float(simulation.get("axial_depth_mm", 1.0))
        if resolution <= 0 or depth <= 0:
            raise ParameterError("simulation.resolution_mm 与 axial_depth_mm 必须是正数")

        raw_weights = (payload or {}).get("weights")
        weights: tuple[float, float, float] = DEFAULT_WEIGHTS
        if raw_weights is not None:
            if not isinstance(raw_weights, Sequence) or isinstance(raw_weights, (str, bytes)) \
                    or len(raw_weights) != 3:
                raise ParameterError("weights 必须是长度为 3 的数组 [质量, 效率, 行程]")
            try:
                values = tuple(float(value) for value in raw_weights)
            except (TypeError, ValueError) as error:
                raise ParameterError("weights 必须是数字") from error
            if min(values) < 0 or abs(sum(values) - 1.0) > 1e-9:
                raise ParameterError("weights 必须是三个非负数且之和为 1，例如 [0.5, 0.3, 0.2]")
            weights = (values[0], values[1], values[2])

        return cls(
            tool=tool,
            region=region,
            candidates=tuple(candidates),
            resolution_mm=resolution,
            axial_depth_mm=depth,
            weights=weights,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool": self.tool.describe(),
            "region": {"shape": self.region.id, "parameters": self.region.to_params()},
            "candidates": [
                {"id": planner_id, "parameters": dict(parameters)}
                for planner_id, parameters in self.candidates
            ],
            "simulation": {
                "resolution_mm": self.resolution_mm,
                "axial_depth_mm": self.axial_depth_mm,
            },
            "weights": list(self.weights),
        }
