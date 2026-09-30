"""领域层：纯数据类型与几何，不含任何 I/O 或框架依赖。"""

from toolpath_lab.core.errors import (
    ParameterError,
    PlanningError,
    RegistryError,
    ToolpathLabError,
)
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind,
    ParameterSet,
    ParameterSpec,
)
from toolpath_lab.core.path import MOVE_KIND_LABELS, Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.payload import coerce_group, split_capability
from toolpath_lab.core.region import (
    REGION_SHAPES,
    CircleRegion,
    RegionShape,
    SquareRegion,
    build_region,
    region_catalog,
)
from toolpath_lab.core.registry import Registry
from toolpath_lab.core.tool import (
    TOOL_KIND_LABELS,
    TOOL_KINDS,
    TOOL_TYPE_LABELS,
    TOOL_TYPES,
    Tool,
    ToolKind,
    ToolRecord,
    ToolType,
    build_tool,
    tool_geometry_values,
    tool_parameters,
    tool_type_defaults,
    tool_type_parameters,
    tool_type_parameters_for,
)

__all__ = [
    "MOVE_KIND_LABELS",
    "REGION_SHAPES",
    "TOOL_KINDS",
    "TOOL_KIND_LABELS",
    "TOOL_TYPES",
    "TOOL_TYPE_LABELS",
    "Choice",
    "CircleRegion",
    "Move",
    "MoveKind",
    "ParameterError",
    "ParameterKind",
    "ParameterSet",
    "ParameterSpec",
    "PlanningError",
    "Registry",
    "RegistryError",
    "RegionShape",
    "SquareRegion",
    "Tool",
    "ToolKind",
    "ToolRecord",
    "ToolType",
    "Toolpath",
    "ToolpathLabError",
    "build_region",
    "build_tool",
    "coerce_group",
    "region_catalog",
    "retract_move",
    "split_capability",
    "tool_geometry_values",
    "tool_parameters",
    "tool_type_defaults",
    "tool_type_parameters",
    "tool_type_parameters_for",
]
