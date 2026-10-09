"""一次 CAM 工序的完整流程。

请求 → 校验 → 拾取特征（面的加工区域）→ 规划 → 统计。
HTTP 层、脚本、测试都用这一个入口，因此"界面上能做的"和"接口能做的"永远一致。

加工类型分两类，走两条不同的路：

* **2.5 轴**（平面铣 / 型腔铣 / 轮廓铣）：由选中面的边界环建栅格加工区域，刀路在水平层里走；
* **3 轴曲面**（平行行切 / 等高铣）：交给 :mod:`toolpath_lab.surfacing`，
  平行行切吃三角网格（opencamlib），等高铣按层剖切 BRep（OCP）再偏置（pyclipper）。

两条路产出的都是同一个 :class:`~toolpath_lab.core.path.Toolpath`，
所以仿真、G-code 导出、工序树统计一行都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.cam import parameters as cam_parameters_module
from toolpath_lab.cam.boundary import (DEFAULT_CELL_MM, MachiningRegion, adaptive_cell_mm,
                                       build_region, offset_outline_polygons,
                                       region_from_face)
from toolpath_lab.cam.common import (MillingContext, MoveBuilder, depth_levels,
                                     in_level_transfer, level_key, stepped_levels)
from toolpath_lab.cam.face_mill import plan_face_mill, plan_face_mill_multi
from toolpath_lab.cam.pocket_mill import plan_pocket_mill, plan_pocket_mill_multi
from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.operation import (FACE_SELECTION_KINDS, OPERATION_KIND_LABELS,
                                         SURFACE_KINDS, SURFACE_STRATEGIES, OperationKind)
from toolpath_lab.core.part import PartModel
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.core.payload import coerce_group
from toolpath_lab.core.stock import Mesh
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.surfacing.parameters import (coerce_surface_parameters,
                                               surface_defaults_for, surface_parameters,
                                               surface_parameters_for)


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
    #: BRep 模型（等高铣按层剖切需要它；网格里没有拓扑）。
    #: 由 :class:`~toolpath_lab.server.workspace.Workspace` 注入 —— 它手里才有原始文件。
    brep: Any = None

    @property
    def is_surface(self) -> bool:
        return self.kind in SURFACE_KINDS

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None, part: PartModel,
                     tool_resolver: Callable[[str], Tool | None] | None = None,
                     ) -> "CAMOperationRequest":
        """由请求体构造工序请求。

        ``tool_resolver`` 是"刀具 id → 刀具几何"的查询函数（由工作空间注入刀具库）。
        给了它，请求里的 ``tool_id`` 就会被解析成真正的刀具几何，并**覆盖**
        参数字典里的 ``tool_kind`` / ``tool_diameter_mm`` / ``tool_length_mm`` /
        ``tool_flute_mm``；没给（纯算法调用、离线脚本）就完全按旧行为走。
        指定的刀具不存在时一律报错，绝不悄悄退回默认刀 —— 那会让人以为刀路是对的。
        """

        if payload is None or not isinstance(payload, Mapping):
            raise ParameterError("CAM 请求必须是 JSON 对象")
        kind = str(payload.get("kind") or OperationKind.POCKET_MILL.value)
        if kind not in OPERATION_KIND_LABELS:
            raise ParameterError(
                f"未知的加工类型 {kind!r}；可选：{', '.join(sorted(OPERATION_KIND_LABELS))}"
            )
        face_ids = _face_ids_from_payload(payload, kind)
        if kind in SURFACE_KINDS:
            return cls._surface(kind, payload, part, face_ids, tool_resolver)

        raw = dict(coerce_group(payload, "parameters"))
        tool_id = str(raw.get("tool_id") or "").strip()
        tool = _tool_from_library(tool_id, tool_resolver) if tool_id else None
        if tool is not None:
            raw.update(cam_parameters_module.tool_geometry_parameters(tool))
        values = cam_parameters_module.cam_parameters().coerce(raw)
        if tool is not None:
            values["tool_id"] = tool_id
        if tool is None:
            tool = cam_parameters_module.tool_from_cam_parameters(values)
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
            face_ids=face_ids,
            parameters=values,
            tool=tool,
            top_z=None if top_z is None else float(top_z),
            cell_mm=cell_mm,
        )

    @classmethod
    def _surface(cls, kind: str, payload: Mapping[str, Any], part: PartModel,
                 face_ids: tuple[int, ...],
                 tool_resolver: Callable[[str], Tool | None] | None = None,
                 ) -> "CAMOperationRequest":
        """曲面工序：参数走 ``surfacing`` 的声明，加工面可以一个都不选。"""

        raw = dict(coerce_group(payload, "parameters"))
        tool_id = str(raw.get("tool_id") or "").strip()
        tool = _tool_from_library(tool_id, tool_resolver) if tool_id else None
        if tool is not None:
            # 刀库里的刀具类型（钻头/丝锥等）映射到曲面能用的三种刀型，避免把
            # 一个不在选项里的类型塞给 surfacing。
            raw.update(cam_parameters_module.tool_geometry_parameters(tool))
        values = coerce_surface_parameters(raw)
        # 类型已经决定了策略，这里**覆盖**掉请求里的值：客户端传什么都不影响，
        # 免得"选了等高铣却带着平行行切的策略"这种自相矛盾的请求悄悄跑出怪刀路。
        values["strategy"] = SURFACE_STRATEGIES[kind]
        # 刀具键由解析出来的刀具决定（面参数声明里没有这些键，不清理会一直挂在参数里）。
        values.pop("tool_id", None)
        values.pop("tool_flute_mm", None)
        if tool is None:
            tool = Tool(
                kind=ToolKind(str(values.get("tool_kind") or ToolKind.FLAT.value)),
                diameter_mm=float(values["tool_diameter_mm"]),
                length_mm=float(values["tool_length_mm"]),
            )
        else:
            values["tool_kind"] = tool.kind.value
            values["tool_diameter_mm"] = tool.diameter_mm
            values["tool_length_mm"] = tool.length_mm
        return cls(kind=kind, part=part, face_ids=face_ids, parameters=values, tool=tool)

    def header_lines(self) -> list[str]:
        kind_label = OPERATION_KIND_LABELS[self.kind]
        scope = ("面 " + ", ".join(f"#{item}" for item in self.face_ids)) if self.face_ids \
            else "整个零件"
        return [
            f"operation: {kind_label} ({self.kind}) on {scope}",
            f"tool: {self.tool.kind.value} D{self.tool.diameter_mm:g} mm L{self.tool.length_mm:g} mm",
            "parameters: " + ", ".join(f"{key}={value}" for key, value in self.parameters.items()),
        ]


def _tool_from_library(tool_id: str, resolver: Callable[[str], Tool | None] | None) -> Tool | None:
    """按 id 取刀具库里的刀；没有解析器（纯算法调用）时返回 None。

    有解析器却查不到这把刀 = 请求引用了不存在的刀具，必须报错：
    静默回退到默认刀会算出一条"看起来正常但用错刀"的刀路。
    """

    if resolver is None:
        return None
    tool = resolver(tool_id)
    if tool is None:
        raise ParameterError(f"刀具库里没有这把刀：{tool_id!r}（可能已被删除，请重新选择刀具）")
    return tool


def _face_ids_from_payload(payload: Mapping[str, Any], kind: str) -> tuple[int, ...]:
    """解析加工面。

    2.5 轴工序**必须**选面（没有面就没有加工区域）；曲面工序允许不选，
    不选就是"整个零件"。
    """

    faces_raw = payload.get("faces") or payload.get("face_ids") or []
    if isinstance(faces_raw, (int, str)):
        faces_raw = [faces_raw]
    face_ids: list[int] = []
    for item in faces_raw:
        try:
            face_ids.append(int(item))
        except (TypeError, ValueError) as error:
            raise ParameterError(f"面序号必须是整数（收到 {item!r}）") from error
    if not face_ids and kind in FACE_SELECTION_KINDS:
        raise ParameterError("请先在三维视图中选择至少一个加工面")
    return tuple(face_ids)


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


#: 选了多个加工面时按几何高度统一排层的加工类型——"切削顺序"参数只在这几类上生效
#: （清边铣不选面、曲面工序走另一条路，都只有一个加工区域）。
_MULTI_FACE_KINDS = frozenset({
    OperationKind.FACE_MILL.value,
    OperationKind.POCKET_MILL.value,
    OperationKind.CONTOUR_MILL.value,
})


def execute_operation(request: CAMOperationRequest) -> CAMOperationResult:
    """执行一次工序，返回刀路与加工区域摘要。"""

    if request.is_surface:
        toolpath, warnings = _plan_surface(request)
        return CAMOperationResult(request=request, toolpath=toolpath,
                                  regions=(), warnings=tuple(dict.fromkeys(warnings)))

    # 清边铣不需要选面：区域由毛坯外框与零件包围盒算出
    if request.kind == OperationKind.EDGE_CLEAR.value:
        return _plan_edge_clear(request)

    regions: list[dict[str, Any]] = []
    region_notes: list[list[str]] = []
    planned: list[tuple[MillingContext, str]] = []
    warnings: list[str] = []

    # 第一遍：逐面建加工区域与规划现场（不规划刀路）。
    for face_id in request.face_ids:
        record = request.part.face(face_id)
        # 层高的起算点：平面面读平面方程，曲面/斜面无平面方程时用零件顶面，
        # 真正的底面高度由 region.floor_z（区域内底面最低点）给出。
        floor_z = float(record.plane[3]) if record is not None and record.plane else \
            float(request.part.bounds.z_max)
        # 只有平面铣要求"必须是水平面"：型腔铣要支持斜面/曲面底，轮廓铣沿边界走一刀
        # 也不在乎底面是否水平。
        horizontal_only = request.kind == OperationKind.FACE_MILL.value
        region = region_from_face(
            request.part, face_id, cell_mm=request.cell_mm,
            ceiling_z=_ceiling_for(request, floor_z),
            require_horizontal=horizontal_only,
        )
        regions.append(region.describe())
        region_notes.append(list(region.notes))
        context = MillingContext(
            tool=request.tool,
            top_z=region.top_z,
            floor_z=region.floor_z,
            parameters=request.parameters,
            region=region,
        )
        prefix = f"面 #{face_id}：" if len(request.face_ids) > 1 else ""
        planned.append((context, prefix))

    # 第二遍：规划刀路。多个加工面时按几何高度统一排层（UG 的层优先/深度优先），
    # 否则逐面各自切完，"先选了哪个面"就直接决定了加工顺序。
    merged: Toolpath
    if len(planned) > 1 and request.kind in _MULTI_FACE_KINDS:
        order = str(request.parameters.get("cutting_order", "level_first"))
        if request.kind == OperationKind.FACE_MILL.value:
            merged = plan_face_mill_multi(planned, order=order)
        elif request.kind == OperationKind.POCKET_MILL.value:
            merged = plan_pocket_mill_multi(planned, order=order)
        else:  # CONTOUR_MILL（_MULTI_FACE_KINDS 只含这三类）
            merged = _plan_contour_mill_multi(planned, order=order)
    else:
        toolpaths: list[Toolpath] = []
        for context, prefix in planned:
            if request.kind == OperationKind.FACE_MILL.value:
                toolpath = plan_face_mill(context, notes_prefix=prefix)
            elif request.kind == OperationKind.POCKET_MILL.value:
                toolpath = plan_pocket_mill(context, notes_prefix=prefix)
            elif request.kind == OperationKind.CONTOUR_MILL.value:
                toolpath = _plan_contour_mill(context, prefix)
            else:  # pragma: no cover - from_payload 已校验
                raise PlanningError(f"尚未实现的加工类型 {request.kind!r}")
            toolpaths.append(toolpath)
        merged = _merge_toolpaths(toolpaths, request.kind)

    # 警告按"面"交错收集：区域备注在前、该面规划产生的警告在后（与逐面规划一致）。
    for notes, (context, _) in zip(region_notes, planned):
        warnings.extend(notes)
        warnings.extend(context.warnings)

    return CAMOperationResult(
        request=request,
        toolpath=merged,
        regions=tuple(regions),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _plan_edge_clear(request: "CAMOperationRequest") -> "CAMOperationResult":
    """清边铣：把毛坯比零件大出来的那一圈切掉。

    与型腔铣的"刀具中心必须在区域内"不同，清边的硬约束只有一条：
    **刀具中心距零件轮廓 ≥ 刀具半径 + 余量**（切削刃不伤零件）。
    中心允许走出毛坯边（那里没有材料，属于空切），因此环带宽度小于刀半径时
    依然能清——UG 的清边刀路也是这样贴着零件轮廓走、部分悬在毛坯外。

    实现：合法中心区域 = 零件包围盒外扩 (R + 余量) 之外、毛坯框（或其外扩，
    当合法区超出毛坯时）之内。对这个环带由外向内逐圈环切，按每层切深分层。
    """

    stock = request.stock
    if stock is None:
        raise PlanningError(
            "清边铣需要毛坯：请先在毛坯面板创建毛坯，再生成清边刀路"
        )
    bounds = getattr(stock, "bounds", None)
    part_bounds = request.part.bounds
    if bounds is None or part_bounds is None:
        raise PlanningError("毛坯或零件缺少包围盒信息，无法计算清边区域")

    kind = str(getattr(stock, "id", "") or "")
    if "cyl" in kind:
        raise PlanningError(
            "清边铣当前只支持矩形毛坯；圆柱毛坯的外圈请用等高铣（按整个零件分层）加工"
        )

    margin_xy = min(
        part_bounds.x_min - bounds.x_min, bounds.x_max - part_bounds.x_max,
        part_bounds.y_min - bounds.y_min, bounds.y_max - part_bounds.y_max,
    )
    if margin_xy <= 1e-9:
        raise PlanningError(
            f"毛坯比零件只大 {max(margin_xy, 0.0):g} mm：外圈没有可清除的材料，"
            "清边铣无需执行；若要加工顶面请用平面铣"
        )

    tool_radius = request.tool.radius_mm
    allowance = max(0.0, float(request.parameters.get("stock_allowance_mm", 0.0) or 0.0))
    grow = tool_radius + allowance
    # 合法中心区的内边界：零件包围盒外扩 (R + 余量)
    grown = np.asarray([
        [part_bounds.x_min - grow, part_bounds.y_min - grow],
        [part_bounds.x_max + grow, part_bounds.y_min - grow],
        [part_bounds.x_max + grow, part_bounds.y_max + grow],
        [part_bounds.x_min - grow, part_bounds.y_max + grow],
    ], dtype=np.float64)
    # 外边界：毛坯框与合法区内边界的并集矩形（合法区可能超出毛坯 → 空切）
    outline = np.asarray([
        [min(bounds.x_min, grown[0, 0]), min(bounds.y_min, grown[0, 1])],
        [max(bounds.x_max, grown[1, 0]), min(bounds.y_min, grown[0, 1])],
        [max(bounds.x_max, grown[1, 0]), max(bounds.y_max, grown[2, 1])],
        [min(bounds.x_min, grown[0, 0]), max(bounds.y_max, grown[2, 1])],
    ], dtype=np.float64)

    top_z = float(bounds.z_max)
    floor_z = float(part_bounds.z_max)
    if top_z - floor_z <= 1e-9:
        raise PlanningError(
            "毛坯顶面与零件最高点等高：外圈没有可切除的深度，清边铣无需执行"
        )

    narrow_margin = grow >= margin_xy - 0.5 * request.cell_mm
    if narrow_margin:
        # 窄外圈场景不建环带栅格（合法中心区已越出毛坯框，环带退化为零宽）；
        # 区域描述用毛坯框本身，刀路走下面两圈保底环。
        region = build_region(np.asarray([
            [bounds.x_min, bounds.y_min],
            [bounds.x_max, bounds.y_min],
            [bounds.x_max, bounds.y_max],
            [bounds.x_min, bounds.y_max],
        ], dtype=np.float64), [], top_z=top_z, floor_z=floor_z,
            cell_mm=request.cell_mm)
    else:
        region = build_region(outline, [grown], top_z=top_z, floor_z=floor_z,
                              cell_mm=request.cell_mm)
    parameters = dict(request.parameters)
    parameters["cut_mode"] = "contour"  # 清边只做环切：窄环带上往复会频繁转向
    context = MillingContext(
        tool=request.tool,
        top_z=top_z,
        floor_z=floor_z,
        parameters=parameters,
        region=region,
    )
    builder = MoveBuilder(context)

    levels = depth_levels(top_z, floor_z, context.cut_depth,
                          finish_allowance=context.finish_allowance)
    if not levels:
        raise PlanningError("清边深度不足一层：请检查毛坯顶面与零件高度")

    def _rect_ring(x0: float, y0: float, x1: float, y1: float) -> NDArray[np.float64]:
        return np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                          dtype=np.float64)

    grown_ring = _rect_ring(float(grown[0, 0]), float(grown[0, 1]),
                            float(grown[1, 0]), float(grown[1, 1]))
    stepover = max(context.effective_stepover, 0.5 * region.cell_mm)
    max_distance = float(region.distance.max()) if region.distance.size else 0.0
    rings: list[NDArray[np.float64]] = []
    if narrow_margin:
        # 窄外圈（外圈宽 ≤ 刀具半径 + 余量）：合法中心区已越出毛坯框，环带退化。
        # 只走一圈：零件外扩 (R + 余量) 的方框。它的切削刃覆盖距零件
        # [0, 2R + 2·余量]，外圈 [0, margin] 必然包含在内；而刀心距零件恒为
        # R + 余量，切削刃不会越过零件轮廓。**不能**再贴毛坯框走一圈——
        # 那会让刀心距零件小于 R，直接过切。
        rings = [grown_ring]
    else:
        # 常规外圈：由外（毛坯框）向内（零件外扩 R+余量）逐圈环切
        offset = 0.0
        guard = 0
        while guard < 4096:
            guard += 1
            polygons = offset_outline_polygons(region, offset, min_area_mm2=1.0)
            if not polygons:
                break
            rings.extend(polygons)
            offset += stepover
            if offset > max_distance + 1e-6:
                break
    if not rings:
        raise PlanningError("清边区域为空：请检查毛坯偏移与刀具直径")

    first_cut = True
    for level_index, target_z in enumerate(levels):
        for ring_index, ring in enumerate(rings):
            closed = np.vstack([ring, ring[:1]])
            points = np.column_stack(
                (closed, np.full(closed.shape[0], target_z)))
            builder.rapid_to_safe(points[0], label="清边定位")
            builder.plunge(points[0], label=f"下刀 Z{target_z:.3f}")
            builder.cut(points, label=f"清边 第 {level_index + 1} 层 第 {ring_index + 1} 圈")
            first_cut = False
    if first_cut:  # pragma: no cover - rings 非空时必不相等
        raise PlanningError("清边没有生成任何刀轨")

    notes = [
        f"清边范围：毛坯 {bounds.size[0]:g}×{bounds.size[1]:g} mm，"
        f"外圈宽 {margin_xy:g} mm，切深 {top_z - floor_z:g} mm",
        f"{len(levels)} 层，环切 {len(rings)} 圈，步距 {context.effective_stepover:g} mm",
    ]
    return CAMOperationResult(
        request=request,
        toolpath=builder.finish(planner="edge_clear", label="清边铣", notes=notes),
        regions=(region.describe(),),
        warnings=tuple(dict.fromkeys(context.warnings)),
    )


def _plan_surface(request: CAMOperationRequest) -> tuple[Toolpath, list[str]]:
    """曲面工序（平行行切 / 等高铣）。

    平行行切只需要三角网格，**选中面时按面裁剪网格**（只在这一小片曲面上走刀）；
    等高铣按整层剖切 BRep，所以要求请求里带着 BRep —— 网格文件里没有拓扑，
    剖切无从下手。
    """

    from toolpath_lab.surfacing import (OclUnavailableError, ParallelRequest,
                                        WaterlineRequest, parallel_toolpath,
                                        waterline_toolpath, require_ocl)

    values = request.parameters
    tool_kind = str(values.get("tool_kind") or ToolKind.FLAT.value)

    if request.kind == OperationKind.PARALLEL_SURFACE.value:
        try:
            require_ocl()
        except OclUnavailableError as error:
            raise PlanningError(f"平行行切需要 opencamlib：{error}") from error
        mesh = _surface_mesh(request.part, request.face_ids)
        surface_request = ParallelRequest(
            stepover_mm=float(values["stepover_mm"]),
            direction_deg=float(values["direction_deg"]),
            sampling_mm=float(values["sampling_mm"]),
            tool_kind=tool_kind,
            tool_diameter_mm=request.tool.diameter_mm,
            tool_length_mm=request.tool.length_mm,
            safe_height_mm=float(values["safe_height_mm"]),
            feed_mm_per_min=float(values["feed_mm_per_min"]),
            rapid_feed_mm_per_min=float(values["rapid_feed_mm_per_min"]),
            cut_mode=str(values["cut_mode"]),
            stock_allowance_mm=float(values["stock_allowance_mm"]),
        )
        try:
            result = parallel_toolpath(mesh, surface_request,
                                       bounds=_mesh_bounds(mesh, surface_request))
        except ValueError as error:
            raise PlanningError(f"平行行切失败：{error}") from error
        warnings = list(result.toolpath.notes)
        if tool_kind != ToolKind.FLAT.value:
            warnings.append("平行行切用球头/圆鼻刀时刀心高度取自 opencamlib，已按刀型补偿")
        return result.toolpath, warnings

    if request.brep is None:
        raise PlanningError(
            "等高铣需要 BRep 模型：工程里没有保存原始 STEP/IGES 文件，"
            "请重新导入模型后再生成"
        )
    surface_request = WaterlineRequest(
        step_down_mm=float(values["step_down_mm"]),
        tool_diameter_mm=request.tool.diameter_mm,
        side_allowance_mm=float(values["side_allowance_mm"]),
        safe_height_mm=float(values["safe_height_mm"]),
        feed_mm_per_min=float(values["feed_mm_per_min"]),
        rapid_feed_mm_per_min=float(values["rapid_feed_mm_per_min"]),
        order=str(values["level_order"]),
    )
    try:
        layered = waterline_toolpath(request.brep, surface_request)
    except ValueError as error:
        raise PlanningError(f"等高铣失败：{error}") from error
    warnings = list(layered.toolpath.notes)
    if tool_kind != ToolKind.FLAT.value:
        warnings.append(
            "等高铣的偏置法只对平底刀严格正确：球头/圆鼻刀的等高线要按等残留高度算，"
            "当前版本仍未实现，结果仅供参考"
        )
    if request.face_ids:
        warnings.append("等高铣按整个零件分层，加工面选择不参与计算")
    return layered.toolpath, warnings


def _surface_mesh(part: PartModel, face_ids: Sequence[int]) -> Mesh:
    """取曲面加工要用的网格；给了加工面就只保留这些面的三角形。

    不重新编号顶点：opencamlib 只读三角形，用不到的顶点留着毫无影响，
    却能省掉一次索引重映射 —— 那正是这类代码最容易出错的地方。
    """

    mesh = part.mesh
    if not face_ids:
        return Mesh(positions=mesh.positions, indices=mesh.indices)

    keep = np.zeros(int(mesh.indices.shape[0]), dtype=bool)
    for face_id in face_ids:
        record = part.face(int(face_id))
        if record is None:
            raise PlanningError(f"模型里没有面 #{face_id}")
        start = int(record.triangle_start)
        keep[start:start + int(record.triangle_count)] = True
    if not keep.any():
        raise PlanningError("选中的面没有任何三角面，无法生成曲面刀路")
    return Mesh(positions=mesh.positions, indices=np.asarray(mesh.indices)[keep])


def _mesh_bounds(mesh: Mesh, request: Any) -> tuple[float, float, float, float]:
    """扫描范围：**只用被加工的三角形**算，再按刀半径外扩。

    不这么做的话，只选了一个小面也会沿整个零件铺满扫描线 —— 大部分是空行程，
    落刀一次一点，纯粹在浪费时间。
    """

    corners = mesh.positions[np.asarray(mesh.indices).reshape(-1)]
    radius = float(request.tool_diameter_mm) / 2.0
    return (float(corners[:, 0].min()) - radius, float(corners[:, 1].min()) - radius,
            float(corners[:, 0].max()) + radius, float(corners[:, 1].max()) + radius)


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

    def emit(polygon: NDArray[np.float64], level_index: int, target_z: float,
             allowed: NDArray[np.bool_] | None) -> None:
        loop = np.vstack([polygon, polygon[:1]]) if hasattr(polygon, "shape") else None
        if loop is None:  # pragma: no cover - polygons 一定是 ndarray
            return
        points = np.column_stack((loop, np.full(loop.shape[0], float(target_z))))
        previous = builder.last_point
        if previous is not None and in_level_transfer(
                region, context.tool_radius, previous[:2], points[0][:2],
                level_mask=allowed):
            # 层内平移/层间斜降：一次下刀把各圈与各层连贯走完
            gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
            z_gap = abs(float(previous[2]) - float(points[0][2]))
            if gap > 1e-9 or z_gap > 1e-9:
                builder.link(np.vstack([previous, points[0]]), label="层内转移")
        else:
            builder.rapid_to_safe(points[0], label="定位到轮廓起点")
            builder.plunge(points[0], label=f"下刀 Z{target_z:.3f}")
        builder.cut(points, label=f"{prefix}轮廓 {level_index + 1}/{len(levels)}")

    if region.has_geometry_obstacle:
        # 有几何障碍的区域环要**逐层重算**：凸台挡住的列被裁掉后，
        # 等距轮廓会缩环、断环甚至整层消失（层高越过凸台顶又恢复）。
        for level_index, target_z in enumerate(levels):
            allowed = region.obstacle_mask(target_z, context.tool)
            level_polygons = polygons if allowed is None else \
                offset_outline_polygons(region, offset, mask=allowed)
            for polygon in level_polygons:
                emit(polygon, level_index, target_z, allowed)
    else:
        for polygon in polygons:
            for level_index, target_z in enumerate(levels):
                emit(polygon, level_index, target_z, None)
    notes = [
        f"{prefix}轮廓铣：{len(levels)} 层，沿轮廓偏置 {offset:g} mm",
        f"刀具 D{context.tool.diameter_mm:g} mm",
    ]
    return builder.finish(planner="contour_mill", label="轮廓铣", notes=notes)


@dataclass(slots=True)
class _ContourJob:
    """轮廓铣多区域调度里一个加工面的现场。"""

    context: MillingContext
    region: MachiningRegion
    prefix: str
    offset: float
    polygons: list[NDArray[np.float64]]
    levels: list[float] = field(default_factory=list)


def _plan_contour_mill_multi(items: Sequence[tuple[MillingContext, str]], *,
                             order: str = "level_first") -> Toolpath:
    """轮廓铣的多区域统一层调度（UG/NX 的层优先 / 深度优先）。

    层高用绝对网格在区域间对齐（同 :func:`toolpath_lab.cam.common.stepped_levels`）：
    层优先=每层把各面的轮廓走完再下降；深度优先=一个面的各层走完再换下一个面。
    两种顺序只差遍历顺序，层高与每层刀路完全一致。
    """

    jobs: list[_ContourJob] = []
    for context, prefix in items:
        region = context.region
        offset = context.tool_radius + context.stock_allowance
        polygons = offset_outline_polygons(region, offset)
        if not polygons:
            raise PlanningError(
                f"{prefix}轮廓在偏置 {offset:g} mm 后为空：刀具相对加工区域太大"
            )
        jobs.append(_ContourJob(context=context, region=region, prefix=prefix,
                                offset=offset, polygons=list(polygons)))

    global_top = max(float(job.region.top_z) for job in jobs)
    layers = stepped_levels(
        global_top,
        [float(job.region.floor_z) + job.context.finish_allowance for job in jobs],
        float(jobs[0].context.cut_depth),
    )
    for job in jobs:
        top = float(job.region.top_z)
        floor_target = float(job.region.floor_z) + job.context.finish_allowance
        # 下界用归一后的键比较：floor_target 带 ±1e-7 的网格面拟合噪声，而 layers
        # 已按 1 nm 归一（40.00000015 → 40.0），裸比较会把末层滤掉导致漏切。
        floor_key = level_key(floor_target)
        job.levels = [z for z in layers if floor_key - 1e-9 <= z <= top - 1e-9]
        if not job.levels:
            # 与单面一致：没有可切深度时也补一刀贴底轮廓（光一刀）。
            job.levels = [float(job.region.floor_z)]

    # 共享一个 MoveBuilder，安全高度取全局最高顶面。
    lead = jobs[0].context
    dispatch = MillingContext(
        tool=lead.tool,
        top_z=global_top,
        floor_z=min(float(job.region.floor_z) for job in jobs),
        parameters=lead.parameters,
        region=None,
    )
    builder = MoveBuilder(dispatch)

    def emit(job: _ContourJob, level_index: int, target_z: float) -> None:
        # 有几何障碍时环按层重算（缩环/断环），层内转移也拿同一张掩码约束。
        # 轮廓铣每层只沿轮廓走一条偏置环（不填充区域），嵌套面的环贴各自轮廓、
        # 不会在同层切到同一块 XY，所以不需要型腔/平面铣那样的同层去重。
        allowed = job.region.obstacle_mask(target_z, job.context.tool)
        polygons = job.polygons if allowed is None else \
            offset_outline_polygons(job.region, job.offset, mask=allowed)
        for polygon in polygons:
            loop = np.vstack([polygon, polygon[:1]])
            points = np.column_stack((loop, np.full(loop.shape[0], float(target_z))))
            previous = builder.last_point
            if previous is not None and in_level_transfer(
                    job.region, job.context.tool_radius, previous[:2], points[0][:2],
                    level_mask=allowed):
                gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
                z_gap = abs(float(previous[2]) - float(points[0][2]))
                if gap > 1e-9 or z_gap > 1e-9:
                    builder.link(np.vstack([previous, points[0]]), label="层内转移")
            else:
                builder.rapid_to_safe(points[0], label="定位到轮廓起点")
                builder.plunge(points[0], label=f"下刀 Z{target_z:.3f}")
            builder.cut(points,
                        label=f"{job.prefix}轮廓 {level_index + 1}/{len(job.levels)}")

    if str(order) == "depth_first":
        for job in jobs:
            for level_index, target_z in enumerate(job.levels):
                emit(job, level_index, target_z)
    else:
        # 未知取值一律按层优先（参数校验在上游，这里是防御性回退，与 cut_mode 同风格）。
        index_of = [{z: index for index, z in enumerate(job.levels)} for job in jobs]
        all_levels = sorted({z for job in jobs for z in job.levels}, reverse=True)
        for target_z in all_levels:
            for job, mapping in zip(jobs, index_of):
                level_index = mapping.get(target_z)
                if level_index is None:
                    continue
                emit(job, level_index, target_z)

    notes = [
        f"{job.prefix}轮廓铣：{len(job.levels)} 层，沿轮廓偏置 {job.offset:g} mm"
        for job in jobs
    ]
    notes.append(f"刀具 D{lead.tool.diameter_mm:g} mm")
    if str(order) == "depth_first":
        notes.append(
            f"切削顺序：深度优先——单个区域从上到下切完再换下一个（共 {len(jobs)} 个加工面）"
        )
    else:
        notes.append(
            f"切削顺序：层优先——各区域在同一高度合并、逐层下切（共 {len(jobs)} 个加工面）"
        )
    return builder.finish(planner="contour_mill", label="轮廓铣", notes=notes)


def surface_defaults() -> dict[str, Any]:
    """曲面工序参数的默认值（完整集合，含内部键 ``strategy``）。"""

    return surface_parameters().defaults()


def _operation_entry(kind: OperationKind, description: str) -> dict[str, Any]:
    """目录里的一个加工类型条目（含它自己的参数声明与默认值）。"""

    entry: dict[str, Any] = {
        "id": kind.value,
        "label": OPERATION_KIND_LABELS[kind.value],
        "description": description,
        "surface": kind.value in SURFACE_KINDS,
        # 清边铣按毛坯外框计算，不需要拾取加工面；其余 2.5 轴工序必须选面
        "needs_faces": kind.value in FACE_SELECTION_KINDS,
    }
    if kind.value in SURFACE_KINDS:
        entry["parameters"] = surface_parameters_for(kind.value).to_dicts()
        entry["defaults"] = surface_defaults_for(kind.value)
        # 类型隐含、界面上不显示的参数：前端切换类型时直接写进参数字典，
        # 这样参数声明里的 visible_if 仍然能正确联动。
        entry["implied"] = {"strategy": SURFACE_STRATEGIES[kind.value]}
    return entry


def planning_catalog() -> dict[str, Any]:
    """CAM 能力的目录：加工类型、参数声明、默认值、固定值。"""

    operation_parameters = cam_parameters_module.cam_parameters()
    surface = surface_parameters()
    return {
        "operations": [
            _operation_entry(OperationKind.FACE_MILL, "把选中的平面区域铣平：分层往复/单向扫描，可选精修轮廓"),
            _operation_entry(OperationKind.POCKET_MILL, "把选中的型腔按层切除：环切或平行扫描，自动避让岛屿"),
            _operation_entry(OperationKind.CONTOUR_MILL, "沿选中面的外轮廓走一刀：清边、开粗前的轮廓准备"),
            _operation_entry(
                OperationKind.EDGE_CLEAR,
                "把毛坯比零件大出来的外圈切掉：按毛坯外框与零件包围盒之间的环带环切，"
                "不需要选面（毛坯顶面 → 零件最高点）",
            ),
            _operation_entry(
                OperationKind.PARALLEL_SURFACE,
                "沿一族平行线在曲面上落刀（opencamlib）：三轴曲面的粗加工与半精加工主力。"
                "选中若干面就只加工这些面，不选面则加工整个零件",
            ),
            _operation_entry(
                OperationKind.WATERLINE,
                "按层高逐层剖切零件并沿截面轮廓走刀（OCP 剖切 + pyclipper 偏置）："
                "适合陡壁，能同时加工外轮廓与内腔，按整个零件计算",
            ),
        ],
        "parameters": operation_parameters.to_dicts(),
        "defaults": operation_parameters.defaults(),
        # 曲面参数的完整声明（含 strategy）：脚本与测试用它，界面用上面每个类型自己的那份。
        "surface_parameters": surface.to_dicts(),
        "surface_defaults": surface.defaults(),
        "controller": cam_parameters_module.controller_parameters().to_dicts(),
        "controller_defaults": cam_parameters_module.controller_parameters().defaults(),
        "fixed": dict(cam_parameters_module.CAM_FIXED),
    }


__all__ = [
    "CAMOperationRequest",
    "CAMOperationResult",
    "execute_operation",
    "planning_catalog",
    "surface_defaults",
    "surface_defaults_for",
    "surface_parameters_for",
]
