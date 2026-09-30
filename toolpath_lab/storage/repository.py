"""工程持久化：JSON 文件仓库。

一个"工程"就是一次 CAM 会话的全部状态：导入的模型、毛坯、参数与工序树。
存储刻意做成**纯文件**（每个工程一个目录 + 一个 JSON 索引），不引入数据库：

- 工程文件可以直接看、可以直接备份、可以进版本控制；
- 服务重启后状态还在，界面刷新不丢工序；
- 模型网格单独存成 ``.npz``，避免把几十万个浮点数塞进 JSON。

目录结构::

    <root>/
      index.json              工程清单（id / 名称 / 时间）
      <project_id>/
        project.json          零件元数据 + 毛坯 + 工序树 + 参数模板
        mesh.npz              三角网格（positions / indices / normals / 面分组）

写入是"先写临时文件再替换"，因此进程被强杀也不会留下半截 JSON。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.operation import Operation, OperationTree, ParameterTemplate
from toolpath_lab.core.part import PartBounds, PartModel
from toolpath_lab.core.stock import CylindricalStock, RectangularStock, Stock, build_stock
from toolpath_lab.core.tessellation import FaceRecord, TessellatedModel

logger = logging.getLogger(__name__)

#: 工程 id 只允许这些字符，避免路径穿越。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
#: 工程文件格式版本。
FORMAT_VERSION = 1


def _now() -> str:
    # 带上微秒：工程清单按更新时间倒序，同一秒内连续保存两次（测试与脚本里很常见）
    # 只有秒级精度时会分不出先后。
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:  # pragma: no cover - 清理临时文件
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


@dataclass(slots=True)
class Project:
    """一个 CAM 工程。"""

    project_id: str
    name: str
    part: PartModel
    tree: OperationTree = field(default_factory=OperationTree)
    stock_id: str = "rectangular"
    stock_parameters: dict[str, Any] = field(default_factory=dict)
    cam_parameters: dict[str, Any] = field(default_factory=dict)
    controller_parameters: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # -- 毛坯 --------------------------------------------------------------
    def stock(self) -> Stock:
        return build_stock(self.stock_id, self.part, self.stock_parameters)

    def stock_bounds_payload(self) -> dict[str, Any]:
        return self.stock().bounds.to_payload()

    # -- 摘要 --------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return {
            "id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "part": {
                "name": self.part.name,
                "faces": self.part.face_count,
                "triangles": self.part.triangle_count,
                "size_mm": [round(value, 3) for value in self.part.size],
            },
            "stock": {"id": self.stock_id, "parameters": dict(self.stock_parameters)},
            "operations": len(self.tree.operations),
        }

    def to_payload(self, *, include_mesh: bool = True) -> dict[str, Any]:
        return {
            "format": FORMAT_VERSION,
            "id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "part": self.part.to_payload(include_mesh=include_mesh),
            "stock": {"id": self.stock_id, "parameters": dict(self.stock_parameters)},
            "cam_parameters": dict(self.cam_parameters),
            "controller_parameters": dict(self.controller_parameters),
            "tree": self.tree.to_payload(),
        }


class ProjectRepository:
    """文件仓库：工程的增删改查。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    # -- 清单 --------------------------------------------------------------
    def index(self) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = data.get("projects") if isinstance(data, dict) else None
        return list(items) if isinstance(items, list) else []

    def _write_index(self, items: list[dict[str, Any]]) -> None:
        _atomic_write(self.index_path, json.dumps(
            {"format": FORMAT_VERSION, "projects": items}, ensure_ascii=False, indent=2
        ))

    def _touch_index(self, project: Project) -> None:
        items = [item for item in self.index() if item.get("id") != project.project_id]
        items.append({
            "id": project.project_id,
            "name": project.name,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "part_name": project.part.name,
            "operations": len(project.tree.operations),
        })
        items.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")),
                   reverse=True)
        self._write_index(items)

    # -- 路径 --------------------------------------------------------------
    def _dir(self, project_id: str) -> Path:
        if not _ID_PATTERN.match(str(project_id)):
            raise ParameterError(f"非法的工程 id：{project_id!r}")
        return self.root / str(project_id)

    # -- 创建 / 读取 -------------------------------------------------------
    @staticmethod
    def new_id(prefix: str = "proj") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def save(self, project: Project) -> Project:
        project.updated_at = _now()
        directory = self._dir(project.project_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = project.to_payload(include_mesh=False)
        _atomic_write(directory / "project.json", json.dumps(payload, ensure_ascii=False, indent=2))
        self._save_mesh(directory / "mesh.npz", project.part)
        self._touch_index(project)
        return project

    def _save_mesh(self, path: Path, part: PartModel) -> None:
        mesh = part.mesh
        face_ids = np.asarray([face.id for face in mesh.faces], dtype=np.int64)
        face_kinds = np.asarray([face.surface_kind for face in mesh.faces], dtype=object)
        face_planes = np.asarray(
            [face.plane if face.plane is not None else (0.0, 0.0, 0.0, 0.0) for face in mesh.faces],
            dtype=np.float64,
        ).reshape(-1, 4)
        face_loops: list[np.ndarray] = []
        for face in mesh.faces:
            for loop in face.loops:
                face_loops.append(np.asarray(loop, dtype=np.float64))
        # 每条环用 (face_index, offset, count) 定位
        loop_offsets: list[tuple[int, int, int]] = []
        cursor = 0
        for face_index, face in enumerate(mesh.faces):
            for loop in face.loops:
                array = np.asarray(loop, dtype=np.float64)
                loop_offsets.append((face_index, cursor, array.shape[0]))
                cursor += array.shape[0]
        np.savez_compressed(
            path,
            positions=np.asarray(mesh.positions, dtype=np.float64),
            indices=np.asarray(mesh.indices, dtype=np.int64),
            normals=np.asarray(mesh.normals, dtype=np.float64),
            face_of_triangle=np.asarray(mesh.face_of_triangle, dtype=np.int64),
            face_ids=face_ids,
            face_kinds=face_kinds,
            face_planes=face_planes,
            face_tri_start=np.asarray([face.triangle_start for face in mesh.faces], dtype=np.int64),
            face_tri_count=np.asarray([face.triangle_count for face in mesh.faces], dtype=np.int64),
            face_areas=np.asarray([face.area_mm2 for face in mesh.faces], dtype=np.float64),
            face_normals=np.asarray([face.normal for face in mesh.faces], dtype=np.float64).reshape(-1, 3),
            face_planar=np.asarray([face.is_planar for face in mesh.faces], dtype=bool),
            face_loop_offsets=np.asarray(loop_offsets, dtype=np.int64).reshape(-1, 3),
            face_loops=(
                np.vstack(face_loops) if face_loops else np.zeros((0, 3), dtype=np.float64)
            ),
        )

    # -- 原始模型文件 ------------------------------------------------------
    def save_model(self, project_id: str, data: bytes, *, filename: str = "") -> Path | None:
        """把上传的原始模型文件也存一份。

        网格文件（``mesh.npz``）里只有三角形，没有 BRep 拓扑，而**等高铣要按层剖切 BRep**，
        所以工程必须留住原始文件。存不下（IO 出错）时返回 None —— 工程本身仍然可用，
        只是重新打开后等高铣会提示重新导入模型。
        """

        if not data:
            return None
        directory = self._dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("source.*"):
            try:
                old.unlink()
            except OSError:  # pragma: no cover - 文件被占用等
                pass
        suffix = Path(filename or "").suffix.lower() or ".step"
        path = directory / f"source{suffix}"
        try:
            path.write_bytes(data)
        except OSError as error:  # pragma: no cover - 磁盘满/权限
            logger.warning("模型源文件没能存进工程 %s：%s", project_id, error)
            return None
        return path

    def model_path(self, project_id: str) -> Path | None:
        """工程里保存的原始模型文件（可能是 ``.step`` / ``.stp`` / ``.iges`` / ``.igs``）。"""

        try:
            directory = self._dir(project_id)
        except ParameterError:
            return None
        for item in sorted(directory.glob("source.*")):
            if item.is_file() and item.stat().st_size > 0:
                return item
        return None

    def load(self, project_id: str) -> Project:
        directory = self._dir(project_id)
        project_file = directory / "project.json"
        if not project_file.is_file():
            raise ParameterError(f"工程不存在：{project_id}")
        payload = json.loads(project_file.read_text(encoding="utf-8"))
        part = self._load_part(directory, payload)
        tree = OperationTree()
        tree_payload = payload.get("tree") or {}
        for item in tree_payload.get("operations") or []:
            tree.add(Operation(
                operation_id=str(item.get("id")),
                name=str(item.get("name") or ""),
                kind=str(item.get("kind")),
                parameters=dict(item.get("parameters") or {}),
                face_ids=tuple(int(value) for value in (item.get("faces") or [])),
                enabled=bool(item.get("enabled", True)),
                state=str(item.get("state") or "draft"),
                statistics=dict(item.get("statistics") or {}),
                warnings=tuple(item.get("warnings") or ()),
            ))
        for item in tree_payload.get("templates") or []:
            tree.add_template(ParameterTemplate(
                template_id=str(item.get("id")),
                name=str(item.get("name") or ""),
                kind=str(item.get("kind") or ""),
                parameters=dict(item.get("parameters") or {}),
                builtin=bool(item.get("builtin", False)),
            ))
        tree.resequence()
        return Project(
            project_id=str(payload.get("id") or project_id),
            name=str(payload.get("name") or project_id),
            part=part,
            tree=tree,
            stock_id=str((payload.get("stock") or {}).get("id") or "rectangular"),
            stock_parameters=dict((payload.get("stock") or {}).get("parameters") or {}),
            cam_parameters=dict(payload.get("cam_parameters") or {}),
            controller_parameters=dict(payload.get("controller_parameters") or {}),
            created_at=str(payload.get("created_at") or _now()),
            updated_at=str(payload.get("updated_at") or _now()),
        )

    def _load_part(self, directory: Path, payload: Mapping[str, Any]) -> PartModel:
        part_payload = payload.get("part") or {}
        mesh_path = directory / "mesh.npz"
        if not mesh_path.is_file():
            raise ParameterError(f"工程缺少网格文件：{mesh_path.name}")
        with np.load(mesh_path, allow_pickle=True) as data:
            positions = np.asarray(data["positions"], dtype=np.float64)
            indices = np.asarray(data["indices"], dtype=np.int64)
            normals = np.asarray(data["normals"], dtype=np.float64)
            face_of_triangle = np.asarray(data["face_of_triangle"], dtype=np.int64)
            face_ids = np.asarray(data["face_ids"], dtype=np.int64)
            face_kinds = [str(item) for item in data["face_kinds"]]
            face_planes = np.asarray(data["face_planes"], dtype=np.float64)
            tri_start = np.asarray(data["face_tri_start"], dtype=np.int64)
            tri_count = np.asarray(data["face_tri_count"], dtype=np.int64)
            areas = np.asarray(data["face_areas"], dtype=np.float64)
            face_normals = np.asarray(data["face_normals"], dtype=np.float64)
            planar = np.asarray(data["face_planar"], dtype=bool)
            loop_offsets = np.asarray(data["face_loop_offsets"], dtype=np.int64).reshape(-1, 3)
            loops_flat = np.asarray(data["face_loops"], dtype=np.float64)

        loops_by_face: dict[int, list[np.ndarray]] = {}
        for face_index, offset, count in loop_offsets:
            loops_by_face.setdefault(int(face_index), []).append(
                loops_flat[int(offset):int(offset) + int(count)]
            )
        faces: list[FaceRecord] = []
        for index in range(face_ids.shape[0]):
            plane = tuple(float(value) for value in face_planes[index])
            faces.append(FaceRecord(
                id=int(face_ids[index]),
                surface_kind=face_kinds[index],
                triangle_start=int(tri_start[index]),
                triangle_count=int(tri_count[index]),
                area_mm2=float(areas[index]),
                normal=tuple(float(value) for value in face_normals[index]),
                loop_count=len(loops_by_face.get(index, [])),
                is_planar=bool(planar[index]),
                plane=plane if any(abs(value) > 0 for value in plane) else None,
                loops=tuple(loops_by_face.get(index, [])),
            ))
        bounds_payload = part_payload.get("bounds") or {}
        bounds = _bounds_from_payload(bounds_payload, positions)
        mesh = TessellatedModel(
            positions=positions, indices=indices, normals=normals,
            face_of_triangle=face_of_triangle, faces=faces,
            file_info=dict(part_payload.get("source") or {}),
            warnings=list(part_payload.get("warnings") or []),
            source_name=str(part_payload.get("name") or ""),
        )
        part = PartModel(
            model_id=str(part_payload.get("id") or payload.get("id") or "part"),
            name=str(part_payload.get("name") or "part"),
            mesh=mesh,
            bounds=bounds,
            source=dict(part_payload.get("source") or {}),
            warnings=list(part_payload.get("warnings") or []),
        )
        from toolpath_lab.core.part import describe_face

        part.features = [describe_face(record) for record in mesh.faces]
        return part

    # -- 删除 --------------------------------------------------------------
    def delete(self, project_id: str) -> bool:
        directory = self._dir(project_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        items = [item for item in self.index() if item.get("id") != project_id]
        self._write_index(items)
        return True

    def exists(self, project_id: str) -> bool:
        return (self._dir(project_id) / "project.json").is_file()


def _bounds_from_payload(payload: Mapping[str, Any],
                         positions: np.ndarray) -> PartBounds:
    x = payload.get("x")
    y = payload.get("y")
    z = payload.get("z")
    if isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(y, (list, tuple)) \
            and len(y) == 2 and isinstance(z, (list, tuple)) and len(z) == 2:
        return PartBounds(float(x[0]), float(y[0]), float(z[0]),
                          float(x[1]), float(y[1]), float(z[1]))
    if positions.size:
        low = positions.min(axis=0)
        high = positions.max(axis=0)
        return PartBounds.from_array(low, high)
    return PartBounds(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


__all__ = ["FORMAT_VERSION", "Project", "ProjectRepository"]
