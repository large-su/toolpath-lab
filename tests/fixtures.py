"""测试夹具：用 **OCP** 造几何，不再手写 STEP 文本。

以前这里是"手写 STEP 写入器 + 手写解析器"配对：写入器怎么写、解析器就怎么读，
于是**真实导出器的写法一旦和写入器不同，测试就永远发现不了**
（``*`` 派生属性、``ORIENTED_EDGE`` 反向、直线 magnitude 三个 bug 都是这么漏过去的）。

现在几何由 OpenCascade 生成，和真实 CAD 走同一套内核，夹具本身的合法性不再需要怀疑。
每个夹具同时提供两种形态：

* ``plate_with_pocket()`` 等 -> 直接返回 :class:`PartModel`（单元测试用，最快）；
* :func:`solid_bytes` -> 写成真正的 STEP 字节（HTTP 上传测试用）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from toolpath_lab.brep import load_brep, to_tessellated_model
from toolpath_lab.brep.tessellate_model import MeshOptions
from toolpath_lab.core.part import build_part

__all__ = ["cylinder_boss", "fixture_shape", "plate_with_cylinder", "plate_with_pocket",
           "simple_box", "solid_and_part", "solid_bytes", "to_part"]

#: 夹具用的离散精度：比默认细一点，让面积/体积断言更接近解析值。
FIXTURE_MESH = MeshOptions(linear_deflection=0.02, angular_deflection=0.35,
                           boundary_deflection=0.02)


def _ocp():
    """延迟导入 OCP，缺依赖时给出清晰错误。"""

    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    except ImportError as error:  # pragma: no cover - 取决于环境
        raise RuntimeError("测试夹具需要 OCP：python -m pip install cadquery-ocp") from error
    return (BRepAlgoAPI_Cut, BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
            gp_Ax2, gp_Dir, gp_Pnt)


def _translate(shape, dz: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    transform = gp_Trsf()
    transform.SetTranslation(gp_Vec(0.0, 0.0, float(dz)))
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _box(width: float, depth: float, height: float, *, z: float = 0.0, centered: bool = True):
    """长方体。

    注意 ``BRepPrimAPI_MakeBox(gp_Pnt(...), dx, dy, dz)`` 的参考点是**角点**、不是中心：
    直接传原点会让长方体落在第一象限，外轮廓和型腔就不再同心 —— 型腔会跑到边上去变成
    一个角缺口（面数从 11 掉到 9，体积也全错）。所以默认把角点挪到 ``(-w/2, -d/2, z)``。
    """

    _, make_box, _, _, _, gp_Pnt = _ocp()
    x = -width / 2.0 if centered else 0.0
    y = -depth / 2.0 if centered else 0.0
    return make_box(gp_Pnt(x, y, float(z)), float(width), float(depth), float(height)).Shape()


def _cylinder(radius: float, height: float, *, z: float = 0.0):
    _, _, make_cylinder, gp_Ax2, gp_Dir, gp_Pnt = _ocp()
    axis = gp_Ax2(gp_Pnt(0.0, 0.0, float(z)), gp_Dir(0.0, 0.0, 1.0))
    return make_cylinder(axis, float(radius), float(height)).Shape()


def _cut(base, tool):
    cut, *_ = _ocp()
    return cut(base, tool).Shape()


def _plate_shape(width: float, depth: float, height: float,
                 pocket_x: float, pocket_y: float, pocket_depth: float):
    """带**向上开口**型腔的板：型腔底面在 ``z = height - pocket_depth``，顶面是口字形框。

    为什么开口朝上：加工面必须朝上才能铣（``region_from_face`` 会拒绝朝下的面）。
    旧夹具是从底面往上挖的 —— 那样型腔底面的外法向朝**下**，几何上自相矛盾
    （材料在它上方、空气在下方），只是旧解析器不校验法向才没暴露。
    现在几何由 OpenCascade 生成，法向是对的，夹具也必须真的可加工。
    多切 1mm 穿过顶面，保证布尔切得干净。
    """

    plate = _box(width, depth, height)
    pocket = _box(pocket_x, pocket_y, pocket_depth + 1.0, z=height - pocket_depth)
    return _cut(plate, pocket)


def _to_part(shape, name: str):
    """OCP shape -> BrepModel -> PartModel（走真实的那条转换链）。"""

    from toolpath_lab.brep.model import BrepModel

    model = BrepModel(shape=shape, name=name).refresh()
    mesh = to_tessellated_model(model, FIXTURE_MESH, normalize=True)
    return build_part(mesh, model_id=name, name=name)


# ------------------------------------------------------------------ 夹具
def simple_box(width: float = 40.0, depth: float = 30.0, height: float = 20.0,
               name: str = "box"):
    """长方体：6 个平面面。体积 = 长×宽×高。"""

    return _to_part(_box(width, depth, height), name)


def plate_with_pocket(width: float = 100.0, depth: float = 80.0, height: float = 40.0,
                      pocket_x: float = 60.0, pocket_y: float = 40.0,
                      pocket_depth: float = 15.0, name: str = "plate"):
    """带型腔的板：外轮廓 6 个平面 + 型腔 5 个面（共 11 个面）。

    型腔**从底面贯通**到 ``z = height - pocket_depth``，所以底面是带方孔的框，
    体积 = ``width*depth*height - pocket_x*pocket_y*(height - pocket_depth)``。
    """

    return _to_part(_plate_shape(width, depth, height, pocket_x, pocket_y, pocket_depth), name)


def plate_with_cylinder(width: float = 100.0, depth: float = 80.0, height: float = 40.0,
                        hole_diameter: float = 30.0, name: str = "plate_hole"):
    """带**通孔**的板：上下两个带孔平面 + 4 个侧面 + 圆柱孔壁 = 7 个面。

    体积 = ``width*depth*height - π r² * height``。
    """

    plate = _box(width, depth, height)
    hole = _cylinder(hole_diameter / 2.0, height + 2.0, z=-1.0)
    return _to_part(_cut(plate, hole), name)


def cylinder_boss(diameter: float = 60.0, height: float = 50.0, name: str = "boss"):
    """圆柱体：上下两个平面 + 一个圆柱侧面。体积 = π r² h。"""

    return _to_part(_cylinder(diameter / 2.0, height), name)


# ------------------------------------------------------------------ STEP 字节
def fixture_shape(kind: str = "plate", **kwargs):
    """夹具的 OCP 形状（BRep）。"""

    if kind == "box":
        return _box(kwargs.get("width", 40.0), kwargs.get("depth", 30.0),
                    kwargs.get("height", 20.0))
    if kind == "plate":
        return _plate_shape(kwargs.get("width", 100.0), kwargs.get("depth", 80.0),
                            kwargs.get("height", 40.0), kwargs.get("pocket_x", 60.0),
                            kwargs.get("pocket_y", 40.0), kwargs.get("pocket_depth", 15.0))
    if kind == "cylinder":
        return _cylinder(kwargs.get("diameter", 60.0) / 2.0, kwargs.get("height", 50.0))
    raise ValueError(f"未知夹具 {kind!r}")  # pragma: no cover - 夹具用法错误


def solid_and_part(kind: str = "plate", **kwargs):
    """一次拿到夹具的三样东西：``(PartModel, BrepModel, bytes)``。

    等高铣要**按层剖切 BRep**，所以接入测试不能只有 :class:`PartModel`——
    这里返回的 BRep 与零件网格出自同一次归一化，坐标系一定对齐。
    顺手把 STEP 字节也给了，HTTP 上传测试不用再算一遍。
    """

    from toolpath_lab.brep.model import BrepModel

    shape = fixture_shape(kind, **kwargs)
    brep = BrepModel(shape=shape, name=kind, source="fixture").normalized()
    model = to_tessellated_model(brep, FIXTURE_MESH, normalize=False)
    part = build_part(model, model_id=f"{kind}-fixture", name=kind)
    return part, brep, solid_bytes(kind, **kwargs)


def solid_bytes(kind: str = "plate", **kwargs) -> bytes:
    """把夹具写成**真正的 STEP 文件**字节，供 HTTP 上传测试使用。

    刻意走 OCP 的 ``STEPControl_Writer``：测试上传的就是真实 CAD 会导出的那种文件，
    而不是自家写入器生成的"只有自家解析器认"的文本。
    """

    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    shape = fixture_shape(kind, **kwargs)
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    with tempfile.TemporaryDirectory(prefix="tplab-fx-") as folder:
        path = Path(folder) / f"{kind}.step"
        writer.Write(str(path))
        return path.read_bytes()


def to_part(path: str | Path, *, name: str = ""):
    """从磁盘上的模型文件构造 PartModel（夹具与真实文件都可以）。"""

    stem = Path(path).stem
    model = load_brep(path, normalize=False, name=name or stem)
    return build_part(to_tessellated_model(model, FIXTURE_MESH, normalize=True),
                      model_id=stem, name=name or stem)
