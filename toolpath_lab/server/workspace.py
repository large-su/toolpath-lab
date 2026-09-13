"""工作空间：一个会话里"当前打开的工程"。

界面上的操作都是"对当前工程"的：导入模型 → 建毛坯 → 加工序 → 生成刀路 → 仿真。
把这些串起来的状态放在这里，HTTP 层只做翻译，不保存业务状态。

刻意做成进程内单例（每个服务实例一个工作空间）：桌面端只有一个人在用，
引入多用户会话只会让调试变复杂。工程本身已经落盘，重启后可以重新打开。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from toolpath_lab.cam import parameters as cam_parameters_module
from toolpath_lab.cam.service import CAMOperationRequest, CAMOperationResult, execute_operation
from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.operation import OPERATION_KIND_LABELS, Operation, OperationTree, ParameterTemplate
from toolpath_lab.core.part import PartModel
from toolpath_lab.core.stock import build_stock, stock_catalog
from toolpath_lab.server.catalog import default_operation_parameters
from toolpath_lab.storage.repository import Project, ProjectRepository
from toolpath_lab.step.reader import read_step_bytes


@dataclass(slots=True)
class Workspace:
    """当前打开的工程 + 文件仓库。"""

    repository: ProjectRepository
    project: Project | None = None
    #: 最近一次各工序的规划结果（工序 id -> 结果），供仿真与导出复用
    results: dict[str, CAMOperationResult] = field(default_factory=dict)

    # -- 导入 --------------------------------------------------------------
    def import_step(self, data: bytes, *, filename: str = "",
                    name: str = "") -> Project:
        """导入 STEP 文件，建立（或替换）当前工程。"""

        model = read_step_bytes(data, source_name=filename)
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
        self.results.clear()
        return project

    def close(self) -> None:
        self.project = None
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
        faces = _coerce_faces(face_ids)
        if not faces:
            raise ParameterError("请先选择至少一个加工面")
        values = dict(project.cam_parameters)
        if parameters:
            values.update(parameters)
        validated = cam_parameters_module.cam_parameters().coerce(values)
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
        if "parameters" in changes and changes["parameters"] is not None:
            changes["parameters"] = cam_parameters_module.cam_parameters().coerce(
                {**project.cam_parameters, **dict(changes["parameters"])}
            )
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
            tool=cam_parameters_module.tool_from_cam_parameters(operation.parameters),
            top_z=None,
            stock=project.stock(),
        )
        result = execute_operation(request)
        operation.statistics = dict(result.toolpath.statistics())
        operation.warnings = tuple(result.warnings)
        operation.state = "generated"
        self.results[operation_id] = result
        if save:
            self.repository.save(project)
        return result

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
