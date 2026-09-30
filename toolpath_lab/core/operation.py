"""工序（operation）：工序树、参数模板与状态。

工序树对标 NX 的"工序导航器"：一行是一个工序，包含序号、名称、加工类型、参数与状态。
本模块只描述数据，不生成刀路——刀路由 :mod:`toolpath_lab.cam` 里的规划器负责，
这样"工序"与"策略"可以各自演进。

三种加工类型（首期）：

``face_mill``
    平面铣：把选中的平面区域（通常是顶面）铣平；
``pocket_mill``
    型腔铣：把选中的型腔（含岛屿）按层切除；
``contour_mill``
    轮廓铣：沿选中面的外轮廓走一刀（型腔铣的副产品，同一套边界算法）。

状态机刻意做得简单：``draft``（参数已改、需要重新生成）→ ``generated``（刀路已生成）
→ ``verified``（仿真过）。界面上的"启用/禁用"只是一个布尔开关，禁用的工序不参与
整体仿真与导出。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from toolpath_lab.core.errors import ParameterError


class OperationKind(str, Enum):
    """加工类型。

    前三种是 **2.5 轴**加工：先拾取一个面，再由这个面的边界环算出加工区域，
    刀路只在一层层的水平面里走（``cam``）。

    后两种是 **3 轴曲面**加工：不需要拾取面，直接对整个零件（或选中的面）
    生成沿曲面起伏的刀路（``surfacing``）。
    """

    FACE_MILL = "face_mill"
    POCKET_MILL = "pocket_mill"
    CONTOUR_MILL = "contour_mill"
    #: 清边铣：把毛坯比零件大出来的那一圈切掉（不需要选面，按毛坯外框计算）。
    EDGE_CLEAR = "edge_clear"
    PARALLEL_SURFACE = "parallel_surface"
    WATERLINE = "waterline"


OPERATION_KIND_LABELS: dict[str, str] = {
    OperationKind.FACE_MILL.value: "平面铣",
    OperationKind.POCKET_MILL.value: "型腔铣",
    OperationKind.CONTOUR_MILL.value: "轮廓铣",
    OperationKind.EDGE_CLEAR.value: "清边铣",
    OperationKind.PARALLEL_SURFACE.value: "平行行切",
    OperationKind.WATERLINE.value: "等高铣",
}

#: 需要拾取加工面的加工类型（2.5 轴）。不在这个集合里的按整个零件加工。
FACE_SELECTION_KINDS: frozenset[str] = frozenset({
    OperationKind.FACE_MILL.value,
    OperationKind.POCKET_MILL.value,
    OperationKind.CONTOUR_MILL.value,
})

#: 曲面加工类型（3 轴），实际计算在 :mod:`toolpath_lab.surfacing`。
SURFACE_KINDS: frozenset[str] = frozenset({
    OperationKind.PARALLEL_SURFACE.value,
    OperationKind.WATERLINE.value,
})

#: 曲面加工类型 -> ``surfacing`` 的加工策略。类型本身已经决定了策略，
#: 所以界面上不再单独出一个"策略"下拉框（避免两个控件说同一件事、还可能互相矛盾）。
SURFACE_STRATEGIES: dict[str, str] = {
    OperationKind.PARALLEL_SURFACE.value: "parallel",
    OperationKind.WATERLINE.value: "waterline",
}


class OperationState(str, Enum):
    """工序状态。"""

    DRAFT = "draft"
    GENERATED = "generated"
    VERIFIED = "verified"


OPERATION_STATE_LABELS: dict[str, str] = {
    OperationState.DRAFT.value: "待生成",
    OperationState.GENERATED.value: "已生成",
    OperationState.VERIFIED.value: "已仿真",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Operation:
    """一道工序。"""

    operation_id: str
    name: str
    kind: str
    parameters: dict[str, Any] = field(default_factory=dict)
    face_ids: tuple[int, ...] = ()
    enabled: bool = True
    state: str = OperationState.DRAFT.value
    sequence: int = 0
    statistics: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.kind not in OPERATION_KIND_LABELS:
            raise ParameterError(
                f"未知的加工类型 {self.kind!r}；可选：{', '.join(sorted(OPERATION_KIND_LABELS))}"
            )

    @property
    def kind_label(self) -> str:
        return OPERATION_KIND_LABELS[self.kind]

    @property
    def state_label(self) -> str:
        return OPERATION_STATE_LABELS.get(self.state, self.state)

    def mark_draft(self) -> None:
        if self.state != OperationState.DRAFT.value:
            self.state = OperationState.DRAFT.value
        self.updated_at = _now()

    def to_payload(self, *, include_parameters: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.operation_id,
            "sequence": self.sequence,
            "name": self.name,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "state": self.state,
            "state_label": self.state_label,
            "enabled": self.enabled,
            "faces": list(self.face_ids),
            "statistics": dict(self.statistics),
            "warnings": list(self.warnings),
            "updated_at": self.updated_at,
        }
        if include_parameters:
            payload["parameters"] = dict(self.parameters)
        return payload


@dataclass(frozen=True, slots=True)
class ParameterTemplate:
    """参数模板：把一组常用参数存起来复用（"默认模板"与"自定义保存"共用）。"""

    template_id: str
    name: str
    kind: str
    parameters: dict[str, Any]
    builtin: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.template_id,
            "name": self.name,
            "kind": self.kind,
            "kind_label": OPERATION_KIND_LABELS.get(self.kind, self.kind),
            "parameters": dict(self.parameters),
            "builtin": self.builtin,
        }


@dataclass(slots=True)
class OperationTree:
    """一个工程的工序树。"""

    operations: list[Operation] = field(default_factory=list)
    templates: list[ParameterTemplate] = field(default_factory=list)

    # -- 查询 --------------------------------------------------------------
    def get(self, operation_id: str) -> Operation:
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        raise ParameterError(f"找不到工序 {operation_id!r}")

    def enabled_operations(self) -> list[Operation]:
        return [item for item in self.ordered() if item.enabled]

    def ordered(self) -> list[Operation]:
        return sorted(self.operations, key=lambda item: (item.sequence, item.created_at))

    def next_sequence(self) -> int:
        return (max((item.sequence for item in self.operations), default=-1) + 1)

    def templates_for(self, kind: str) -> list[ParameterTemplate]:
        return [item for item in self.templates if item.kind == kind]

    def template(self, template_id: str) -> ParameterTemplate:
        for item in self.templates:
            if item.template_id == template_id:
                return item
        raise ParameterError(f"找不到参数模板 {template_id!r}")

    # -- 变更 --------------------------------------------------------------
    def add(self, operation: Operation) -> Operation:
        if any(item.operation_id == operation.operation_id for item in self.operations):
            raise ParameterError(f"工序 id 重复：{operation.operation_id!r}")
        operation.sequence = self.next_sequence()
        self.operations.append(operation)
        return operation

    def remove(self, operation_id: str) -> Operation:
        operation = self.get(operation_id)
        self.operations.remove(operation)
        self.resequence()
        return operation

    def resequence(self) -> None:
        """按**当前列表顺序**重排序号。

        注意：不能先 ``ordered()`` 再赋新序号——``ordered()`` 是按旧序号排序的，
        那样等于把刚调整好的顺序又还原回去（早先的 move 就是因为这个而不生效）。
        """

        for index, operation in enumerate(self.operations):
            operation.sequence = index

    def move(self, operation_id: str, target: int) -> list[Operation]:
        """把工序挪到目标序号（其余工序顺次调整）。"""

        ordered = self.ordered()
        index = next((position for position, item in enumerate(ordered)
                      if item.operation_id == operation_id), None)
        if index is None:
            raise ParameterError(f"找不到工序 {operation_id!r}")
        target = int(max(0, min(target, len(ordered) - 1)))
        item = ordered.pop(index)
        ordered.insert(target, item)
        self.operations = ordered
        self.resequence()
        return self.ordered()

    def update(self, operation_id: str, *, name: str | None = None,
               kind: str | None = None,
               parameters: Mapping[str, Any] | None = None,
               face_ids: Sequence[int] | None = None,
               enabled: bool | None = None,
               state: str | None = None) -> Operation:
        operation = self.get(operation_id)
        if name is not None:
            operation.name = str(name)
        if kind is not None:
            # 工序的加工类型是可以改的（平面铣 ↔ 型腔铣）：改了要退回"待生成"，
            # 否则树上还挂着上一种工艺算出来的刀路统计，会误导用户。
            new_kind = str(kind)
            if new_kind != operation.kind:
                operation.kind = new_kind
                operation.mark_draft()
        if parameters is not None:
            operation.parameters = dict(parameters)
            operation.mark_draft()
        if face_ids is not None:
            operation.face_ids = tuple(int(item) for item in face_ids)
            operation.mark_draft()
        if enabled is not None:
            operation.enabled = bool(enabled)
            operation.updated_at = _now()
        if state is not None:
            operation.state = str(state)
        if state is None and (name is not None or enabled is not None):
            operation.updated_at = _now()
        return operation

    def add_template(self, template: ParameterTemplate) -> ParameterTemplate:
        if any(item.template_id == template.template_id for item in self.templates):
            raise ParameterError(f"参数模板 id 重复：{template.template_id!r}")
        self.templates.append(template)
        return template

    def remove_template(self, template_id: str) -> None:
        template = self.template(template_id)
        if template.builtin:
            raise ParameterError("内置模板不能删除")
        self.templates.remove(template)

    # -- 序列化 ------------------------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        return {
            "operations": [item.to_payload() for item in self.ordered()],
            "templates": [item.to_payload() for item in self.templates],
            "count": len(self.operations),
            "enabled_count": len(self.enabled_operations()),
        }


__all__ = [
    "FACE_SELECTION_KINDS",
    "OPERATION_KIND_LABELS",
    "OPERATION_STATE_LABELS",
    "SURFACE_KINDS",
    "SURFACE_STRATEGIES",
    "Operation",
    "OperationKind",
    "OperationState",
    "OperationTree",
    "ParameterTemplate",
]
