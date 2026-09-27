"""工作空间：一个会话里"当前打开的工程"。

界面上的操作都是"对当前工程"的：导入模型 → 建毛坯 → 加工序 → 生成刀路 → 仿真。
把这些串起来的状态放在这里，HTTP 层只做翻译，不保存业务状态。

刻意做成进程内单例（每个服务实例一个工作空间）：桌面端只有一个人在用，
引入多用户会话只会让调试变复杂。工程本身已经落盘，重启后可以重新打开。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from toolpath_lab.cam import parameters as cam_parameters_module
from toolpath_lab.cam.service import (CAMOperationRequest, CAMOperationResult,
                                      execute_operation, surface_defaults_for,
                                      surface_parameters_for)
from toolpath_lab.brep import import_model_bytes_full, import_model_full
from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.operation import (OPERATION_KIND_LABELS, SURFACE_KINDS, Operation,
                                         OperationTree, ParameterTemplate)
from toolpath_lab.core.part import PartModel
from toolpath_lab.core.stock import build_stock, stock_catalog
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.server.catalog import default_operation_parameters
from toolpath_lab.storage.repository import Project, ProjectRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Workspace:
    """当前打开的工程 + 文件仓库。"""

    repository: ProjectRepository
    project: Project | None = None
    #: 最近一次各工序的规划结果（工序 id -> 结果），供仿真与导出复用
    results: dict[str, CAMOperationResult] = field(default_factory=dict)
    #: 当前工程的 BRep 模型（**只有它才能按层剖切**，等高铣要用）。
    #: 网格里没有拓扑，所以这份数据必须来自原始 STEP/IGES 文件，而不是 mesh.npz。
    brep: Any = None

    # -- 导入 --------------------------------------------------------------
    def import_step(self, data: bytes, *, filename: str = "",
                    name: str = "") -> Project:
        """导入 STEP / IGES 文件，建立（或替换）当前工程。"""

        imported = import_model_bytes_full(data, source_name=filename)
        model = imported.model
        from toolpath_lab.core.part import build_part

        project_id = ProjectRepository.new_id()
        part = build_part(
            model,
            model_id=project_id,
            name=name or Path(filename).stem or "导入零件",
            source={"filename": filename, "bytes": len(data)},
        )
        project = Project(
            project_id=project_id,
            name=path_title(filename) or part.name,
            part=part,
            stock_id="rectangular",
            stock_parameters=_default_stock_parameters("rectangular"),
            cam_parameters=default_operation_parameters(),
            controller_parameters=cam_parameters_module.controller_parameters().defaults(),
        )
        self.repository.save(project)
        # 原始文件也留一份：等高铣要剖切 BRep，而网格文件里没有拓扑。
        self.repository.save_model(project_id, data, filename=filename)
        self.brep = imported.brep
        self.project = project
        self.results.clear()
        return project

    # -- 查询 --------------------------------------------------------------
    def require_project(self) -> Project:
        if self.project is None:
            raise ParameterError("还没有打开工程：请先导入 STEP 模型")
        return self.project

    def open(self, project_id: str) -> Project:
        project = self.repository.load(project_id)
        self.project = project
        self.brep = self._reload_brep(project_id)
        self.results.clear()
        return project

    def _reload_brep(self, project_id: str) -> Any:
        """重新打开工程时把 BRep 读回来。

        读不回来（老工程没存源文件、文件坏了、没装 OCP）不是错误 ——
        2.5 轴工序照常工作，只有等高铣会给出明确的提示。
        """

        path = self.repository.model_path(project_id)
        if path is None:
            return None
        try:
            return import_model_full(path, name=self.project.part.name if self.project else "").brep
        except Exception as error:  # noqa: BLE001 - 任何读取失败都只是让等高铣不可用
            logger.warning("工程 %s 的 BRep 没能读回来：%s", project_id, error)
            return None

    def close(self) -> None:
        self.project = None
        self.brep = None
        self.results.clear()

    # -- 毛坯 --------------------------------------------------------------
    def set_stock(self, stock_id: str, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        project = self.require_project()
        stock = build_stock(stock_id, project.part, parameters)
        project.stock_id = stock.id
        project.stock_parameters = stock.to_params()
        self.repository.save(project)
        return self.stock_payload()

    def stock_payload(self) -> dict[str, Any]:
        project = self.require_project()
        stock = project.stock()
        mesh = stock.build_mesh()
        residual = stock.residual_mm(project.part)
        return {
            "id": stock.id,
            "label": stock.label,
            "parameters": stock.to_params(),
            "bounds": stock.bounds.to_payload(),
            "volume_mm3": round(stock.volume_mm3(), 3),
            "residual_mm": [round(value, 3) for value in residual],
            "mesh": mesh.to_payload(),
            "catalog": stock_catalog(),
        }

    # -- 工序 --------------------------------------------------------------
    def add_operation(self, kind: str, face_ids: Iterable[int],
                      parameters: Mapping[str, Any] | None = None,
                      name: str = "") -> tuple[Operation, CAMOperationResult | None]:
        project = self.require_project()
        if kind not in OPERATION_KIND_LABELS:
            raise ParameterError(
                f"未知的加工类型 {kind!r}；可选：{', '.join(sorted(OPERATION_KIND_LABELS))}"
            )
        faces = _coerce_faces(face_ids)
        if not faces and kind not in SURFACE_KINDS:
            raise ParameterError("请先选择至少一个加工面")
        values = self._parameter_baseline(kind)
        if parameters:
            # 切类型时旧类型的参数会被前端一并送上来，这里只收本类型认识的键，
            # 否则一道等高铣工序的参数里会混进平面铣的 stepover_mm。
            values.update(self._filter_parameters(kind, parameters))
        validated = self._coerce_parameters(kind, values)
        label = OPERATION_KIND_LABELS.get(kind, kind)
        operation = Operation(
            operation_id=ProjectRepository.new_id("op"),
            name=name or self._unique_name(label),
            kind=kind,
            parameters=validated,
            face_ids=tuple(faces),
        )
        project.tree.add(operation)
        result = self.generate(operation.operation_id, save=False)
        self.repository.save(project)
        return operation, result

    # -- 参数按类型分流 ----------------------------------------------------
    def _parameter_baseline(self, kind: str) -> dict[str, Any]:
        """新工序的起始参数：曲面工序有自己的默认值。"""

        project = self.require_project()
        if kind in SURFACE_KINDS:
            return surface_defaults_for(kind)
        return dict(project.cam_parameters)

    def _parameter_set(self, kind: str):
        if kind in SURFACE_KINDS:
            return surface_parameters_for(kind)
        return cam_parameters_module.cam_parameters()

    def _filter_parameters(self, kind: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        known = {item.key for item in self._parameter_set(kind).specs}
        return {key: value for key, value in parameters.items() if key in known}

    def _coerce_parameters(self, kind: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._parameter_set(kind).coerce(values))

    def _unique_name(self, base: str) -> str:
        project = self.require_project()
        existing = {item.name for item in project.tree.operations}
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def update_operation(self, operation_id: str, **changes: Any) -> Operation:
        project = self.require_project()
        target = project.tree.get(operation_id)
        kind = str(changes.get("kind") or target.kind)
        if "parameters" in changes and changes["parameters"] is not None:
            values = {**self._parameter_baseline(kind),
                      **self._filter_parameters(kind, changes["parameters"])}
            changes["parameters"] = self._coerce_parameters(kind, values)
        if "face_ids" in changes and changes["face_ids"] is not None:
            changes["face_ids"] = _coerce_faces(changes["face_ids"])
        operation = project.tree.update(operation_id, **changes)
        self.results.pop(operation_id, None)
        self.repository.save(project)
        return operation

    def remove_operation(self, operation_id: str) -> None:
        project = self.require_project()
        project.tree.remove(operation_id)
        self.results.pop(operation_id, None)
        self.repository.save(project)

    def move_operation(self, operation_id: str, sequence: int) -> list[Operation]:
        project = self.require_project()
        ordered = project.tree.move(operation_id, sequence)
        self.repository.save(project)
        return ordered

    def duplicate_operation(self, operation_id: str) -> Operation:
        project = self.require_project()
        source = project.tree.get(operation_id)
        clone = Operation(
            operation_id=ProjectRepository.new_id("op"),
            name=self._unique_name(source.name),
            kind=source.kind,
            parameters=dict(source.parameters),
            face_ids=tuple(source.face_ids),
        )
        project.tree.add(clone)
        self.repository.save(project)
        return clone

    def generate(self, operation_id: str, *, save: bool = True) -> CAMOperationResult:
        project = self.require_project()
        operation = project.tree.get(operation_id)
        request = CAMOperationRequest(
            kind=operation.kind,
            part=project.part,
            face_ids=tuple(operation.face_ids),
            parameters=dict(operation.parameters),
            tool=self._tool_for(operation),
            top_z=None,
            stock=project.stock(),
            # 等高铣要剖切 BRep；其它类型用不到，给了也无害（不参与计算）。
            brep=self.brep if operation.kind in SURFACE_KINDS else None,
        )
        result = execute_operation(request)
        operation.statistics = dict(result.toolpath.statistics())
        operation.warnings = tuple(result.warnings)
        operation.state = "generated"
        self.results[operation_id] = result
        if save:
            self.repository.save(project)
        return result

    def _tool_for(self, operation: Operation) -> Tool:
        """工序参数的刀具。

        2.5 轴只有平底刀（栅格距离场就是按平底刀建的）；
        曲面工序的刀具类型由参数决定，球头刀与圆鼻刀在 ``surfacing`` 里是真的支持。
        """

        if operation.kind in SURFACE_KINDS:
            values = operation.parameters
            return Tool(
                kind=ToolKind(str(values.get("tool_kind") or ToolKind.FLAT.value)),
                diameter_mm=float(values.get("tool_diameter_mm", 6.0)),
                length_mm=float(values.get("tool_length_mm", 40.0)),
            )
        return cam_parameters_module.tool_from_cam_parameters(operation.parameters)

    def generate_all(self, *, save: bool = True) -> list[dict[str, Any]]:
        """按工序顺序生成全部启用的工序。"""

        project = self.require_project()
        summary: list[dict[str, Any]] = []
        for operation in project.tree.enabled_operations():
            try:
                result = self.generate(operation.operation_id, save=False)
                summary.append({
                    "id": operation.operation_id,
                    "name": operation.name,
                    "ok": True,
                    "statistics": dict(operation.statistics),
                    "warnings": list(operation.warnings),
                })
            except Exception as error:  # 单道工序失败不该中断整条工序链
                operation.state = "draft"
                operation.warnings = (str(error),)
                summary.append({
                    "id": operation.operation_id,
                    "name": operation.name,
                    "ok": False,
                    "error": str(error),
                })
        if save:
            self.repository.save(project)
        return summary

    def results_for(self, operation_id: str) -> CAMOperationResult:
        cached = self.results.get(operation_id)
        if cached is not None:
            return cached
        return self.generate(operation_id)

    # -- 参数模板 ----------------------------------------------------------
    def save_template(self, name: str, kind: str, parameters: Mapping[str, Any]) -> ParameterTemplate:
        project = self.require_project()
        template = ParameterTemplate(
            template_id=ProjectRepository.new_id("tpl"),
            name=name or "自定义模板",
            kind=kind,
            parameters=cam_parameters_module.cam_parameters().coerce(
                {**project.cam_parameters, **dict(parameters)}
            ),
        )
        project.tree.add_template(template)
        self.repository.save(project)
        return template

    def remove_template(self, template_id: str) -> None:
        project = self.require_project()
        project.tree.remove_template(template_id)
        self.repository.save(project)

    # -- 全局参数 ----------------------------------------------------------
    def set_parameters(self, *, cam: Mapping[str, Any] | None = None,
                       controller: Mapping[str, Any] | None = None) -> dict[str, Any]:
        project = self.require_project()
        if cam is not None:
            project.cam_parameters = cam_parameters_module.cam_parameters().coerce(
                {**project.cam_parameters, **dict(cam)}
            )
        if controller is not None:
            project.controller_parameters = cam_parameters_module.controller_parameters().coerce(
                {**project.controller_parameters, **dict(controller)}
            )
        self.repository.save(project)
        return self.parameters_payload()

    def parameters_payload(self) -> dict[str, Any]:
        project = self.require_project()
        return {
            "cam": dict(project.cam_parameters),
            "controller": dict(project.controller_parameters),
        }


def _coerce_faces(face_ids: Iterable[int]) -> list[int]:
    result: list[int] = []
    for item in face_ids:
        try:
            result.append(int(item))
        except (TypeError, ValueError) as error:
            raise ParameterError(f"面序号必须是整数（收到 {item!r}）") from error
    return result


def path_title(filename: str) -> str:
    stem = Path(filename).stem if filename else ""
    return stem.strip() or ""


def _default_stock_parameters(stock_id: str) -> dict[str, Any]:
    from toolpath_lab.core.stock import STOCK_TYPES

    try:
        cls = STOCK_TYPES.get(stock_id)
    except Exception:  # pragma: no cover - stock_id 由调用方保证合法
        return {}
    return cls.parameters.defaults()


__all__ = ["Workspace"]
