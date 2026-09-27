"""BRep 模型：读文件、查拓扑、修复、量尺寸。

**只做 BRep**，不产生网格也不产生刀路。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.brep.backend import OcpUnavailableError, require_ocp
from toolpath_lab.brep.errors import BrepFormatError

logger = logging.getLogger(__name__)

#: 支持的后缀 -> OCP 读取器类型。
STEP_SUFFIXES = (".step", ".stp", ".stpz")
IGES_SUFFIXES = (".iges", ".igs")
SUPPORTED_SUFFIXES = STEP_SUFFIXES + IGES_SUFFIXES
#: 上传接口允许的后缀（给前端对话框与后端校验共用）。
ALLOWED_SUFFIXES = SUPPORTED_SUFFIXES
#: 单个模型文件的默认体积上限：32 MB。
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

#: 面类型枚举 -> 可读名字（GeomAbs_SurfaceType 的数值）。
SURFACE_KIND_NAMES = {
    0: "plane", 1: "cylinder", 2: "cone", 3: "sphere", 4: "torus",
    5: "bspline", 6: "bezier", 7: "revolution", 8: "extrusion",
    9: "offset", 10: "other",
}


@dataclass(slots=True)
class BrepFaceInfo:
    """一个面的轻量描述（供特征识别与调试用）。"""

    index: int
    kind: str
    area_mm2: float
    #: 外法向（平面才有意义；曲面给面中心处的法向）
    normal: tuple[float, float, float]
    center: tuple[float, float, float]
    is_planar: bool
    #: 平面方程 (nx, ny, nz, d)，满足 n·p = d
    plane: tuple[float, float, float, float] | None = None


@dataclass(slots=True)
class BrepModel:
    """一个 BRep 实体（或复合体）。

    ``shape`` 是 OCP 的 ``TopoDS_Shape``，不在这里做深拷贝 —— 它是句柄语义，传引用很便宜。
    """

    shape: Any
    name: str = ""
    source: str = ""
    #: 由 :meth:`refresh` 填充
    faces: list[BrepFaceInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 拓扑统计
    def refresh(self) -> "BrepModel":
        """重新统计拓扑与面的几何信息。修改过 shape 之后要调一次。"""

        self.faces = list(_iterate_faces(self.shape))
        return self

    @property
    def face_count(self) -> int:
        return len(self.faces)

    def counts(self) -> dict[str, int]:
        """各拓扑类型的数量。探索器是按类型的，这里逐个查一遍。"""

        from OCP.TopAbs import (TopAbs_COMPOUND, TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL,
                                TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE)
        from OCP.TopExp import TopExp_Explorer

        out: dict[str, int] = {}
        for label, enum in (("solids", TopAbs_SOLID), ("shells", TopAbs_SHELL),
                            ("faces", TopAbs_FACE), ("wires", TopAbs_WIRE),
                            ("edges", TopAbs_EDGE), ("vertices", TopAbs_VERTEX),
                            ("compounds", TopAbs_COMPOUND)):
            explorer = TopExp_Explorer(self.shape, enum)
            count = 0
            while explorer.More():
                count += 1
                explorer.Next()
            out[label] = count
        return out

    def bounds(self) -> tuple[float, float, float, float, float, float]:
        """(x_min, y_min, z_min, x_max, y_max, z_max)。

        注意用 ``GetXMin()`` 这一组访问器，**不要**用 ``Bnd_Box.Get()``：
        后者返回未注册的 ``Bnd_Box::Limits`` 结构，OCP 8 会直接抛 TypeError。
        """

        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        box = Bnd_Box()
        # Bnd_Box 默认带 1e-7 量级的 gap，会把尺寸报成 100.0000002 这种值；
        # 加工尺寸要精确，先清掉。
        box.SetGap(0.0)
        BRepBndLib.Add_s(self.shape, box)
        if box.IsVoid():
            raise ValueError("模型的包围盒是空的：可能没有实体几何")
        return (box.GetXMin(), box.GetYMin(), box.GetZMin(),
                box.GetXMax(), box.GetYMax(), box.GetZMax())

    def size(self) -> tuple[float, float, float]:
        x0, y0, z0, x1, y1, z1 = self.bounds()
        return (x1 - x0, y1 - y0, z1 - z0)

    def volume_mm3(self) -> float:
        """精确体积（散度定理，OpenCascade 解析计算，不是网格近似）。"""

        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(self.shape, props)
        return float(props.Mass())

    def surface_area_mm2(self) -> float:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(self.shape, props)
        return float(props.Mass())

    def is_valid(self) -> bool:
        from OCP.BRepCheck import BRepCheck_Analyzer

        return bool(BRepCheck_Analyzer(self.shape).IsValid())

    def statistics(self) -> dict[str, Any]:
        x0, y0, z0, x1, y1, z1 = self.bounds()
        kinds: dict[str, int] = {}
        for face in self.faces:
            kinds[face.kind] = kinds.get(face.kind, 0) + 1
        return {
            "name": self.name,
            "faces": self.face_count,
            "topology": self.counts(),
            "surface_kinds": kinds,
            "volume_mm3": round(self.volume_mm3(), 3),
            "area_mm2": round(self.surface_area_mm2(), 3),
            "size_mm": [round(v, 4) for v in (x1 - x0, y1 - y0, z1 - z0)],
            "bounds": [round(v, 4) for v in (x0, y0, z0, x1, y1, z1)],
            "valid": self.is_valid(),
            "warnings": list(self.warnings),
        }

    def to_payload(self) -> dict[str, Any]:
        return self.statistics()

    # ------------------------------------------------------------ 变换
    def translated(self, offset: Iterable[float]) -> "BrepModel":
        """平移（返回新模型，不改原对象）。常用于把零件放到机床坐标系。"""

        from OCP.gp import gp_Trsf, gp_Vec
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

        dx, dy, dz = (float(v) for v in offset)
        transform = gp_Trsf()
        transform.SetTranslation(gp_Vec(dx, dy, dz))
        moved = BRepBuilderAPI_Transform(self.shape, transform, True).Shape()
        model = BrepModel(shape=moved, name=self.name, source=self.source)
        return model.refresh()

    def normalized(self) -> "BrepModel":
        """归一化到机床坐标系：XY 居中、Z 最低点为 0。

        和手写解析器保持一致的约定，这样上层（毛坯、仿真）不用改。
        """

        x0, y0, z0, x1, y1, z1 = self.bounds()
        return self.translated((-(x0 + x1) / 2.0, -(y0 + y1) / 2.0, -z0))

    # ------------------------------------------------------------ 修复
    def healed(self, *, tolerance: float = 1e-6, max_iterations: int = 3,
               sew: bool = True) -> "BrepModel":
        """修复模型：缝合自由边、统一面方向、修小边小面。

        CAD 导出的 BRep 经常有微小缝隙或反向面，直接离散会得到破网格。
        修复后再离散，网格质量会明显好一截。
        """

        from OCP.ShapeFix import ShapeFix_Shape
        from OCP.ShapeBuild import ShapeBuild_ReShape

        fixer = ShapeFix_Shape(self.shape)
        fixer.SetPrecision(float(tolerance))
        fixer.SetMaxTolerance(float(tolerance) * 1000.0)
        context = ShapeBuild_ReShape()
        fixer.SetContext(context)
        for _ in range(max(1, int(max_iterations))):
            if not fixer.Perform():
                break
        healed = BrepModel(shape=fixer.Shape(), name=self.name, source=self.source)
        if sew:
            healed = _sew_if_shells(healed, tolerance=tolerance)
        return healed.refresh()


def _sew_if_shells(model: BrepModel, *, tolerance: float) -> BrepModel:
    """有多个壳时尝试缝合（缝完还是多个也不报错，只记警告）。"""

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    counts = model.counts()
    if counts.get("shells", 0) <= 1:
        return model
    sewing = BRepBuilderAPI_Sewing(float(tolerance))
    sewing.Add(model.shape)
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn.IsNull():
        model.warnings.append("缝合失败，保留原模型")
        return model
    out = BrepModel(shape=sewn, name=model.name, source=model.source)
    out.warnings = list(model.warnings)
    if sewing.NbFreeEdges() > 0:
        out.warnings.append(f"缝合后仍有 {sewing.NbFreeEdges()} 条自由边（模型不是封闭实体）")
    return out


def _iterate_faces(shape: Any):
    """遍历所有面，产出 :class:`BrepFaceInfo`。"""

    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    index = 0
    while explorer.More():
        face = TopoDS.Face(explorer.Current())
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        center = props.CentreOfMass()
        adaptor = BRepAdaptor_Surface(face)
        raw_kind = int(adaptor.GetType())
        kind = SURFACE_KIND_NAMES.get(raw_kind, "other")
        is_planar = raw_kind == 0
        normal = (0.0, 0.0, 1.0)
        plane = None
        if is_planar:
            gp_plane = adaptor.Plane()
            axis = gp_plane.Axis().Direction()
            location = gp_plane.Location()
            nx, ny, nz = float(axis.X()), float(axis.Y()), float(axis.Z())
            d = nx * float(location.X()) + ny * float(location.Y()) + nz * float(location.Z())
            # 面的方向可能是反的，按拓扑朝向翻一下，外法向才对
            if face.Orientation() == 1:  # TopAbs_REVERSED
                nx, ny, nz, d = -nx, -ny, -nz, -d
            normal = (nx, ny, nz)
            plane = (nx, ny, nz, d)
        yield BrepFaceInfo(
            index=index, kind=kind, area_mm2=float(props.Mass()), normal=normal,
            center=(float(center.X()), float(center.Y()), float(center.Z())),
            is_planar=is_planar, plane=plane,
        )
        index += 1
        explorer.Next()


# ---------------------------------------------------------------- 读文件
def load_brep(path: str | Path, *, heal: bool = False, normalize: bool = False,
              name: str = "") -> BrepModel:
    """读 STEP / IGES。

    :param path: 文件路径
    :param heal: 是否做一次 ShapeFix（模型不干净时打开，会慢一点）
    :param normalize: 是否归一化到机床坐标系（XY 居中、Z 最低点 0）
    :param name: 覆盖模型名；默认用文件名
    :raises OcpUnavailableError: 没装 OCP
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 后缀不支持，或读取失败（附 OCP 的失败原因）
    """

    require_ocp()
    file_path = Path(path)
    # 先判后缀再判存在：扩展名写错时，"不支持的后缀"比"找不到文件"更有指导性
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise BrepFormatError(f"不支持的后缀 {suffix!r}，支持 {', '.join(SUPPORTED_SUFFIXES)}")
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到模型文件：{file_path}")

    import time

    started = time.perf_counter()
    if suffix in STEP_SUFFIXES:
        shape = _read_step(file_path)
    else:
        shape = _read_iges(file_path)
    elapsed = time.perf_counter() - started

    if shape is None or shape.IsNull():
        raise BrepFormatError(f"OCP 无法从 {file_path.name} 里读出任何几何（文件可能损坏或不是实体模型）")

    model = BrepModel(shape=shape, name=name or file_path.stem, source=str(file_path))
    model.refresh()
    logger.info("读取 %s：%d 个面，耗时 %.3fs", file_path.name, model.face_count, elapsed)

    if heal:
        before = model.face_count
        model = model.healed()
        logger.info("修复：面数 %d -> %d，有效 %s", before, model.face_count, model.is_valid())
    if normalize:
        model = model.normalized()
    return model


def _read_step(file_path: Path):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone

    reader = STEPControl_Reader()
    status = reader.ReadFile(str(file_path))
    if status != IFSelect_RetDone:
        raise BrepFormatError(f"STEP 读取失败（状态 {status}）")
    # 有的文件根对象不带实体，需要把 transferable roots 全部搬过来
    reader.TransferRoots()
    if reader.NbShapes() <= 0:
        return None
    return reader.OneShape()


def _read_iges(file_path: Path):
    from OCP.IGESControl import IGESControl_Reader
    from OCP.IFSelect import IFSelect_RetDone

    reader = IGESControl_Reader()
    status = reader.ReadFile(str(file_path))
    if status != IFSelect_RetDone:
        raise BrepFormatError(f"IGES 读取失败（状态 {status}）")
    reader.TransferRoots()
    if reader.NbShapes() <= 0:
        return None
    return reader.OneShape()


__all__ = ["BrepFaceInfo", "BrepModel", "SUPPORTED_SUFFIXES", "SURFACE_KIND_NAMES",
           "load_brep"]
