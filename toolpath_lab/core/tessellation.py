"""离散模型的中立数据结构：三角网格 + 面拓扑。

这一层**不认识任何 CAD 格式**，也不做离散 —— 它是 ``brep``（OCP）与上层
（``core.part`` / ``cam`` / ``storage`` / 前端载荷）之间的唯一契约。

谁产生它：``toolpath_lab.brep``（OCP 离散完之后填进来）。
谁消费它：``core.part``（零件模型）、``storage``（存盘/读盘）、``cam``（按面取加工区域）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray


@dataclass(slots=True)
class FaceRecord:
    """一个面的拓扑与显示信息。

    ``triangle_start`` / ``triangle_count`` 指向 :class:`TessellatedModel` 的索引数组，
    因此每个面都能独立高亮、独立参与拾取——平面铣的特征选择就靠它。
    """

    id: int
    surface_kind: str
    triangle_start: int
    triangle_count: int
    area_mm2: float = 0.0
    normal: tuple[float, float, float] = (0.0, 0.0, 1.0)
    boundary_points: int = 0
    loop_count: int = 0
    shell_id: int = 0
    is_planar: bool = False
    #: 平面面的法向与到原点距离（平面方程 n·p = d），方便后续做特征识别。
    plane: tuple[float, float, float, float] | None = None
    bounds: tuple[float, float, float, float, float, float] | None = None
    #: 该面的离散是否只能近似（记录给用户）。
    approximate: bool = False
    #: 边界环的三维折线（世界坐标）。型腔铣的加工区域、特征轮廓都直接用它。
    loops: tuple[NDArray[np.float64], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "surface": self.surface_kind,
            "triangle_start": self.triangle_start,
            "triangle_count": self.triangle_count,
            "area_mm2": round(self.area_mm2, 4),
            "normal": [round(value, 5) for value in self.normal],
            "planar": self.is_planar,
            "loops": self.loop_count,
            "shell": self.shell_id,
        }
        if self.plane is not None:
            payload["plane"] = [round(value, 6) for value in self.plane]
        if self.bounds is not None:
            payload["bounds"] = [round(value, 4) for value in self.bounds]
        if self.approximate:
            payload["approximate"] = True
        return payload


@dataclass(slots=True)
class TessellatedModel:
    """一次模型导入的结果：三角网格 + 面拓扑 + 统计信息。"""

    positions: NDArray[np.float64]  # (V, 3)
    indices: NDArray[np.int64]  # (T, 3)
    normals: NDArray[np.float64]  # (T, 3) 三角面法向（离散后重算）
    face_of_triangle: NDArray[np.int64]  # (T,) 每个三角形属于哪个面
    faces: list[FaceRecord] = field(default_factory=list)
    vertices: NDArray[np.float64] = field(default_factory=lambda: np.zeros((0, 3)))
    edges: list[dict[str, Any]] = field(default_factory=list)
    file_info: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_name: str = ""

    # -- 基本量 ------------------------------------------------------------
    @property
    def triangle_count(self) -> int:
        return int(self.indices.shape[0])

    @property
    def vertex_count(self) -> int:
        return int(self.positions.shape[0])

    @property
    def face_count(self) -> int:
        return len(self.faces)

    def bounds(self) -> tuple[float, float, float, float, float, float]:
        """模型包围盒 (x_min, y_min, z_min, x_max, y_max, z_max)，空模型返回全零。"""

        if self.positions.size == 0:
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        low = self.positions.min(axis=0)
        high = self.positions.max(axis=0)
        return (float(low[0]), float(low[1]), float(low[2]),
                float(high[0]), float(high[1]), float(high[2]))

    def size(self) -> tuple[float, float, float]:
        low_x, low_y, low_z, high_x, high_y, high_z = self.bounds()
        return (high_x - low_x, high_y - low_y, high_z - low_z)

    def center(self) -> tuple[float, float, float]:
        low_x, low_y, low_z, high_x, high_y, high_z = self.bounds()
        return (0.5 * (low_x + high_x), 0.5 * (low_y + high_y), 0.5 * (low_z + high_z))

    def translation_to_origin(self) -> NDArray[np.float64]:
        """把模型搬到"XY 中心在原点、Z 最低在 0"的机床坐标系所需平移量。

        毛坯计算与刀路生成都假设这个坐标系，导入时统一一次，后面就不用再想。
        """

        low_x, low_y, low_z, high_x, high_y, high_z = self.bounds()
        return np.array([-0.5 * (low_x + high_x), -0.5 * (low_y + high_y), -low_z],
                        dtype=np.float64)

    def translated(self, offset: NDArray[np.float64]) -> None:
        """就地把网格、边界点与面边界环平移。"""

        delta = np.asarray(offset, dtype=np.float64).reshape(3)
        if not np.any(np.abs(delta) > 1e-12):
            return
        self.positions = self.positions + delta
        self.vertices = self.vertices + delta
        for face in self.faces:
            if face.loops:
                face.loops = tuple(loop + delta for loop in face.loops)
            if face.bounds is not None:
                x0, y0, z0, x1, y1, z1 = face.bounds
                face.bounds = (x0 + delta[0], y0 + delta[1], z0 + delta[2],
                               x1 + delta[0], y1 + delta[1], z1 + delta[2])
            if face.plane is not None:
                nx, ny, nz, d = face.plane
                face.plane = (nx, ny, nz, d + float(np.dot(delta, (nx, ny, nz))))

    def triangle_areas(self) -> NDArray[np.float64]:
        if self.triangle_count == 0:
            return np.zeros(0, dtype=np.float64)
        corners = self.positions[self.indices]
        return 0.5 * np.linalg.norm(
            np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
        )

    def surface_area_mm2(self) -> float:
        return float(self.triangle_areas().sum())

    def open_edge_count(self, tolerance: float = 1e-6) -> int:
        """没有被反向配对的边数：0 表示网格闭合（水密）。"""

        if self.triangle_count == 0:
            return 0
        quantized = np.round(self.positions / tolerance).astype(np.int64)
        # numpy 2.x 的 return_inverse 保持输入形状 (V, 3)，必须 reshape(-1) 才能当顶点编号用。
        _, inverse = np.unique(quantized, axis=0, return_inverse=True)
        inverse = np.asarray(inverse).reshape(-1)
        counts: dict[tuple[int, int], int] = {}
        for triangle in self.indices:
            corners = inverse[triangle]
            for index in range(3):
                key = (int(corners[index]), int(corners[(index + 1) % 3]))
                counts[key] = counts.get(key, 0) + 1
        return sum(
            1 for (start, end), count in counts.items() if count != counts.get((end, start), 0)
        )

    def is_watertight(self, tolerance: float = 1e-6) -> bool:
        """网格是否基本闭合（参考诊断；统计与仿真不依赖它）。"""

        if self.triangle_count == 0:
            return False
        return self.open_edge_count(tolerance) // 2 < 0.2 * self.triangle_count

    def volume_mm3(self) -> float:
        """闭合网格的定向体积（散度定理）；非闭合网格上只作为参考值。

        想要精确体积请用 ``BrepModel.volume_mm3()`` —— 那是 OpenCascade 的解析计算。
        """

        if self.triangle_count == 0:
            return 0.0
        corners = self.positions[self.indices]
        return float(np.einsum("ij,ij->i", corners[:, 0],
                               np.cross(corners[:, 1], corners[:, 2])).sum() / 6.0)

    def face_normals_grouped(self) -> dict[str, int]:
        """表面类型 -> 面数量，用于给用户报告"模型里有什么"。"""

        counts: dict[str, int] = {}
        for face in self.faces:
            counts[face.surface_kind] = counts.get(face.surface_kind, 0) + 1
        return counts

    def smooth_normals(self) -> NDArray[np.float64]:
        """逐顶点法向（同位置顶点求平均），用于曲面的平滑着色。"""

        if self.vertex_count == 0 or self.triangle_count == 0:
            return np.zeros_like(self.positions)
        normals = np.zeros_like(self.positions)
        for corner in range(3):
            np.add.at(normals, self.indices[:, corner], self.normals)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        return normals / np.where(lengths > 1e-12, lengths, 1.0)

    def statistics(self) -> dict[str, Any]:
        size = self.size()
        return {
            "vertices": self.vertex_count,
            "triangles": self.triangle_count,
            "faces": self.face_count,
            "edges": len(self.edges),
            "volume_mm3": round(self.volume_mm3(), 3),
            "area_mm2": round(self.surface_area_mm2(), 3),
            "size_mm": [round(value, 4) for value in size],
            "surface_kinds": self.face_normals_grouped(),
            "watertight": self.is_watertight(),
            "open_edges": self.open_edge_count() // 2,
        }

    def to_payload(self, *, max_triangles: int | None = None) -> dict[str, Any]:
        """给前端的网格描述（三角形索引与顶点位置分开存，前端重建 BufferGeometry）。"""

        indices = self.indices
        positions = self.positions
        if max_triangles is not None and self.triangle_count > max_triangles:
            keep = np.linspace(0, self.triangle_count - 1, max_triangles).astype(np.int64)
            indices = indices[keep]
        bounds = self.bounds()
        return {
            "positions": [[round(float(value), 4) for value in row] for row in positions],
            "indices": indices.reshape(-1).astype(int).tolist(),
            "normals": [[round(float(value), 5) for value in row] for row in self.normals],
            "face_of_triangle": self.face_of_triangle.astype(int).tolist(),
            "faces": [face.to_payload() for face in self.faces],
            "bounds": [round(value, 4) for value in bounds],
            "statistics": self.statistics(),
            "warnings": list(self.warnings),
            "file": dict(self.file_info),
            "name": self.source_name,
        }


__all__ = ["FaceRecord", "TessellatedModel"]
