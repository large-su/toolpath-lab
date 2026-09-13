"""STEP 面离散：把 B-rep 的每一张面变成三角形。

流程：实体图 → 面 → 边界环（沿边的曲线采样）→ 三角形。

三种离散路径，按面的类型自动选择：

**平面**
    边界投到平面局部坐标系，外环与内环（岛屿）合并后用耳切法三角化。
    这是机加工零件里最常见的一类面，因此做得最"实"：孔、凹多边形都正确。

**解析曲面（圆柱 / 圆锥 / 球 / 环面）**
    在参数域上按密度采样网格，用边界环在参数域里围出的多边形裁剪（单元中心判内外）。
    参数域用边界点的角度中位数解锁"接缝"问题，环绕不超过一圈的常见面都能正确裁剪。

**B 样条曲面**
    同样在参数域裁剪；离散密度由边界长度与控制网格规模共同决定。

无法离散的面（例如 OFFSET_SURFACE）不会让导入失败：记一条警告，跳过该面，
上层会在界面提示"该面未离散"。这样工业文件里偶尔出现的冷门实体不会阻塞整个流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, pi, sin
from typing import Any, Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.step.errors import StepUnsupportedError
from toolpath_lab.step.geometry import (
    BSplineSurface,
    Circle,
    CompositeCurve,
    Cone,
    Curve,
    Cylinder,
    EdgeRecord,
    Ellipse,
    Line,
    Plane,
    Resolver,
    Sphere,
    Surface,
    Torus,
    chord_count,
    edge_length_hint,
)
from toolpath_lab.step.parser import StepEntity, StepFile, as_bool, as_int, as_ref, as_sequence

#: 参数域网格的最大单元数（两个方向），控制单个面的三角形上限。
MAX_GRID_CELLS_PER_AXIS = 120
#: 单元中心落在边界外多远仍算命中（参数单位，用于吃掉离散误差）。
_INSIDE_TOLERANCE = 1e-9

#: 实体里被当作"壳"的实体关键字。
SHELL_KEYWORDS = (
    "CLOSED_SHELL",
    "OPEN_SHELL",
    "ORIENTED_CLOSED_SHELL",
    "ORIENTED_OPEN_SHELL",
    "CONNECTED_FACE_SET",
)
#: 实体里被当作"实体"的实体关键字。
SOLID_KEYWORDS = (
    "MANIFOLD_SOLID_BREP",
    "BREP_WITH_VOIDS",
    "FACETED_BREP",
    "SHELL_BASED_SURFACE_MODEL",
    "CSG_SOLID",
    "SOLID_MODEL",
)
#: 被当作"曲面"的实体关键字（作为兜底，正常路径靠关键字里含 SURFACE 判断）。
SURFACE_KEYWORDS = frozenset({
    "PLANE", "CYLINDRICAL_SURFACE", "CONICAL_SURFACE", "SPHERICAL_SURFACE",
    "TOROIDAL_SURFACE", "B_SPLINE_SURFACE", "B_SPLINE_SURFACE_WITH_KNOTS",
    "RATIONAL_B_SPLINE_SURFACE", "BEZIER_SURFACE", "UNIFORM_SURFACE",
    "SURFACE_OF_REVOLUTION", "SURFACE_OF_LINEAR_EXTRUSION", "SWEPT_SURFACE",
    "OFFSET_SURFACE", "CURVE_BOUNDED_SURFACE", "RECTANGULAR_TRIMMED_SURFACE",
})


def surface_argument(step: StepFile, face: StepEntity) -> Any:
    """取 ``ADVANCED_FACE`` 的曲面参数。

    规范里 ``ADVANCED_FACE`` 是 ``(name, bounds, face_geometry, same_sense)``，
    但不同导出器对可选 ``name`` 的处理并不一致（有的直接省掉），因此这里按
    "最后一个指向 SURFACE 实体的参数"来定位，而不是写死下标。
    """

    for index in range(len(face.arguments) - 1, -1, -1):
        entity = step.get(face.arg(index))
        if entity is None:
            continue
        if entity.keyword in SURFACE_KEYWORDS or entity.keyword.endswith("SURFACE"):
            return face.arg(index)
    return face.arg(2) if len(face.arguments) > 2 else None


def bounds_arguments(face: StepEntity) -> list[Any]:
    """取 ``ADVANCED_FACE`` 的边界环参数（通常是唯一的一个列表）。"""

    for argument in face.arguments:
        if isinstance(argument, (list, tuple)) and argument:
            return list(argument)
    return []


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
    #: 该面在参数域里的裁剪多边形是否因为退化而只能近似（记录给用户）。
    approximate: bool = False
    #: 边界环的三维折线（世界坐标，逆时针/顺时针由原文件决定）。
    #: 型腔铣的加工区域、特征轮廓都直接用它——重新从网格反推边界既慢又不可靠。
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
    """一次 STEP 导入的结果：三角网格 + 面拓扑 + 统计信息。"""

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
        return np.array([-0.5 * (low_x + high_x), -0.5 * (low_y + high_y), -low_z], dtype=np.float64)

    def translated(self, offset: NDArray[np.float64]) -> None:
        """就地把网格与边界点平移。"""

        delta = np.asarray(offset, dtype=np.float64).reshape(3)
        if not np.any(np.abs(delta) > 1e-12):
            return
        self.positions = self.positions + delta
        self.vertices = self.vertices + delta

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
        """没有被反向配对的边数：0 表示网格闭合（水密）。

        先按量化坐标合并重合顶点，避免相邻面各自离散出的同一位置顶点被当成不同点。
        """

        if self.triangle_count == 0:
            return 0
        quantized = np.round(self.positions / tolerance).astype(np.int64)
        # numpy 2.x 的 return_inverse 保持输入形状 (V, 3)，必须 reshape(-1) 才能当成顶点编号用。
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
        """网格是否基本闭合。

        每个面独立离散，接缝处的采样点不一定完全重合，因此这里只作为**参考诊断**：
        开边数远小于三角形数时视为实体。统计与仿真不依赖这个结果。
        """

        if self.triangle_count == 0:
            return False
        return self.open_edge_count(tolerance) // 2 < 0.2 * self.triangle_count

    def volume_mm3(self) -> float:
        """闭合网格的定向体积（散度定理）；非闭合网格上只作为参考值。"""

        if self.triangle_count == 0:
            return 0.0
        corners = self.positions[self.indices]
        return float(np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])).sum() / 6.0)

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


# ------------------------------------------------------------------ 入口
def tessellate_step(step: StepFile, *, source_name: str = "") -> TessellatedModel:
    """把解析好的 STEP 文件离散成三角网格。"""

    resolver = Resolver(step)
    warnings: list[str] = []
    face_entities = _collect_face_entities(step, warnings)

    if not face_entities:
        raise StepUnsupportedError(
            "文件里没有可离散的 B-rep 面（ADVANCED_FACE）："
            "可能只包含线框、二维图纸或不支持的几何表示"
        )

    positions: list[NDArray[np.float64]] = []
    indices: list[NDArray[np.int64]] = []
    face_of_triangle: list[int] = []
    faces: list[FaceRecord] = []
    edge_records: list[dict[str, Any]] = []
    vertex_offset = 0
    unsupported: dict[str, int] = {}
    malformed: dict[str, int] = {}

    for face_entity, shell_id in face_entities:
        try:
            surface = resolver.surface(surface_argument(step, face_entity))
        except Exception as error:  # 单个面的几何坏了不该让整份文件导入失败
            keyword = _surface_keyword(step, face_entity)
            malformed[keyword] = malformed.get(keyword, 0) + 1
            warnings.append(f"面 #{face_entity.id} 的曲面定义无法解释（{error}），已跳过")
            continue
        if surface is None:
            keyword = _surface_keyword(step, face_entity)
            unsupported[keyword] = unsupported.get(keyword, 0) + 1
            continue
        same_sense = as_bool(face_entity.arg(3), True)
        loops, loop_records = _face_loops(step, resolver, face_entity, warnings)
        if not loops:
            warnings.append(f"面 #{face_entity.id} 没有边界环，已跳过")
            continue

        uv_range = None
        boundary_segments = 0
        if not isinstance(surface, Plane):
            try:
                uv_range = boundary_uv_range(step, resolver, face_entity, surface)
                boundary_segments = _boundary_cells(step, resolver, face_entity)
            except Exception:  # 解析投影失败就退回离散点反算
                uv_range = None
                boundary_segments = 0

        result = _triangulate_face(surface, loops, same_sense, uv_range, boundary_segments)
        if result is None or result[1].size == 0:
            warnings.append(f"面 #{face_entity.id}（{surface.kind}）无法离散，已跳过")
            face = FaceRecord(
                id=face_entity.id,
                surface_kind=surface.kind,
                triangle_start=0,
                triangle_count=0,
                loop_count=len(loops),
                shell_id=shell_id,
                boundary_points=int(sum(loop.shape[0] for loop in loops)),
                loops=tuple(np.array(loop, dtype=np.float64) for loop in loops),
            )
            faces.append(face)
            continue

        points, triangles, approximate = result
        # 顶点去重（同一位置的点只存一次），显著压缩载荷体积。
        local_positions, local_triangles = _weld(points, triangles)
        start = int(sum(item.shape[0] for item in indices))  # 已写入的三角形数量
        positions.append(local_positions)
        indices.append(local_triangles + vertex_offset)
        face_of_triangle.extend([face_entity.id] * int(local_triangles.shape[0]))
        vertex_offset += int(local_positions.shape[0])

        area = _triangle_area_sum(local_positions, local_triangles)
        normal = _face_normal(local_positions, local_triangles, surface)
        bounds = _bounds_of(local_positions)
        plane: tuple[float, float, float, float] | None = None
        if isinstance(surface, Plane):
            axis = surface.matrix[:3, 2]
            if not same_sense:
                axis = -axis
            plane = (float(axis[0]), float(axis[1]), float(axis[2]),
                     float(np.dot(axis, surface.matrix[:3, 3])))
        faces.append(
            FaceRecord(
                id=face_entity.id,
                surface_kind=surface.kind,
                triangle_start=start,
                triangle_count=int(local_triangles.shape[0]),
                area_mm2=area,
                normal=(float(normal[0]), float(normal[1]), float(normal[2])),
                boundary_points=int(sum(loop.shape[0] for loop in loops)),
                loop_count=len(loops),
                shell_id=shell_id,
                is_planar=isinstance(surface, Plane),
                plane=plane,
                bounds=bounds,
                approximate=approximate,
                loops=tuple(np.array(loop, dtype=np.float64) for loop in loops),
            )
        )
        edge_records.extend(loop_records)

    if not positions:
        detail = "、".join(f"{key}×{value}" for key, value in sorted(unsupported.items())[:6])
        raise StepUnsupportedError(
            "所有面都无法离散"
            + (f"（不支持的曲面类型：{detail}）" if detail else "")
        )

    mesh_positions = np.vstack(positions)
    mesh_indices = np.vstack(indices)
    # 全局焊接：相邻面各自离散出的同一位置顶点必须合并，网格才是闭合的
    # （否则水密检测会报一堆"开边"，实时切削仿真也无法判断内外）。
    mesh_positions, mesh_indices = _weld(mesh_positions, mesh_indices)
    # 焊掉之后可能出现三点重合的退化三角形；它们没有法向，会污染前端的包围盒计算
    mesh_positions, mesh_indices = _drop_degenerate(mesh_positions, mesh_indices)
    if mesh_indices.shape[0] == 0:
        raise StepUnsupportedError("离散后没有有效三角形（模型可能退化成了一条线或一个点）")
    mesh_normals = _triangle_normals(mesh_positions, mesh_indices)
    mesh_indices, mesh_normals, flipped = _orient_outward(mesh_positions, mesh_indices, mesh_normals)

    unique_edges = _unique_edges(edge_records)
    file_info = step.describe()
    if unsupported:
        summary = "、".join(f"{key}×{value}" for key, value in sorted(unsupported.items()))
        warnings.append(f"以下曲面类型未离散（已跳过）：{summary}")
    if malformed:
        summary = "、".join(f"{key}×{value}" for key, value in sorted(malformed.items()))
        warnings.append(f"以下曲面定义有误（已跳过）：{summary}")

    model = TessellatedModel(
        positions=mesh_positions,
        indices=mesh_indices,
        normals=mesh_normals,
        face_of_triangle=np.asarray(face_of_triangle, dtype=np.int64),
        faces=faces,
        vertices=_collect_vertices(step, resolver),
        edges=unique_edges,
        file_info=file_info,
        warnings=warnings,
        source_name=source_name or str(file_info.get("header", {}).get("FILE_NAME", [""])[0] if file_info.get("header") else ""),
    )
    if flipped:
        model.warnings.append("网格整体朝向已按体积符号翻转，以得到向外的法向")
    return model


# ------------------------------------------------------------- 拓扑遍历
def _collect_face_entities(step: StepFile, warnings: list[str]) -> list[tuple[StepEntity, int]]:
    """找出所有要离散的面，附带它所属的壳编号。"""

    found: list[tuple[StepEntity, int]] = []
    seen: set[int] = set()
    shell_counter = 0

    for keyword in SOLID_KEYWORDS:
        for entity in step.by_keyword(keyword):
            shell_ids = _shells_of(step, entity)
            for shell_id in shell_ids:
                shell_counter += 1
                for face in _faces_of_shell(step, shell_id):
                    if face.id in seen:
                        continue
                    seen.add(face.id)
                    found.append((face, shell_counter))

    # 有些文件直接给出游离的 ADVANCED_FACE（没有实体/壳的包装）。
    for entity in step.by_keyword("ADVANCED_FACE", "FACE_SURFACE"):
        if entity.id in seen:
            continue
        seen.add(entity.id)
        shell_counter += 1
        found.append((entity, shell_counter))

    if not found:
        warnings.append("没有找到 MANIFOLD_SOLID_BREP / ADVANCED_FACE，模型可能是空壳")
    return found


def _shells_of(step: StepFile, entity: StepEntity) -> list[int]:
    keyword = entity.keyword
    if keyword == "MANIFOLD_SOLID_BREP":
        shell = as_ref(entity.arg(1))
        return [shell] if shell is not None else []
    if keyword == "BREP_WITH_VOIDS":
        shells = [as_ref(entity.arg(1))]
        for void in as_sequence(entity.arg(2)):
            void_entity = step.get(void)
            if void_entity is not None:
                shells.append(as_ref(void_entity.arg(1)))
        return [shell for shell in shells if shell is not None]
    if keyword == "FACETED_BREP":
        shell = as_ref(entity.arg(1))
        return [shell] if shell is not None else []
    if keyword == "SHELL_BASED_SURFACE_MODEL":
        return [as_ref(item) for item in as_sequence(entity.arg(1)) if as_ref(item) is not None]
    if keyword in SHELL_KEYWORDS:
        return [entity.id]
    if keyword in {"CSG_SOLID", "SOLID_MODEL"}:
        return []
    # 兜底：把参数里的第一个壳引用当作壳。
    for item in entity.arguments:
        reference = as_ref(item)
        if reference is None:
            continue
        candidate = step.get(reference)
        if candidate is not None and candidate.keyword in SHELL_KEYWORDS:
            return [reference]
    return []


def _faces_of_shell(step: StepFile, shell_id: int) -> list[StepEntity]:
    shell = step.get(shell_id)
    if shell is None:
        return []
    if shell.keyword in {"ORIENTED_CLOSED_SHELL", "ORIENTED_OPEN_SHELL"}:
        inner = as_ref(shell.arg(1))
        return _faces_of_shell(step, inner) if inner is not None else []
    if shell.keyword == "CONNECTED_FACE_SET":
        faces = []
        for item in as_sequence(shell.arg(1)):
            entity = step.get(item)
            if entity is None:
                continue
            if entity.keyword == "FACE_SURFACE":
                faces.append(entity)
            else:
                faces.extend(_faces_of_shell(step, entity.id))
        return faces
    faces: list[StepEntity] = []
    for item in as_sequence(shell.arg(1)):
        entity = step.get(item)
        if entity is None:
            continue
        if entity.keyword in {"ADVANCED_FACE", "FACE_SURFACE"}:
            faces.append(entity)
        elif entity.keyword == "ORIENTED_FACE":
            inner = as_ref(entity.arg(1))
            inner_entity = step.get(inner) if inner is not None else None
            if inner_entity is not None:
                faces.append(inner_entity)
    return faces


def _surface_keyword(step: StepFile, face: StepEntity) -> str:
    surface = step.get(surface_argument(step, face))
    return surface.keyword if surface is not None else "UNKNOWN"


def _collect_vertices(step: StepFile, resolver: Resolver) -> NDArray[np.float64]:
    points: list[NDArray[np.float64]] = []
    for entity in step.by_keyword("VERTEX_POINT", "CARTESIAN_POINT"):
        point = resolver.point3(entity.id)
        if point is not None:
            points.append(point)
    if not points:
        return np.zeros((0, 3), dtype=np.float64)
    return np.array(points, dtype=np.float64)


def _unique_edges(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        edge_id = int(record["id"])
        if edge_id in seen:
            continue
        seen.add(edge_id)
        unique.append(record)
    return unique


# ------------------------------------------------------------- 边界环采样
def _face_loops(step: StepFile, resolver: Resolver, face: StepEntity,
                warnings: list[str]) -> tuple[list[NDArray[np.float64]], list[dict[str, Any]]]:
    """读出面的所有边界环，返回 (3D 折线列表, 边记录)。"""

    loops: list[NDArray[np.float64]] = []
    records: list[dict[str, Any]] = []
    for bound in bounds_arguments(face):
        bound_entity = step.get(bound)
        if bound_entity is None:
            continue
        loop_id = as_ref(bound_entity.arg(1))
        loop_entity = step.get(loop_id) if loop_id is not None else None
        if loop_entity is None:
            continue
        points, loop_records = _loop_polyline(step, resolver, loop_entity, warnings)
        if points is not None and points.shape[0] >= 2:
            loops.append(points)
            records.extend(loop_records)
    return loops, records


def _loop_polyline(step: StepFile, resolver: Resolver, loop_entity: StepEntity,
                   warnings: list[str]) -> tuple[NDArray[np.float64] | None, list[dict[str, Any]]]:
    """把一个 EDGE_LOOP 采样成闭合折线。"""

    if loop_entity.keyword == "POLY_LOOP":
        points = [resolver.point3(item) for item in as_sequence(loop_entity.arg(1))]
        cleaned = [point for point in points if point is not None]
        if len(cleaned) < 2:
            return None, []
        return np.array(cleaned, dtype=np.float64), []

    if loop_entity.keyword == "VERTEX_LOOP":
        point = resolver.point3(loop_entity.arg(1))
        return (None, []) if point is None else (np.array([point], dtype=np.float64), [])

    oriented_edges = as_sequence(loop_entity.arg(1))
    if loop_entity.keyword != "EDGE_LOOP" and not oriented_edges:
        # FACE_BOUND 有时直接跟着一个曲线
        curve = resolver.curve(loop_entity.arg(1))
        if curve is not None:
            return curve.sample(32), []

    pieces: list[NDArray[np.float64]] = []
    records: list[dict[str, Any]] = []
    for item in oriented_edges:
        edge = _edge_entities(step, item)
        if edge is None:
            continue
        curve_entity, start_ref, end_ref, orientation = edge
        start, end = _edge_vertices(step, resolver, curve_entity, start_ref, end_ref)
        curve = resolver.curve(curve_entity.arg(3)) if curve_entity.keyword == "EDGE_CURVE" else None
        curve_sense = as_bool(curve_entity.arg(4), True)
        # 一条边的**实际走向**由 ORIENTED_EDGE 的 orientation 决定：为 .F. 时整条边要反向。
        # 以前只把它并进 same_sense、从不交换端点，于是一个环里有一半的边走反：
        # 折线变成来回走的锯齿（A→B、B→C、C→B…），面积正好少一半、体积也跟着错。
        if not orientation and start is not None and end is not None:
            start, end = end, start
        # 相对曲线自身参数方向的走向：ORIENTED_EDGE 与 EDGE_CURVE 的方向**相同**才顺参数走，
        # 因此是"两者相异取反"，也就是相等为真（写成 `and` 会在两者都为 .F. 时判反）。
        same_sense = curve_sense == orientation
        if start is None or end is None:
            if curve is None:
                continue
            sampled = curve.sample(2)
            start = sampled[0] if start is None else start
            end = sampled[-1] if end is None else end
        length = edge_length_hint(curve, start, end)
        count = chord_count(length) + 1
        if isinstance(curve, Circle):
            # 圆必须按"连接这两个端点的劣弧"采样：直接取整个参数区间会画出一整圈。
            points = curve.sample_arc(start, end, same_sense, count)
        elif curve is None or isinstance(curve, Line):
            # 直线段按两个端点线性插值。**不要**去采样曲线实体自己的参数域：
            # 直线是无界的，它的参数区间与这条边的实际长度无关（真实导出器还会把
            # VECTOR 的 magnitude 写成 1000），照搬参数域会把点甩到 1e6 量级之外。
            steps = np.linspace(0.0, 1.0, max(2, count)).reshape(-1, 1)
            points = np.asarray(start, dtype=np.float64) + steps * (
                np.asarray(end, dtype=np.float64) - np.asarray(start, dtype=np.float64)
            )
        else:
            points = curve.sample(count)
            sampled = points if same_sense else points[::-1]
            # 用顶点把曲线两端钉住，避免参数区间与顶点不一致留下缝隙
            points = np.array(sampled, dtype=np.float64)
        points = np.array(points, dtype=np.float64)
        points[0] = start
        points[-1] = end
        # 安全检查：采样点必须落在"两端点包围盒 + 该边长度余量"之内。
        # ELLIPSE / B_SPLINE_CURVE 实体描述的是**整条**曲线，而一条边只覆盖其中一段；
        # 参数域一旦取错，采样点会跑到几百上千毫米以外，整张面随之崩掉，而且不报任何错。
        # 与其让坏几何悄悄流进模型，不如退化成直线段并留下一条警告。
        span = length + 1.0
        if points.size and (
            np.any(points < np.minimum(start, end) - span)
            or np.any(points > np.maximum(start, end) + span)
        ):
            warnings.append(
                f"边 #{int(as_ref(item) or 0)}（{curve.kind}）的采样点偏离两端点过远，"
                "已退化为直线段"
            )
            points = np.array([start, end], dtype=np.float64)
        pieces.append(points)
        records.append(
            {
                "id": int(as_ref(item) or 0),
                "kind": curve.kind if curve is not None else "unknown",
                "length_mm": round(length, 4),
                "start": [round(float(value), 4) for value in start],
                "end": [round(float(value), 4) for value in end],
            }
        )

    if not pieces:
        return None, records

    merged: list[NDArray[np.float64]] = []
    for piece in pieces:
        if merged and np.linalg.norm(piece[0] - merged[-1][-1]) < 1e-6:
            merged.append(piece[1:])
        else:
            merged.append(piece)
    points = np.vstack(merged)
    # 首尾重合的点留一个，后面按"不重复首点"的约定处理。
    if points.shape[0] > 1 and np.linalg.norm(points[0] - points[-1]) < 1e-6:
        points = points[:-1]
    return points, records


def _edge_entities(step: StepFile, item: Any) -> tuple[StepEntity, Any, Any, bool] | None:
    """解开 ``ORIENTED_EDGE``，返回 (EDGE_CURVE 实体, 起点引用, 终点引用, 方向)。"""

    entity = step.get(item)
    if entity is None:
        return None
    if entity.keyword == "ORIENTED_EDGE":
        # ORIENTED_EDGE(name, edge_start, edge_end, edge_element, orientation)
        curve_entity = step.get(as_ref(entity.arg(3)))
        if curve_entity is None:
            return None
        return curve_entity, entity.arg(1), entity.arg(2), as_bool(entity.arg(4), True)
    if entity.keyword == "EDGE_CURVE":
        return entity, entity.arg(1), entity.arg(2), True
    return None


def _edge_vertices(step: StepFile, resolver: Resolver, curve_entity: StepEntity,
                   start_ref: Any, end_ref: Any) -> tuple[Any, Any]:
    """取一条边的两个端点坐标。

    ``ORIENTED_EDGE`` 的前两个属性允许写 ``*``（"由 EDGE_CURVE 决定"），绝大多数导出器
    都这么写，因此必须回退到 ``EDGE_CURVE`` 自己的两个顶点。
    """

    start = resolver.point3(_vertex_point(step, start_ref))
    end = resolver.point3(_vertex_point(step, end_ref))
    if start is None and curve_entity.keyword == "EDGE_CURVE":
        start = resolver.point3(_vertex_point(step, curve_entity.arg(1)))
    if end is None and curve_entity.keyword == "EDGE_CURVE":
        end = resolver.point3(_vertex_point(step, curve_entity.arg(2)))
    return start, end



def _edge_span_data(step: StepFile, resolver: Resolver, item: Any
                    ) -> tuple[Curve | None, Any, Any, bool, None] | None:
    """读出一条环边：曲线、两个端点、参数方向（``same_sense``）。"""

    edge = _edge_entities(step, item)
    if edge is None:
        return None
    curve_entity, start_ref, end_ref, orientation = edge
    curve = resolver.curve(curve_entity.arg(3)) if curve_entity.keyword == "EDGE_CURVE" else None
    start, end = _edge_vertices(step, resolver, curve_entity, start_ref, end_ref)
    curve_sense = as_bool(curve_entity.arg(4), True)
    # 与 _loop_polyline 同一套规则：orientation 为 .F. 要交换端点，
    # 相对参数方向的走向是"两者相同为真"。
    if not orientation and start is not None and end is not None:
        start, end = end, start
    same_sense = curve_sense == orientation
    if start is None or end is None:
        if curve is None:
            return None
        sampled = curve.sample(2)
        start = sampled[0] if start is None else start
        end = sampled[-1] if end is None else end
    return curve, start, end, same_sense, None



def _edge_uv_piece(surface: Surface, curve: Curve | None, start: Any, end: Any,
                   same_sense: bool, anchor: float | None = None) -> NDArray[np.float64]:
    """把一条边变成参数域里的一段折线。

    圆弧用圆自身的解析扫角（``uv_of`` 反算对弦上的采样点不精确）；方向按 ``same_sense``。
    整段再平移到 ``anchor``（上一段的终点 u）附近，既保持段间连续，又避免 ``uv_of``
    在接缝处返回另一支。链上各段因此落在同一分支上，u 的总跨度就是真实环绕量。
    """

    if isinstance(curve, Circle):
        low, high = curve.arc_parameters(start, end, same_sense)
        rows = []
        for parameter in np.linspace(low, high, 5):
            uv = surface.uv_of(curve.point(float(parameter)))
            rows.append([float(parameter), float(uv[1])])
        piece = np.array(rows, dtype=np.float64)
    else:
        delta = np.asarray(end, dtype=np.float64) - np.asarray(start, dtype=np.float64)
        piece = np.array(
            [surface.uv_of(np.asarray(start, dtype=np.float64) + ratio * delta)
             for ratio in np.linspace(0.0, 1.0, 2)],
            dtype=np.float64,
        )
    if anchor is not None and piece.shape[0]:
        # 只在"接缝造成的 ±2π 跳变"时整体平移：先看首、尾、中点的相位是否一致。
        # 注意不能一律取"最近的 2π 倍数"——相邻圆弧的 u 差本身就是小量，粗粒度吸附
        # 会把整段推出去 2π（同一侧环上的圆弧会被推成绕远路的那一条）。
        wrapped = np.angle(np.exp(1j * (piece - anchor)))[:, 0]
        spread = float(wrapped.max() - wrapped.min())
        if spread > pi:
            piece[:, 0] = piece[:, 0] + 2.0 * pi * round((anchor - float(piece[0, 0])) / (2.0 * pi))
    return piece


def boundary_uv_range(step: StepFile, resolver: Resolver, face: StepEntity, surface: Surface
                      ) -> tuple[float, float, float, float] | None:
    """从边界边的**解析信息**直接求出面的参数域范围 (u_lo, u_hi, v_lo, v_hi)。

    做法：

    - 每条边的两个端点在曲面上反算出 (u, v)，u 按"相邻点最接近"的 2π 整数倍解绕；
    - 如果某条圆边的扫角超过 1.9π（整圈），说明该方向覆盖了整个周期，stretch 到整圈。

    这样得到的范围不依赖"把边界离散点连成多边形"，因此不会出现周期面上多边形自交、
    退化或只剩一条细带的经典问题。
    """

    u_values: list[float] = []
    v_values: list[float] = []
    full_wrap = False
    previous: float | None = None

    def push(point: Any) -> None:
        nonlocal previous
        if point is None:
            return
        uv = surface.uv_of(point)
        u = float(uv[0])
        if previous is not None:
            u = previous + float(np.angle(np.exp(1j * (u - previous))))
        previous = u
        u_values.append(u)
        v_values.append(float(uv[1]))

    for bound in bounds_arguments(face):
        bound_entity = step.get(bound)
        if bound_entity is None:
            continue
        loop_entity = step.get(as_ref(bound_entity.arg(1)))
        if loop_entity is None:
            continue
        previous = None
        for item in as_sequence(loop_entity.arg(1)):
            edge = _edge_span_data(step, resolver, item)
            if edge is None:
                continue
            curve, start, end, same_sense, _ = edge
            if isinstance(curve, Circle):
                low, high = curve.arc_parameters(start, end, same_sense)
                if abs(high - low) >= 1.9 * pi:
                    full_wrap = True
            push(start)
            push(end)

    if not u_values:
        return None
    u_lo, u_hi = min(u_values), max(u_values)
    v_lo, v_hi = min(v_values), max(v_values)
    if v_hi - v_lo < 1e-9:
        v_hi = v_lo + 1.0
    periodic = getattr(surface, "periodic_u", False) and surface.u_range is not None
    if full_wrap and periodic:
        u_lo, u_hi = float(surface.u_range[0]), float(surface.u_range[1])
    elif u_hi - u_lo < 1e-9:
        u_hi = u_lo + (2.0 * pi if periodic else 1.0)
    if not periodic and surface.u_range is not None:
        u_lo = max(u_lo, float(surface.u_range[0]))
        u_hi = min(u_hi, float(surface.u_range[1]))
    return u_lo, u_hi, v_lo, v_hi




def _vertex_point(step: StepFile, value: Any) -> Any:
    entity = step.get(value)
    if entity is not None and entity.keyword == "VERTEX_POINT":
        return entity.arg(1)
    return value


# ---------------------------------------------------------------- 三角化
def _triangulate_face(surface: Surface, loops: Sequence[NDArray[np.float64]],
                      same_sense: bool,
                      uv_range: tuple[float, float, float, float] | None = None,
                      boundary_segments: int = 0
                      ) -> tuple[NDArray[np.float64], NDArray[np.int64], bool] | None:
    """按曲面类型选择离散方式，返回 (点, 三角形, 是否近似)。"""

    if isinstance(surface, Plane):
        return _triangulate_planar(surface, loops, same_sense)
    return _triangulate_parametric(surface, uv_range, loops, same_sense, boundary_segments)


# -- 平面：耳切法 ---------------------------------------------------------
def _triangulate_planar(surface: Plane, loops: Sequence[NDArray[np.float64]],
                        same_sense: bool) -> tuple[NDArray[np.float64], NDArray[np.int64], bool] | None:
    projected = [np.array([surface.uv_of(point) for point in loop], dtype=np.float64) for loop in loops]
    projected = [item for item in projected if item.shape[0] >= 3]
    if not projected:
        return None

    # 同一个顶点在 STEP 里可能因为不同实例而坐标相同；这里做一次平面内去重。
    projected = [_dedupe_loop(item) for item in projected]
    projected = [item for item in projected if item.shape[0] >= 3]
    if not projected:
        return None

    outer_index = max(range(len(projected)), key=lambda index: abs(_signed_area(projected[index])))
    outer = projected[outer_index]
    if _signed_area(outer) < 0:
        outer = outer[::-1]
    holes = []
    for index, loop in enumerate(projected):
        if index == outer_index:
            continue
        hole = loop if _signed_area(loop) < 0 else loop[::-1]
        holes.append(hole)

    # 每个孔用一条"桥"接到外环上，得到一个单一多边形，再用耳切法三角化。
    # 采样点里有大量共线点（短直线边被离散成多段），因此先做共线化简：否则耳切法
    # 要在成百上千个退化顶点里找真正的"耳朵"，既慢又容易误判（曾出现过整个孔被忽略）。
    polygon, _hole_ranges = _bridge_holes(outer, holes)
    polygon = _simplify_polygon(polygon)
    if polygon.shape[0] < 3:
        return None
    triangles_2d = _ear_clip(polygon)
    if triangles_2d.size == 0:
        return None

    # 去掉退化三角形与来自"桥"的重复点
    triangles_2d = np.array(
        [triangle for triangle in triangles_2d if len(set(int(index) for index in triangle)) == 3],
        dtype=np.int64,
    )
    if triangles_2d.size == 0:
        return None

    base = np.asarray(surface.matrix[:3, 3], dtype=np.float64)
    axis_u = np.asarray(surface.matrix[:3, 0], dtype=np.float64)
    axis_v = np.asarray(surface.matrix[:3, 1], dtype=np.float64)
    points = base + polygon[:, 0:1] * axis_u + polygon[:, 1:2] * axis_v
    if not same_sense:
        triangles_2d = triangles_2d[:, ::-1]
    return points, triangles_2d, False


def _simplify_polygon(polygon: NDArray[np.float64], tolerance: float = 1e-9
                      ) -> NDArray[np.float64]:
    """去掉折线里的重复点与共线点（至少保留 3 个顶点）。"""

    points = np.asarray(polygon, dtype=np.float64)
    if points.shape[0] < 4:
        return points
    keep = [points[0]]
    for point in points[1:]:
        if float(np.linalg.norm(point - keep[-1])) > tolerance:
            keep.append(point)
    if len(keep) > 1 and float(np.linalg.norm(keep[0] - keep[-1])) <= tolerance:
        keep.pop()
    if len(keep) < 3:
        return np.asarray(keep if keep else points, dtype=np.float64)

    # 反复去掉共线点（每次扫描去掉一批，几轮就收敛）
    for _ in range(64):
        if len(keep) <= 3:
            break
        result: list[NDArray[np.float64]] = []
        count = len(keep)
        for index in range(count):
            previous = keep[(index - 1) % count]
            current = keep[index]
            following = keep[(index + 1) % count]
            first = current - previous
            second = following - current
            scale = max(float(np.linalg.norm(first)), 1e-12)
            if abs(_cross2(first, second)) / scale <= tolerance:
                continue
            result.append(current)
        if len(result) < 3 or len(result) == len(keep):
            break
        keep = result
    return np.asarray(keep, dtype=np.float64)


def _dedupe_loop(loop: NDArray[np.float64], tolerance: float = 1e-7) -> NDArray[np.float64]:
    keep = [loop[0]]
    for point in loop[1:]:
        if float(np.linalg.norm(point - keep[-1])) > tolerance:
            keep.append(point)
    while len(keep) > 1 and float(np.linalg.norm(keep[0] - keep[-1])) <= tolerance:
        keep.pop()
    return np.array(keep, dtype=np.float64)


def _cross2(a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
    """平面向量的"叉积"（标量），在 numpy 2.x 上 np.cross 不再接受二维向量。"""

    return float(a[0] * b[1] - a[1] * b[0])


def _signed_area(polygon: NDArray[np.float64]) -> float:
    x = polygon[:, 0]
    y = polygon[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _bridge_holes(outer: NDArray[np.float64], holes: Sequence[NDArray[np.float64]]
                  ) -> tuple[NDArray[np.float64], list[tuple[int, int]]]:
    """把内环用"桥"接到外环上，得到一个单一多边形（耳切法的标准前置步骤）。

    做法：从内环最右点向它在外环上最近的凸顶点连一条**来回路**，插入到外环序列里::

        … B, H0, H1, …, H0, B, …

    其中 B 是外环上的点，H0 是内环最右点。插入位置在 B 之后、原序列继续之前，
    这样桥是"B→H0"与"H0→B"两条重合的往返边，不会形成自交。
    """

    polygon = outer.copy()
    hole_ranges: list[tuple[int, int]] = []
    ordered = sorted(range(len(holes)), key=lambda index: -float(holes[index][:, 0].max()))
    for hole_index in ordered:
        hole = holes[hole_index]
        if hole.shape[0] < 3:
            continue
        right_index = int(np.argmax(hole[:, 0]))
        bridge_point = hole[right_index]
        bridge_index = _nearest_visible_index(polygon, bridge_point)
        rotated = np.vstack([hole[right_index:], hole[:right_index], hole[right_index]])
        # 关键顺序：先放桥点、再放内环、最后回到桥点，然后原多边形从 bridge_index 重新开始。
        polygon = np.vstack([
            polygon[:bridge_index + 1],
            rotated,
            polygon[bridge_index:],
        ])
        start = bridge_index + 1
        hole_ranges.append((start, int(rotated.shape[0])))
    return polygon, hole_ranges


def _nearest_visible_index(polygon: NDArray[np.float64], point: NDArray[np.float64]) -> int:
    distances = np.linalg.norm(polygon - point, axis=1)
    # 只在"凸顶点"里挑，桥才不会穿过外环自身。
    convex = _convex_vertex_indices(polygon)
    candidates = convex if convex else list(range(polygon.shape[0]))
    return int(min(candidates, key=lambda index: float(distances[index])))


def _convex_vertex_indices(polygon: NDArray[np.float64]) -> list[int]:
    count = polygon.shape[0]
    result: list[int] = []
    for index in range(count):
        previous = polygon[(index - 1) % count]
        current = polygon[index]
        following = polygon[(index + 1) % count]
        if _cross2(current - previous, following - current) > 0.0:
            result.append(index)
    return result


def _ear_clip(polygon: NDArray[np.float64]) -> NDArray[np.int64]:
    """耳切法：返回 (T, 3) 的顶点下标。

    复杂度是 O(n²)，而真实零件的一个平面面可能被离散成上千个点，因此这里做两件事：

    1. 预先把每个顶点的坐标取进 numpy 数组，避免在内层循环里反复索引 numpy；
    2. 用耳朵三角形的包围盒先过滤一遍候选点——绝大多数点在不做任何叉积计算的情况下
       就被排除，实测比朴素写法快一个数量级。
    """

    count = polygon.shape[0]
    if count < 3:
        return np.zeros((0, 3), dtype=np.int64)
    points = np.asarray(polygon, dtype=np.float64)
    xs = points[:, 0]
    ys = points[:, 1]
    indices = list(range(count))
    x_list = xs.tolist()
    y_list = ys.tolist()
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    limit = 4 * count + 16
    while len(indices) > 3 and guard < limit:
        guard += 1
        clipped = False
        size = len(indices)
        for position in range(size):
            previous = indices[(position - 1) % size]
            current = indices[position]
            following = indices[(position + 1) % size]
            ax, ay = x_list[previous], y_list[previous]
            bx, by = x_list[current], y_list[current]
            cx, cy = x_list[following], y_list[following]
            if (bx - ax) * (cy - by) - (by - ay) * (cx - bx) <= 0.0:
                continue
            area = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))
            scale = area if area > 1e-12 else 1e-12
            x_min, x_max = min(ax, bx, cx), max(ax, bx, cx)
            y_min, y_max = min(ay, by, cy), max(ay, by, cy)
            blocked = False
            for index in indices:
                if index == previous or index == current or index == following:
                    continue
                px, py = x_list[index], y_list[index]
                # 包围盒预筛：不在盒内的点不可能落在三角形里
                if px < x_min or px > x_max or py < y_min or py > y_max:
                    continue
                d1 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
                d2 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
                d3 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
                if d1 >= -1e-12 and d2 >= -1e-12 and d3 >= -1e-12:
                    # 严格在内部才阻挡；恰好贴边（桥接缝隙的公共顶点）不算
                    if abs(d1) <= 1e-9 * scale or abs(d2) <= 1e-9 * scale or abs(d3) <= 1e-9 * scale:
                        continue
                    blocked = True
                    break
            if blocked:
                continue
            triangles.append((previous, current, following))
            indices.pop(position)
            clipped = True
            break
        if not clipped:
            # 找不到耳朵（自交或桥接退化）：去掉一个最"扁"的顶点继续
            worst_position = 0
            worst = None
            for position in range(len(indices)):
                previous = indices[(position - 1) % len(indices)]
                current = indices[position]
                following = indices[(position + 1) % len(indices)]
                ax, ay = x_list[previous], y_list[previous]
                bx, by = x_list[current], y_list[current]
                cx, cy = x_list[following], y_list[following]
                value = abs((bx - ax) * (cy - by) - (by - ay) * (cx - bx))
                if worst is None or value < worst:
                    worst = value
                    worst_position = position
            indices.pop(worst_position)
    if len(indices) == 3:
        triangles.append((indices[0], indices[1], indices[2]))
    return np.asarray(triangles, dtype=np.int64)


def _is_ear(polygon: NDArray[np.float64], indices: list[int], previous: int, current: int,
            following: int) -> bool:
    a = polygon[previous]
    b = polygon[current]
    c = polygon[following]
    if _cross2(b - a, c - b) <= 0.0:
        return False
    # 面积尺度：用来把"恰好落在耳朵边上"的点与"真正落在内部"的点区分开。
    # 桥接产生的缝隙里，同一位置的顶点会精确落在耳朵边上，若把它们算作"内部"，`
    # 耳切法会一个耳朵都找不到，最后退化成几个三角形（孔洞会被当成实心）。
    area = abs(_cross2(b - a, c - a))
    scale = max(area, 1e-12)
    for index in indices:
        if index in (previous, current, following):
            continue
        point = polygon[index]
        d1 = _cross2(b - a, point - a)
        d2 = _cross2(c - b, point - b)
        d3 = _cross2(a - c, point - c)
        if d1 >= -1e-12 and d2 >= -1e-12 and d3 >= -1e-12:
            # 严格在内部，或者贴在某条边上：只有"贴边"才不算阻挡
            if abs(d1) <= 1e-9 * scale or abs(d2) <= 1e-9 * scale or abs(d3) <= 1e-9 * scale:
                continue
            return False
    return True


def _point_in_triangle(point: NDArray[np.float64], a: NDArray[np.float64], b: NDArray[np.float64],
                       c: NDArray[np.float64]) -> bool:
    d1 = _cross2(b - a, point - a)
    d2 = _cross2(c - b, point - b)
    d3 = _cross2(a - c, point - c)
    has_negative = d1 < -1e-12 or d2 < -1e-12 or d3 < -1e-12
    has_positive = d1 > 1e-12 or d2 > 1e-12 or d3 > 1e-12
    return not (has_negative and has_positive)


# -- 解析曲面 / B 样条：参数域网格裁剪 ------------------------------------
def _triangulate_parametric(surface: Surface,
                            uv_range: tuple[float, float, float, float] | None,
                            loops: Sequence[NDArray[np.float64]], same_sense: bool,
                            boundary_segments: int = 0
                            ) -> tuple[NDArray[np.float64], NDArray[np.int64], bool] | None:
    """在参数域里用边界范围裁剪采样网格。

    ``uv_range`` 由 :func:`boundary_uv_range` 用解析曲线算出来（首选）；没有时退回
    对离散点逐点反算参数并用射线法裁剪（对周期面会不准确，因此结果标记为 approximate）。
    """

    approximate = False
    if uv_range is not None:
        # 首选：由边界的解析信息直接给出参数域范围（圆弧扫角 + 端点角度）。
        # 这条路不依赖"把边界点连成多边形"，因此对周期面（圆柱/圆锥/球/环面）
        # 不会出现"多边形自交 / 只剩一条细带"的问题。
        u_lo, u_hi, v_lo, v_hi = uv_range
        v_lo, v_hi = _snap_range(v_lo, v_hi)
        trim = None
    else:
        loop_uv = []
        for loop in loops:
            uv = np.array([surface.uv_of(point) for point in loop], dtype=np.float64)
            if uv.shape[0] >= 3:
                loop_uv.append(_unwrap_piece(uv))
        if not loop_uv:
            return None
        approximate = True
        all_points = np.vstack(loop_uv)
        u_low, u_high = float(all_points[:, 0].min()), float(all_points[:, 0].max())
        v_low, v_high = float(all_points[:, 1].min()), float(all_points[:, 1].max())
        u_span = max(u_high - u_low, 1e-9)
        u_lo, u_hi = u_low - 0.06 * u_span, u_high + 0.06 * u_span
        v_span = max(v_high - v_low, 1e-9)
        v_lo, v_hi = _snap_range(v_low - 0.12 * v_span, v_high + 0.12 * v_span)
        trim = _outer_trim_polygon(loop_uv)

    steps_u, steps_v = _grid_steps(surface, u_lo, u_hi, v_lo, v_hi)
    steps_u = _align_steps(steps_u, boundary_segments)
    u_values = np.linspace(u_lo, u_hi, steps_u + 1)
    v_values = np.linspace(v_lo, v_hi, steps_v + 1)
    grid = surface.points_grid(u_values, v_values)  # (steps_u+1, steps_v+1, 3)

    keep_cells: list[tuple[int, int]] = []
    for i in range(steps_u):
        for j in range(steps_v):
            if trim is None or _point_in_polygon(
                np.array([0.5 * (u_values[i] + u_values[i + 1]), 0.5 * (v_values[j] + v_values[j + 1])]), trim
            ):
                keep_cells.append((i, j))
    if not keep_cells:
        return None

    # 只收集用到的格点，顺带做编号映射。
    vertex_map: dict[tuple[int, int], int] = {}
    points: list[NDArray[np.float64]] = []
    triangles: list[tuple[int, int, int]] = []

    def vertex(i: int, j: int) -> int:
        key = (i, j)
        cached = vertex_map.get(key)
        if cached is not None:
            return cached
        index = len(points)
        points.append(grid[i, j])
        vertex_map[key] = index
        return index

    for i, j in keep_cells:
        a = vertex(i, j)
        b = vertex(i + 1, j)
        c = vertex(i + 1, j + 1)
        d = vertex(i, j + 1)
        if same_sense:
            triangles.extend([(a, b, c), (a, c, d)])
        else:
            triangles.extend([(a, c, b), (a, d, c)])
    if not triangles:
        return None
    return (np.asarray(points, dtype=np.float64), np.asarray(triangles, dtype=np.int64), approximate)


def _snap_range(low: float, high: float, target: float = 0.08) -> tuple[float, float]:
    """把区间端点向外取整到 target 的倍数，让网格边界与面的边界尽量对齐。"""

    span = high - low
    if span <= 1e-12:
        return low, high
    step = max(span / MAX_GRID_CELLS_PER_AXIS, min(target, span / 2.0))
    snapped_low = np.floor(low / step) * step
    snapped_high = np.ceil(high / step) * step
    return float(snapped_low), float(snapped_high)


def _boundary_cells(step: StepFile, resolver: Resolver, face: StepEntity) -> int:
    """边界环上"离散段"的数量，作为柱面/球面在圆周方向的目标网格数。

    相邻平面上的孔通常由 N 段直线拼成，柱面如果不用同样的 N 段，两者边缘就对不上：
    模型会出现细缝，体积与面积统计随之偏差。取所有环中**最少**的一段，保证对齐可行。
    """

    best: int | None = None
    for bound in bounds_arguments(face):
        bound_entity = step.get(bound)
        if bound_entity is None:
            continue
        loop_entity = step.get(as_ref(bound_entity.arg(1)))
        if loop_entity is None:
            continue
        points: list[Any] = []
        for item in as_sequence(loop_entity.arg(1)):
            edge = _edge_span_data(step, resolver, item)
            if edge is None:
                continue
            points.append(edge[1])
            points.append(edge[2])
        count = _unique_point_count(points)
        if count >= 4 and (best is None or count < best):
            best = count
    return best or 0


def _unique_point_count(points: list[Any], tolerance: float = 1e-6) -> int:
    """这些点里有多少个**互不相同**的位置（按容差去重）。"""

    seen: list[NDArray[np.float64]] = []
    for point in points:
        if point is None:
            continue
        array = np.asarray(point, dtype=np.float64).reshape(3)
        if any(float(np.linalg.norm(array - item)) <= tolerance for item in seen):
            continue
        seen.append(array)
    return len(seen)


def _align_steps(steps: int, target: int | None) -> int:
    """把网格步数调整成 target 的整数倍。

    允许 1/3 的浮动：宁可精度稍微降一点，也要让柱面的圆周分段落在相邻平面的孔顶点上，
    否则两个面之间会留下细缝（体积、面积统计随之偏差）。
    """

    if not target or target < 4:
        return steps
    multiple = max(1, round(steps / target))
    aligned = target * multiple
    if steps <= 0 or abs(aligned - steps) / steps <= 0.34:
        return aligned
    return steps


def _grid_steps(surface: Surface, u_lo: float, u_hi: float, v_lo: float,
                v_hi: float) -> tuple[int, int]:
    """按"弦高误差"决定两个参数的网格步数。

    关键在于把参数增量换算成**物理长度**：圆柱/圆锥的 u 是弧度，直接拿弧度与
    v 的毫米比较会得到荒谬的步数。这里用曲面在域中心的切向量长度做换算。
    """

    tolerance = _chord_tolerance(surface)
    u_mid = 0.5 * (u_lo + u_hi)
    v_mid = 0.5 * (v_lo + v_hi)
    base = np.asarray(surface.point(u_mid, v_mid), dtype=np.float64)
    delta_u = max((u_hi - u_lo) * 1e-3, 1e-4)
    delta_v = max((v_hi - v_lo) * 1e-3, 1e-4)
    length_u = float(np.linalg.norm(surface.point(u_mid + delta_u, v_mid) - base)) / delta_u
    length_v = float(np.linalg.norm(surface.point(u_mid, v_mid + delta_v) - base)) / delta_v
    length_u = max(length_u, 1e-6)
    length_v = max(length_v, 1e-6)
    physical = min((u_hi - u_lo) * length_u, (v_hi - v_lo) * length_v)
    physical = max(physical, 1e-6)
    steps_from_tolerance = max(12.0, 2.0 * physical / max(tolerance, 1e-3))
    total_max = 4.0 * MAX_GRID_CELLS_PER_AXIS
    steps_u = int(round(steps_from_tolerance * (u_hi - u_lo) * length_u / physical))
    steps_v = int(round(steps_from_tolerance * (v_hi - v_lo) * length_v / physical))
    steps_u = int(min(MAX_GRID_CELLS_PER_AXIS, max(4, steps_u)))
    steps_v = int(min(MAX_GRID_CELLS_PER_AXIS, max(4, steps_v)))
    # 两个方向的总单元数再限一次，避免超大面上出现百万三角形。
    if steps_u * steps_v > 4000:
        scale = (4000.0 / (steps_u * steps_v)) ** 0.5
        steps_u = max(4, int(steps_u * scale))
        steps_v = max(4, int(steps_v * scale))
    _ = total_max
    return steps_u, steps_v


def _chord_tolerance(surface: Surface) -> float:
    """弦高误差目标（mm），按面的尺寸取，保证小面也有足够精度。"""

    if isinstance(surface, Cylinder):
        return max(abs(surface.radius) * 0.012, 0.02)
    if isinstance(surface, Cone):
        return max(abs(surface.radius) * 0.012, 0.02)
    if isinstance(surface, Sphere):
        return max(abs(surface.radius) * 0.012, 0.02)
    if isinstance(surface, Torus):
        return max(surface.minor_radius * 0.02, 0.02)
    return 0.2


def _unwrap_u(loop: NDArray[np.float64]) -> NDArray[np.float64]:
    """把参数域 u 的跳变（±2π 接缝）解开。"""

    result = loop.copy()
    center = float(np.median(result[:, 0]))
    result[:, 0] = center + np.angle(np.exp(1j * (result[:, 0] - center)))
    return result



def _point_in_polygon(point: NDArray[np.float64], polygon: NDArray[np.float64]) -> bool:
    """射线法（参数域通常只有几十个点，纯 Python 足够快）。"""

    x, y = float(point[0]), float(point[1])
    inside = False
    count = polygon.shape[0]
    for index in range(count):
        x0, y0 = float(polygon[index, 0]), float(polygon[index, 1])
        x1, y1 = float(polygon[(index + 1) % count, 0]), float(polygon[(index + 1) % count, 1])
        if (y0 > y) != (y1 > y):
            denominator = y1 - y0
            if abs(denominator) > 1e-15:
                crossing = x0 + (y - y0) * (x1 - x0) / denominator
                if crossing > x:
                    inside = not inside
    return inside


# ---------------------------------------------------------------- 网格工具
def _weld(points: NDArray[np.float64], triangles: NDArray[np.int64],
          tolerance: float = 1e-7) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """按量化坐标合并重复顶点。"""

    if points.size == 0:
        return points, triangles
    quantized = np.round(points / tolerance).astype(np.int64)
    _, first_index, inverse = np.unique(quantized, axis=0, return_index=True, return_inverse=True)
    order = np.argsort(first_index)
    remap = np.empty_like(order)
    remap[order] = np.arange(order.shape[0])
    positions = points[np.sort(first_index)]
    indices = remap[inverse.reshape(-1)][triangles]
    return positions, indices.astype(np.int64)


def _triangle_normals(points: NDArray[np.float64], triangles: NDArray[np.int64]) -> NDArray[np.float64]:
    if triangles.size == 0:
        return np.zeros((0, 3), dtype=np.float64)
    corners = points[triangles]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    # 退化三角形（面积为零）的法向没有意义，给一个缺省值而不是 NaN：
    # 一个 NaN 顶点就会让前端的包围盒 / 包围球计算失败，进而整块模型不显示。
    return normals / np.where(lengths > 1e-15, lengths, 1.0)


def _drop_degenerate(points: NDArray[np.float64], triangles: NDArray[np.int64],
                     tolerance: float = 1e-12) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """丢掉面积为零的三角形（共线三点 / 重复顶点）。"""

    if triangles.size == 0:
        return points, triangles
    corners = points[triangles]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    areas = np.linalg.norm(normals, axis=1)
    keep = areas > tolerance
    if bool(keep.all()):
        return points, triangles
    triangles = triangles[keep]
    if triangles.size == 0:
        return points, np.zeros((0, 3), dtype=np.int64)
    used = np.unique(triangles)
    remap = np.full(points.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(used.shape[0], dtype=np.int64)
    return points[used], remap[triangles]


def _triangle_area_sum(points: NDArray[np.float64], triangles: NDArray[np.int64]) -> float:
    if triangles.size == 0:
        return 0.0
    corners = points[triangles]
    return float(0.5 * np.linalg.norm(
        np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
    ).sum())


def _face_normal(points: NDArray[np.float64], triangles: NDArray[np.int64],
                 surface: Surface) -> NDArray[np.float64]:
    """面的代表法向：解析曲面直接用曲面法向在参数中心的值，平面用三角形面积加权。"""

    if isinstance(surface, (Cylinder, Cone, Sphere, Torus, BSplineSurface)):
        center = points.mean(axis=0)
        try:
            return np.asarray(surface.normal(*_uv_guess(surface, center)), dtype=np.float64)
        except Exception:  # pragma: no cover - 兜底
            pass
    if triangles.size == 0:
        return np.array([0.0, 0.0, 1.0], dtype=np.float64)
    corners = points[triangles]
    raw = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    areas = np.linalg.norm(raw, axis=1, keepdims=True)
    weighted = (raw / np.where(areas > 1e-15, areas, 1.0) * areas).sum(axis=0)
    norm = float(np.linalg.norm(weighted))
    if norm <= 1e-12:
        return np.array([0.0, 0.0, 1.0], dtype=np.float64)
    return weighted / norm


def _uv_guess(surface: Surface, point: NDArray[np.float64]) -> tuple[float, float]:
    uv = surface.uv_of(point)
    return float(uv[0]), float(uv[1])


def _bounds_of(points: NDArray[np.float64]) -> tuple[float, float, float, float, float, float]:
    low = points.min(axis=0)
    high = points.max(axis=0)
    return (float(low[0]), float(low[1]), float(low[2]),
            float(high[0]), float(high[1]), float(high[2]))


def _orient_outward(points: NDArray[np.float64], triangles: NDArray[np.int64],
                    normals: NDArray[np.float64]) -> tuple[NDArray[np.int64], NDArray[np.float64], bool]:
    """按体积符号统一朝向：闭合实体应当得到正的体积。"""

    if triangles.size == 0:
        return triangles, normals, False
    corners = points[triangles]
    volume = float(np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])).sum() / 6.0)
    if volume >= 0.0:
        return triangles, normals, False
    return triangles[:, ::-1], -normals, True


__all__ = [
    "FaceRecord",
    "SHELL_KEYWORDS",
    "SOLID_KEYWORDS",
    "TessellatedModel",
    "tessellate_step",
]
