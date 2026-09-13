"""零件模型：导入结果与后续所有工序共享的那一份数据。

一次导入产生一个 :class:`PartModel`：

- 原始三角网格（渲染用）与**平面面**列表（特征拾取用）；
- 包容盒、尺寸、体积等几何量；
- 统一到机床坐标系后的摆放（XY 居中、Z 最低为 0）。

后续的毛坯计算、特征识别、刀路生成、切削仿真都从这里取数据，而不是自己再解析一次
STEP。因此"换一种模型格式"只需要在 :mod:`toolpath_lab.step` 旁边再写一个 reader，
把结果塞进 PartModel 即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.step.tessellate import FaceRecord, TessellatedModel


@dataclass(frozen=True, slots=True)
class PartBounds:
    """轴对齐包容盒（机床坐标系）。"""

    x_min: float
    y_min: float
    z_min: float
    x_max: float
    y_max: float
    z_max: float

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.x_max - self.x_min, self.y_max - self.y_min, self.z_max - self.z_min)

    @property
    def center(self) -> tuple[float, float, float]:
        return (
            0.5 * (self.x_min + self.x_max),
            0.5 * (self.y_min + self.y_max),
            0.5 * (self.z_min + self.z_max),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "x": [round(self.x_min, 4), round(self.x_max, 4)],
            "y": [round(self.y_min, 4), round(self.y_max, 4)],
            "z": [round(self.z_min, 4), round(self.z_max, 4)],
            "size": [round(value, 4) for value in self.size],
            "center": [round(value, 4) for value in self.center],
        }

    @classmethod
    def from_array(cls, low: NDArray[np.float64], high: NDArray[np.float64]) -> "PartBounds":
        return cls(float(low[0]), float(low[1]), float(low[2]),
                   float(high[0]), float(high[1]), float(high[2]))


@dataclass(slots=True)
class PartModel:
    """一个导入的零件。"""

    model_id: str
    name: str
    mesh: TessellatedModel
    bounds: PartBounds
    source: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: 面的附加信息（面积、法向、平面方程），按面 id 索引，供特征拾取直接使用。
    features: list[dict[str, Any]] = field(default_factory=list)

    # -- 几何量 ------------------------------------------------------------
    @property
    def size(self) -> tuple[float, float, float]:
        return self.bounds.size

    @property
    def triangle_count(self) -> int:
        return self.mesh.triangle_count

    @property
    def face_count(self) -> int:
        return self.mesh.face_count

    def face(self, face_id: int) -> FaceRecord | None:
        for record in self.mesh.faces:
            if record.id == face_id:
                return record
        return None

    def top_z(self) -> float:
        return self.bounds.z_max

    def statistics(self) -> dict[str, Any]:
        return self.mesh.statistics()

    def to_payload(self, *, include_mesh: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.model_id,
            "name": self.name,
            "bounds": self.bounds.to_payload(),
            "size_mm": [round(value, 4) for value in self.size],
            "statistics": self.statistics(),
            "source": dict(self.source),
            "warnings": list(self.warnings),
            "faces": [face.to_payload() for face in self.mesh.faces],
            "features": [dict(item) for item in self.features],
        }
        if include_mesh:
            payload["mesh"] = self.mesh.to_payload()
        return payload


def build_part(model: TessellatedModel, *, model_id: str, name: str = "",
               source: dict[str, Any] | None = None) -> PartModel:
    """由 STEP 离散结果构造零件模型（并生成面特征摘要）。"""

    low = model.positions.min(axis=0)
    high = model.positions.max(axis=0)
    bounds = PartBounds.from_array(low, high)
    part = PartModel(
        model_id=model_id,
        name=name or model.source_name or model_id,
        mesh=model,
        bounds=bounds,
        source=dict(source or {}),
        warnings=list(model.warnings),
    )
    part.features = [describe_face(record) for record in model.faces]
    return part


def describe_face(record: FaceRecord) -> dict[str, Any]:
    """把一条面记录整理成前端拾取与特征选择需要的信息。"""

    normal = np.asarray(record.normal, dtype=np.float64).reshape(3)
    item: dict[str, Any] = {
        "face_id": record.id,
        "surface": record.surface_kind,
        "planar": record.is_planar,
        "area_mm2": round(record.area_mm2, 4),
        "normal": [round(float(value), 5) for value in normal],
        "triangle_start": record.triangle_start,
        "triangle_count": record.triangle_count,
        "loops": record.loop_count,
        "approximate": record.approximate,
    }
    if record.plane is not None:
        item["plane"] = [round(float(value), 6) for value in record.plane]
    if record.bounds is not None:
        item["bounds"] = [round(float(value), 4) for value in record.bounds]
    # 是否为"朝上的平面"：平面铣/型腔铣的典型加工面
    item["horizontal"] = bool(record.is_planar and normal[2] > 0.999)
    item["vertical"] = bool(record.is_planar and abs(normal[2]) < 0.001)
    return item


def stock_from_bounds(bounds: PartBounds, offsets: Sequence[float]) -> PartBounds:
    """按各轴偏移量在零件包容盒外扩，得到毛坯包容盒。"""

    if len(offsets) != 3:
        raise ParameterError("偏移量需要三个分量（X/Y/Z）")
    dx, dy, dz = (float(value) for value in offsets)
    if dx < 0 or dy < 0 or dz < 0:
        raise ParameterError("毛坯偏移量不能为负")
    return PartBounds(
        bounds.x_min - dx, bounds.y_min - dy, bounds.z_min,
        bounds.x_max + dx, bounds.y_max + dy, bounds.z_max + dz,
    )


__all__ = [
    "PartBounds",
    "PartModel",
    "build_part",
    "describe_face",
    "stock_from_bounds",
]
