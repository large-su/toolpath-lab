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

import numpy as np

from toolpath_lab.brep import load_brep, to_tessellated_model
from toolpath_lab.brep.tessellate_model import MeshOptions
from toolpath_lab.core.part import build_part

__all__ = ["cylinder_boss", "fixture_shape", "plate_with_curved_pocket", "plate_with_cylinder",
           "plate_with_pocket", "plate_with_sloped_pocket", "simple_box", "solid_and_part",
           "solid_bytes", "to_part"]

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


def _rotate(shape, origin: tuple[float, float, float], axis: tuple[float, float, float],
            angle_deg: float):
    """绕经过 origin、方向为 axis 的轴旋转。

    .. warning::
       OCC 的 ``gp_Trsf.SetRotation(gp_Ax1, angle)`` 对**旋转轴上的点**处理得并不
       直观（实测绕 (0,0,25) 旋转 10° 的大方块，底面最终落在 z=0 而不是 z=25）。
       型腔夹具因此**不用旋转**，改用 :func:`_profile_prism` 直接拉伸截面，
       见 :func:`_sloped_pocket_shape`。这个辅助函数只留给不需要精确落位的场合。
    """

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

    transform = gp_Trsf()
    transform.SetRotation(
        gp_Ax1(gp_Pnt(*[float(v) for v in origin]), gp_Dir(*[float(v) for v in axis])),
        float(np.radians(angle_deg)),
    )
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _cut(base, tool):
    cut, *_ = _ocp()
    return cut(base, tool).Shape()


def _vertical_prism(x_min: float, x_max: float, y_min: float, y_max: float,
                    z_top: float, z_bottom: float):
    """竖直棱柱：XY 上是矩形，Z 方向从 z_top 拉到 z_bottom（型腔侧壁的来源）。"""

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    polygon = BRepBuilderAPI_MakePolygon()
    for x, y in ((x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max)):
        polygon.Add(gp_Pnt(float(x), float(y), float(z_top)))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    solid = BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, float(z_bottom - z_top))).Shape()
    return solid


def _profile_prism(profile_xz: np.ndarray, y_min: float, y_max: float):
    """把 XZ 平面上的闭合多边形沿 +Y 拉伸成实体。

    这是造"底面是斜面/曲面"的型腔**最稳**的办法：型腔底面的形状直接在截面里画出来，
    不依赖旋转、也不依赖布尔运算对旋转实体的处理（那些都踩过坑，见 :func:`_rotate`）。
    侧壁依旧是竖直的，因为拉伸方向是 Y —— 沿 X 的两个边界就是竖直平面。
    """

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    points = np.asarray(profile_xz, dtype=np.float64).reshape(-1, 2)
    polygon = BRepBuilderAPI_MakePolygon()
    for x, z in points:
        polygon.Add(gp_Pnt(float(x), float(y_min), float(z)))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, float(y_max - y_min), 0.0)).Shape()


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


def plate_with_sloped_pocket(width: float = 100.0, depth: float = 80.0, height: float = 40.0,
                             pocket_x: float = 60.0, pocket_y: float = 40.0,
                             floor_center_z: float = 25.0, floor_angle_deg: float = 10.0,
                             name: str = "sloped_pocket"):
    """**斜底**型腔：侧壁竖直，底面绕 Y 轴倾斜 floor_angle_deg。

    这是"倾斜平面型腔"的教科书形态，也是实际零件里最常见的一种
    （拔模后的台阶底、需要排液的斜底腔）。底面高度沿 X 线性变化：

        z(x) = floor_center_z - x·tan(floor_angle_deg)

    ``floor_center_z`` 是型腔中心处的底面高度（x = 0）。

    做法：竖直棱柱 ∩ 斜面**上方**空间，再从板上切掉 —— 棱柱保证侧壁竖直，
    斜面提供底面坡度。用旋转后的方块做交会更稳（布尔运算里没有无穷大半空间）。
    """

    return _to_part(_sloped_pocket_shape(
        width=width, depth=depth, height=height, pocket_x=pocket_x, pocket_y=pocket_y,
        floor_center_z=floor_center_z, floor_angle_deg=floor_angle_deg,
    ), name)


def plate_with_curved_pocket(width: float = 100.0, depth: float = 80.0, height: float = 45.0,
                             pocket_x: float = 60.0, pocket_y: float = 40.0,
                             floor_center_z: float = 30.0, floor_radius: float = 100.0,
                             name: str = "curved_pocket"):
    """**曲底**型腔：侧壁竖直，底面是圆柱面（轴沿 Y）。

    底面高度沿 X 按圆弧变化：

        z(x) = axis_z - sqrt(floor_radius² - x²),  axis_z = floor_center_z + floor_radius

    ``floor_center_z`` 是型腔中心处的底面高度。曲面底是"曲面型腔"的代表形态
    （滚道、叶片流道、模具的曲面型腔都长这样）。默认参数：型腔中心 30，
    到腔壁（x = ±30）升到约 34.6，弓高 4.6 mm、最大坡度约 17°。

    板要足够厚（这里 50）：圆弧在腔壁处还要继续往上走，板太薄的话弧线会从**顶面穿出去**，
    型腔就不再是"竖直壁 + 曲底"，而成了一片浅碟（踩过一次）。
    """

    return _to_part(_curved_pocket_shape(
        width=width, depth=depth, height=height, pocket_x=pocket_x, pocket_y=pocket_y,
        floor_center_z=floor_center_z, floor_radius=floor_radius,
    ), name)


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
    if kind == "sloped_pocket":
        return _sloped_pocket_shape(**kwargs)
    if kind == "curved_pocket":
        return _curved_pocket_shape(**kwargs)
    raise ValueError(f"未知夹具 {kind!r}")  # pragma: no cover - 夹具用法错误


def _assert_pocket_floor(shape, floor_center_z: float, label: str, *,
                         min_span_mm: float = 3.0) -> None:
    """夹具自检：真的做出了一个**倾斜/曲面**底面，而不是退化成平底。

    布尔/拉伸写错时最典型的症状就是"看着像型腔、其实底是平的"，
    所以在夹具这一层就把结果查一遍：型腔范围内的顶点（落在型腔 XY 内、
    且明显低于零件顶面）最低点与最高点必须有可观的落差。
    """

    part = _to_part(shape, "fixture-check")
    positions = np.asarray(part.mesh.positions, dtype=np.float64)
    indices = np.asarray(part.mesh.indices, dtype=np.int64)
    if indices.shape[0] == 0 or positions.shape[0] == 0:  # pragma: no cover
        raise AssertionError(f"夹具 {label} 的网格是空的")
    z_max = float(part.bounds.z_max)
    # 量型腔内"低于零件顶面"的顶点落差。
    #
    # 注意两点：
    # * 范围要贴着腔壁取（x=±30 / y=±20）：底面离散后的顶点恰好落在腔壁上，
    #   内缩取值会一个顶点都选不到，检查会误报"底面退化成平底"。
    # * 曲底面离散后会变成**一串小平面片**（不是单个 cylinder 面），所以这里不看面类型，
    #   只按 z 判据选顶点。斜底型腔按 (25, 10°) 摆，底面在 z∈[19.5, 30.5]；
    #   曲底型腔按 (35, R60) 摆，底面在 z∈[31.2, 35.0]，两者都远低于顶面 40。
    in_pocket = ((np.abs(positions[:, 0]) < 30.5) & (np.abs(positions[:, 1]) < 20.5)
                 & (positions[:, 2] < z_max - 1.0))
    if not in_pocket.any():  # pragma: no cover - 夹具几何写错时
        room = positions[positions[:, 2] < z_max - 3.0]
        raise AssertionError(
            f"夹具 {label} 里找不到型腔底部的点：x∈[{room[:, 0].min():.1f},{room[:, 0].max():.1f}] "
            f"y∈[{room[:, 1].min():.1f},{room[:, 1].max():.1f}] "
            f"z∈[{room[:, 2].min():.1f},{room[:, 2].max():.1f}]" if room.size else
            f"夹具 {label} 里没有低于顶面的顶点"
        )
    z_lo = float(positions[in_pocket, 2].min())
    z_hi = float(positions[in_pocket, 2].max())
    span = z_hi - z_lo
    if span < min_span_mm:  # pragma: no cover - 夹具几何退化成平底
        detail = (f"可选顶点 x∈[{positions[in_pocket, 0].min():.1f},"
                  f"{positions[in_pocket, 0].max():.1f}] "
                  f"z∈[{z_lo:.3f},{z_hi:.3f}] n={int(in_pocket.sum())}")
        raise AssertionError(
            f"夹具 {label} 的底面落差只有 {span:.3f} mm（期望 ≥ {min_span_mm}）；{detail}"
        )
    if abs(z_lo - float(floor_center_z)) > 25.0:  # pragma: no cover - 高度离谱
        raise AssertionError(
            f"夹具 {label} 的底面高度 {z_lo:.2f} 与设定的中心高度 {floor_center_z} 差得太远"
        )


def _sloped_floor_profile(half_width: float, center_z: float, angle_deg: float,
                          cap_z: float) -> np.ndarray:
    """斜底型腔的截面（XZ）：下边界是斜线，上边界是 ``cap_z`` 的水平线。

    注意这里画的是**被切掉的型腔实体**（底面在斜面、向上到 cap_z 敞开），
    不是"底面以下的材料" —— 画反了会得到一个开口朝下的槽（底面变成顶面），
    而且顶点数、面数看着都很正常，只有量一下材料在哪一侧才发现（踩过一次）。

    OCC 的 ``SetRotation`` 落位不直观、``Common`` 对轴对齐长方体会走包围盒优化，
    所以底面形状直接在截面里画出来，不靠旋转或布尔运算来"摆"。
    """

    slope = np.tan(np.radians(angle_deg))
    return np.asarray([
        [-half_width, center_z + half_width * slope],
        [half_width, center_z - half_width * slope],
        [half_width, cap_z],
        [-half_width, cap_z],
    ], dtype=np.float64)


def _curved_floor_profile(half_width: float, center_z: float, radius: float,
                          cap_z: float) -> np.ndarray:
    """曲底型腔的截面（XZ）：下边界是圆弧（轴在型腔中心正上方），上边界是 ``cap_z``。"""

    axis_z = float(center_z) + float(radius)
    xs = np.linspace(-half_width, half_width, 41)
    zs = axis_z - np.sqrt(np.maximum(radius ** 2 - xs ** 2, 0.0))
    return np.vstack([
        np.column_stack((xs, zs)),
        [[half_width, cap_z], [-half_width, cap_z]],
    ])


def _pocket_from_profile(width: float, depth: float, height: float,
                         pocket_x: float, pocket_y: float, profile: np.ndarray):
    """把截面沿 Y 拉伸成"型腔要被切掉的实体"，再从板上切掉（侧壁因此是竖直的）。"""

    plate = _box(width, depth, height)
    pocket = _profile_prism(profile, -pocket_y / 2.0, pocket_y / 2.0)
    return _cut(plate, pocket)


def _sloped_pocket_shape(width: float = 100.0, depth: float = 80.0, height: float = 40.0,
                         pocket_x: float = 60.0, pocket_y: float = 40.0,
                         floor_center_z: float = 25.0, floor_angle_deg: float = 10.0,
                         **_ignored):
    """斜底型腔的 OCP 形状（与 :func:`plate_with_sloped_pocket` 同一套几何）。"""

    profile = _sloped_floor_profile(pocket_x / 2.0, floor_center_z, floor_angle_deg,
                                    cap_z=height + 2.0)
    result = _pocket_from_profile(width, depth, height, pocket_x, pocket_y, profile)
    _assert_pocket_floor(result, floor_center_z, "斜底型腔")
    return result


def _curved_pocket_shape(width: float = 100.0, depth: float = 80.0, height: float = 45.0,
                         pocket_x: float = 60.0, pocket_y: float = 40.0,
                         floor_center_z: float = 30.0, floor_radius: float = 100.0,
                         **_ignored):
    """曲底型腔的 OCP 形状（与 :func:`plate_with_curved_pocket` 同一套几何）。"""

    profile = _curved_floor_profile(pocket_x / 2.0, floor_center_z, floor_radius,
                                    cap_z=height + 2.0)
    result = _pocket_from_profile(width, depth, height, pocket_x, pocket_y, profile)
    _assert_pocket_floor(result, floor_center_z, "曲底型腔")
    return result


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
