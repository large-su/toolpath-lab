"""一次 CAM 工序的完整流程。

请求 → 校验 → 拾取特征（面的加工区域）→ 规划 → 统计。
HTTP 层、脚本、测试都用这一个入口，因此"界面上能做的"和"接口能做的"永远一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from toolpath_lab.cam import parameters as cam_parameters_module
from toolpath_lab.cam.boundary import DEFAULT_CELL_MM, adaptive_cell_mm, region_from_face
from toolpath_lab.cam.common import MillingContext
from toolpath_lab.cam.face_mill import plan_face_mill
from toolpath_lab.cam.pocket_mill import plan_pocket_mill
from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.operation import OPERATION_KIND_LABELS, OperationKind
from toolpath_lab.core.part import PartModel
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.core.payload import coerce_group
from toolpath_lab.core.tool import Tool


@dataclass(frozen=True, slots=True)
class CAMOperationRequest:
    """一次经过校验的 CAM 工序请求。"""

    kind: str
    part: PartModel
    face_ids: tuple[int, ...]
    parameters: dict[str, Any]
    tool: Tool
    #: 加工起始高度（毛坯顶面）。缺省按"毛坯顶面 → 零件顶面"推断；
    #: 也可以显式给出，用于"从某个高度开始往下切"的场景（例如第二道工序接上一道的底）。
    top_z: float | None = None
    cell_mm: float = DEFAULT_CELL_MM
    #: 毛坯（可选）：给了它，加工起始高度就直接取毛坯顶面。
    stock: Any = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None, part: PartModel) -> "CAMOperationRequest":
        if payload is None or not isinstance(payload, Mapping):
            raise ParameterError("CAM 请求必须是 JSON 对象")
        kind = str(payload.get("kind") or OperationKind.POCKET_MILL.value)
        if kind not in OPERATION_KIND_LABELS:
            raise ParameterError(
                f"未知的加工类型 {kind!r}；可选：{', '.join(sorted(OPERATION_KIND_LABELS))}"
            )
        faces_raw = payload.get("faces") or payload.get("face_ids") or []
        if isinstance(faces_raw, (int, str)):
            faces_raw = [faces_raw]
        face_ids: list[int] = []
        for item in faces_raw:
            try:
                face_ids.append(int(item))
            except (TypeError, ValueError) as error:
                raise ParameterError(f"面序号必须是整数（收到 {item!r}）") from error
        if not face_ids:
            raise ParameterError("请先在三维视图中选择至少一个加工面")

        values = cam_parameters_module.cam_parameters().coerce(coerce_group(payload, "parameters"))
        top_z = payload.get("top_z")
        requested = payload.get("cell_mm")
        if requested:
            cell_mm = float(requested)
        else:
            # 没指定就按零件大小自适应：固定 0.4 mm 放到 200 mm 的零件上会变成 25 万格，
            # 规划一次要好几秒，响应也大得离谱。
            size = getattr(part, "size", None) or (0.0, 0.0, 0.0)
            cell_mm = adaptive_cell_mm(max(float(size[0]), float(size[1])))
        return cls(
            kind=kind,
            part=part,
            face_ids=tuple(face_ids),
            parameters=values,
            tool=cam_parameters_module.tool_from_cam_parameters(values),
            top_z=None if top_z is None else float(top_z),
            cell_mm=cell_mm,
        )

    def header_lines(self) -> list[str]:
        kind_label = OPERATION_KIND_LABELS[self.kind]
        return [
            f"operation: {kind_label} ({self.kind}) on faces "
            + ", ".join(f"#{item}" for item in self.face_ids),
            f"tool: flat D{self.tool.diameter_mm:g} mm L{self.tool.length_mm:g} mm",
            "parameters: " + ", ".join(f"{key}={value}" for key, value in self.parameters.items()),
        ]


@dataclass(frozen=True, slots=True)
class CAMOperationResult:
    """一次 CAM 工序的结果。"""

    request: CAMOperationRequest
    toolpath: Toolpath
    regions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.request.kind,
            "kind_label": OPERATION_KIND_LABELS[self.request.kind],
            "faces": list(self.request.face_ids),
            "tool": self.request.tool.describe(),
            "parameters": dict(self.request.parameters),
            "regions": [dict(item) for item in self.regions],
            "toolpath": self.toolpath.to_payload(),
            "warnings": list(self.warnings),
            "statistics": self.toolpath.statistics(),
        }


def _merge_toolpaths(toolpaths: Sequence[Toolpath], kind: str) -> Toolpath:
    if len(toolpaths) == 1:
        return toolpaths[0]
    moves: list[Move] = []
    for item in toolpaths:
        moves.extend(item.moves)
    return Toolpath(
        moves=tuple(moves),
        planner=kind,
        planner_label=OPERATION_KIND_LABELS.get(kind, kind),
        notes=tuple(note for item in toolpaths for note in item.notes),
    )


def _ceiling_for(request: CAMOperationRequest, floor_z: float) -> float:
    """决定这一道工序的**加工起始高度**。

    优先级：显式给出的 ``top_z`` → 毛坯顶面 → 零件顶面。
    没有毛坯时，为了不留下一层永远切不掉的"空气层"，起始高度取
    ``max(零件顶面, 目标面 + 每层切深)``——即至少切一层，这样界面上的
    深度、刀轨条数与实际加工一致。
    """

    if request.top_z is not None:
        return float(request.top_z)
    if request.stock is not None:
        return float(request.stock.bounds.z_max)
    part_top = float(request.part.bounds.z_max)
    return max(part_top, floor_z + float(request.parameters.get("cut_depth_mm", 1.0)))


def execute_operation(request: CAMOperationRequest) -> CAMOperationResult:
    """执行一次工序，返回刀路与加工区域摘要。"""

    toolpaths: list[Toolpath] = []
    regions: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, face_id in enumerate(request.face_ids):
        record = request.part.face(face_id)
        floor_z = float(record.plane[3]) if record is not None and record.plane else 0.0
        region = region_from_face(
            request.part, face_id, cell_mm=request.cell_mm,
            ceiling_z=_ceiling_for(request, floor_z),
        )
        regions.append(region.describe())
        warnings.extend(region.notes)
        context = MillingContext(
            tool=request.tool,
            top_z=region.top_z,
            floor_z=region.floor_z,
            parameters=request.parameters,
            region=region,
        )
        prefix = f"面 #{face_id}：" if len(request.face_ids) > 1 else ""
        if request.kind == OperationKind.FACE_MILL.value:
            toolpath = plan_face_mill(context, notes_prefix=prefix)
        elif request.kind == OperationKind.POCKET_MILL.value:
            toolpath = plan_pocket_mill(context, notes_prefix=prefix)
        elif request.kind == OperationKind.CONTOUR_MILL.value:
            toolpath = _plan_contour_mill(context, prefix)
        else:  # pragma: no cover - from_payload 已校验
            raise PlanningError(f"尚未实现的加工类型 {request.kind!r}")
        toolpaths.append(toolpath)
        warnings.extend(context.warnings)

    merged = _merge_toolpaths(toolpaths, request.kind)
    return CAMOperationResult(
        request=request,
        toolpath=merged,
        regions=tuple(regions),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _plan_contour_mill(context: MillingContext, prefix: str = "") -> Toolpath:
    """轮廓铣：只沿选中面的外轮廓走一刀（用于清边、倒角前的开粗）。"""

    from toolpath_lab.cam.boundary import offset_outline_polygons
    from toolpath_lab.cam.common import MoveBuilder, depth_levels

    region = context.region
    levels = depth_levels(region.top_z, region.floor_z, context.cut_depth,
                          finish_allowance=context.finish_allowance)
    if not levels:
        levels = [float(region.floor_z)]
    builder = MoveBuilder(context)
    offset = context.tool_radius + context.stock_allowance
    polygons = offset_outline_polygons(region, offset)
    if not polygons:
        raise PlanningError(
            f"{prefix}轮廓在偏置 {offset:g} mm 后为空：刀具相对加工区域太大"
        )
    for polygon in polygons:
        loop = np.vstack([polygon, polygon[:1]]) if hasattr(polygon, "shape") else None
        if loop is None:  # pragma: no cover - polygons 一定是 ndarray
            continue
        for level_index, target_z in enumerate(levels):
            points = np.column_stack((loop, np.full(loop.shape[0], float(target_z))))
            builder.rapid_to_safe(points[0], label="定位到轮廓起点")
            builder.plunge(points[0], label=f"下刀 Z{target_z:.3f}")
            builder.cut(points, label=f"{prefix}轮廓 {level_index + 1}/{len(levels)}")
    notes = [
        f"{prefix}轮廓铣：{len(levels)} 层，沿轮廓偏置 {offset:g} mm",
        f"刀具 D{context.tool.diameter_mm:g} mm",
    ]
    return builder.finish(planner="contour_mill", label="轮廓铣", notes=notes)


def planning_catalog() -> dict[str, Any]:
    """CAM 能力的目录：加工类型、参数声明、默认值、固定值。"""

    operation_parameters = cam_parameters_module.cam_parameters()
    return {
        "operations": [
            {
                "id": OperationKind.FACE_MILL.value,
                "label": OPERATION_KIND_LABELS[OperationKind.FACE_MILL.value],
                "description": "把选中的平面区域铣平：分层往复/单向扫描，可选精修轮廓",
            },
            {
                "id": OperationKind.POCKET_MILL.value,
                "label": OPERATION_KIND_LABELS[OperationKind.POCKET_MILL.value],
                "description": "把选中的型腔按层切除：环切或平行扫描，自动避让岛屿",
            },
            {
                "id": OperationKind.CONTOUR_MILL.value,
                "label": OPERATION_KIND_LABELS[OperationKind.CONTOUR_MILL.value],
                "description": "沿选中面的外轮廓走一刀：清边、开粗前的轮廓准备",
            },
        ],
        "parameters": operation_parameters.to_dicts(),
        "defaults": operation_parameters.defaults(),
        "controller": cam_parameters_module.controller_parameters().to_dicts(),
        "controller_defaults": cam_parameters_module.controller_parameters().defaults(),
        "fixed": dict(cam_parameters_module.CAM_FIXED),
    }


__all__ = [
    "CAMOperationRequest",
    "CAMOperationResult",
    "execute_operation",
    "planning_catalog",
]
