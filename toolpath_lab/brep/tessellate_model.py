"""把 BRep 离散结果组装成上层使用的 :class:`TessellatedModel`。

这是 OCP 世界与上层之间的**唯一转换点**：``brep`` 之外的代码只认
:class:`~toolpath_lab.core.tessellation.TessellatedModel`，不认识 OCP。

每个面除了三角形，还要给出**边界环**（``FaceRecord.loops``）：
型腔铣的加工区域、平面铣的轮廓都直接用它，从网格反推边界既慢又不准。
面的 wire 在 BRep 里本来就是有序的，所以这里不像切片那样需要自己拼环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.brep.backend import require_ocp
from toolpath_lab.brep.model import BrepFaceInfo, BrepModel
from toolpath_lab.core.tessellation import FaceRecord, TessellatedModel

logger = logging.getLogger(__name__)

#: 面边界环的弦高容差（mm）。刀路用 0.01~0.05。
DEFAULT_BOUNDARY_DEFLECTION = 0.02


@dataclass(frozen=True, slots=True)
class MeshOptions:
    """离散参数。

    :param linear_deflection: 线性偏差（mm），弦到曲面的最大距离。越小越细。
        做刀路建议 0.01~0.05；只要形状预览 0.1~0.5。
    :param angular_deflection: 角度偏差（弧度），相邻面片法向最大夹角。
    :param boundary_deflection: 面边界环离散的弦高容差（mm）。
    :param parallel: 多线程离散。
    """

    linear_deflection: float = 0.05
    angular_deflection: float = 0.5
    boundary_deflection: float = DEFAULT_BOUNDARY_DEFLECTION
    parallel: bool = True


def to_tessellated_model(model: BrepModel, options: MeshOptions | None = None, *,
                         normalize: bool = True) -> TessellatedModel:
    """BRep -> :class:`TessellatedModel`（网格 + 面拓扑 + 边界环）。

    :param model: OCP 读进来的 BRep 模型
    :param options: 离散参数
    :param normalize: 是否归一化到机床坐标系（XY 居中、Z 最低点 0）。
        上层（毛坯、刀路、仿真）都假设这个坐标系。
    """

    require_ocp()
    options = options or MeshOptions()
    if normalize:
        model = model.normalized()

    face_arrays, face_of_triangle, ranges = _tessellate_faces(model, options)
    positions, indices = _merge_and_weld(face_arrays)

    records: list[FaceRecord] = []
    for index, info in enumerate(model.faces):
        start, count = ranges[index]
        loops = _face_loops(model, index, options.boundary_deflection)
        records.append(FaceRecord(
            id=index,
            surface_kind=info.kind,
            triangle_start=start,
            triangle_count=count,
            area_mm2=info.area_mm2,
            normal=info.normal,
            boundary_points=int(sum(loop.shape[0] for loop in loops)),
            loop_count=len(loops),
            shell_id=0,
            is_planar=info.is_planar,
            plane=info.plane,
            bounds=_loop_bounds(loops),
            loops=loops,
        ))

    normals = _triangle_normals(positions, indices)
    result = TessellatedModel(
        positions=positions,
        indices=indices,
        normals=normals,
        face_of_triangle=face_of_triangle,
        faces=records,
        file_info={"reader": "OCP", "source": model.source},
        source_name=model.name,
    )
    logger.info("转换为离散模型：%d 面 / %d 三角面 / %d 顶点",
                len(records), result.triangle_count, result.vertex_count)
    return result


# ---------------------------------------------------------------- 内部实现
def _tessellate_faces(model: BrepModel, options: MeshOptions
                      ) -> tuple[list[tuple[NDArray[np.float64], NDArray[np.int64]]],
                                 NDArray[np.int64], list[tuple[int, int]]]:
    """离散每个面，返回 (各面局部网格, 三角形->面, 各面在合并后的三角形区间)。"""

    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(model.shape, float(options.linear_deflection), False,
                             float(options.angular_deflection), bool(options.parallel))

    arrays: list[tuple[NDArray[np.float64], NDArray[np.int64]]] = []
    face_of_triangle: list[int] = []
    explorer = TopExp_Explorer(model.shape, TopAbs_FACE)
    face_index = 0
    while explorer.More():
        face = TopoDS.Face(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            arrays.append((np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)))
        else:
            # gp_Trsf 上**没有** IsIdentity，要问 TopLoc_Location
            transform = location.Transformation()
            identity = location.IsIdentity()
            node_count = triangulation.NbNodes()
            nodes = np.empty((node_count, 3), dtype=np.float64)
            for node in range(1, node_count + 1):
                point = triangulation.Node(node)
                if not identity:
                    point = point.Transformed(transform)
                nodes[node - 1] = (point.X(), point.Y(), point.Z())

            triangle_count = triangulation.NbTriangles()
            triangles = np.empty((triangle_count, 3), dtype=np.int64)
            reversed_face = face.Orientation() == TopAbs_REVERSED
            for index in range(1, triangle_count + 1):
                a, b, c = triangulation.Triangle(index).Get()
                # 面被反向时三角形绕向必须翻，否则法向朝里
                triangles[index - 1] = ((a - 1, c - 1, b - 1) if reversed_face
                                        else (a - 1, b - 1, c - 1))
            arrays.append((nodes, triangles))
            face_of_triangle.extend([face_index] * triangle_count)
        face_index += 1
        explorer.Next()

    ranges: list[tuple[int, int]] = []
    cursor = 0
    for _, triangles in arrays:
        count = int(triangles.shape[0])
        ranges.append((cursor, count))
        cursor += count
    return arrays, np.asarray(face_of_triangle, dtype=np.int64), ranges


def _merge_and_weld(arrays: list[tuple[NDArray[np.float64], NDArray[np.int64]]],
                    *, tolerance: float = 1e-6
                    ) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """合并各面网格，并按量化坐标焊接重合顶点。

    相邻面各自离散会在公共边上生成重复顶点；不焊接的话网格不闭合，
    仿真判断内外、STL 导出都会出问题。
    """

    usable = [(nodes, triangles) for nodes, triangles in arrays if triangles.shape[0] > 0]
    if not usable:
        return np.zeros((0, 3), dtype=np.float64), np.zeros((0, 3), dtype=np.int64)

    all_positions = np.vstack([nodes for nodes, _ in usable])
    quantized = np.round(all_positions / tolerance).astype(np.int64)
    _, first_index, inverse = np.unique(quantized, axis=0, return_index=True,
                                        return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    welded = all_positions[first_index]

    offsets: list[int] = []
    cursor = 0
    for nodes, triangles in arrays:
        offsets.append(cursor)
        cursor += nodes.shape[0]

    merged: list[NDArray[np.int64]] = []
    for index, (_, triangles) in enumerate(arrays):
        if triangles.shape[0] == 0:
            continue
        merged.append(inverse[triangles + offsets[index]])
    if not merged:
        return welded, np.zeros((0, 3), dtype=np.int64)
    return welded, np.vstack(merged)


def _face_loops(model: BrepModel, face_index: int, deflection: float
                ) -> tuple[NDArray[np.float64], ...]:
    """取一个面的所有边界环（世界坐标）。

    面里的 wire 在 BRep 中本来就是有序的，用 ``TopExp_Explorer`` 拿到 wire，
    再用 ``BRepTools_WireExplorer`` 按顺序走它的边即可 —— 不需要像切片那样拼环。
    """

    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    explorer = TopExp_Explorer(model.shape, TopAbs_FACE)
    for _ in range(face_index):
        explorer.Next()
    if not explorer.More():
        return ()
    face = TopoDS.Face(explorer.Current())

    loops: list[NDArray[np.float64]] = []
    wire_explorer = TopExp_Explorer(face, TopAbs_WIRE)
    while wire_explorer.More():
        wire = TopoDS.Wire(wire_explorer.Current())
        points: list[tuple[float, float, float]] = []
        wire_cursor = BRepTools_WireExplorer(wire)
        while wire_cursor.More():
            edge = wire_cursor.Current()
            curve = BRepAdaptor_Curve(edge)
            discretizer = GCPnts_QuasiUniformDeflection(curve, float(deflection))
            if discretizer.IsDone() and discretizer.NbPoints() >= 2:
                segment = [(discretizer.Value(i).X(), discretizer.Value(i).Y(),
                            discretizer.Value(i).Z())
                           for i in range(1, discretizer.NbPoints() + 1)]
                # **必须**按边在环里的走向取点：BRepAdaptor_Curve 给的是曲线自身的参数方向，
                # 而边在 wire 里可能是反着用的（Orientation == REVERSED）。
                # 不翻转的话折线会来回折返（A→B→A），轮廓直接算错。
                if edge.Orientation() == TopAbs_REVERSED:
                    segment.reverse()
                # 相邻边首尾相接，去掉重复的连接点
                if points and _close(points[-1], segment[0]):
                    points.extend(segment[1:])
                else:
                    points.extend(segment)
            wire_cursor.Next()
        if len(points) >= 3:
            array = np.asarray(points, dtype=np.float64)
            if _close(tuple(array[0]), tuple(array[-1])):
                array = array[:-1]
            if array.shape[0] >= 3:
                loops.append(array)
        wire_explorer.Next()
    return tuple(loops)


def _close(a: tuple[float, ...], b: tuple[float, ...], tolerance: float = 1e-6) -> bool:
    return all(abs(float(x) - float(y)) <= tolerance for x, y in zip(a, b))


def _triangle_normals(positions: NDArray[np.float64], indices: NDArray[np.int64]
                      ) -> NDArray[np.float64]:
    if indices.shape[0] == 0:
        return np.zeros((0, 3), dtype=np.float64)
    corners = positions[indices]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    return normals / np.where(lengths > 1e-12, lengths, 1.0)


def _loop_bounds(loops: tuple[NDArray[np.float64], ...]
                 ) -> tuple[float, float, float, float, float, float] | None:
    if not loops:
        return None
    stacked = np.vstack(loops)
    low = stacked.min(axis=0)
    high = stacked.max(axis=0)
    return (float(low[0]), float(low[1]), float(low[2]),
            float(high[0]), float(high[1]), float(high[2]))


__all__ = ["DEFAULT_BOUNDARY_DEFLECTION", "MeshOptions", "to_tessellated_model"]
