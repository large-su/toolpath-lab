"""加工区域。

区域就是"要加工的那块地方"。它现在有三种来源：

- 方形（square）：一个边长；
- 圆形（circle）：一个直径；
- 导入模型（model）：用模型的 XY 投影轮廓（凸包或包围盒），可以外扩 / 内缩。

所有形状统一归约为一条**逆时针、不重复首点**的边界多边形。栅格刀路只会用到
"一条直线与多边形求交"，因此新增形状（椭圆、跑道形、凹多边形……）只要实现一个
boundary() 就能直接参与规划，不需要改任何刀路代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import pi
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    field_values,
    spec,
)
from toolpath_lab.core.registry import Registry

REGION_SHAPES: Registry[type["RegionShape"]] = Registry("region shape")

#: 圆用多少段折线逼近；固定值，避免把离散精度暴露成一个意义不大的参数。
CIRCLE_SEGMENTS = 180


@dataclass(frozen=True, slots=True)
class RegionShape:
    """所有区域形状的基类。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()
    #: 需要先导入模型才能构造（例如"模型轮廓"）。
    needs_model: ClassVar[bool] = False

    def boundary(self) -> NDArray[np.float64]:
        """逆时针闭合边界，形状 (N, 2)，不重复首点。"""

        raise NotImplementedError

    def to_params(self) -> dict[str, Any]:
        return field_values(self)

    def describe(self) -> dict[str, Any]:
        polygon = self.boundary()
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "parameters": self.to_params(),
            "area_mm2": polygon_area(polygon),
            "bounds_mm": polygon_bounds(polygon),
        }


def polygon_area(polygon: NDArray[np.float64]) -> float:
    """多边形的有向面积（逆时针为正）。"""

    x = polygon[:, 0]
    y = polygon[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def polygon_bounds(polygon: NDArray[np.float64]) -> list[list[float]]:
    """轴对齐包围盒，形式为 [[x_min, x_max], [y_min, y_max]]。"""

    return [
        [float(polygon[:, 0].min()), float(polygon[:, 0].max())],
        [float(polygon[:, 1].min()), float(polygon[:, 1].max())],
    ]


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class SquareRegion(RegionShape):
    """以原点为中心的方形区域。"""

    side_mm: float = 80.0

    id: ClassVar[str] = "square"
    label: ClassVar[str] = "方形"
    description: ClassVar[str] = "面铣最常见的形状，用来对比往复与单向"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("side_mm", "边长", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
        )
    )

    def __post_init__(self) -> None:
        if self.side_mm <= 0:
            raise ParameterError("方形边长必须为正")

    def boundary(self) -> NDArray[np.float64]:
        half = self.side_mm / 2.0
        return np.array(
            [(-half, -half), (half, -half), (half, half), (-half, half)],
            dtype=np.float64,
        )


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class CircleRegion(RegionShape):
    """以原点为中心的圆形区域。"""

    diameter_mm: float = 80.0

    id: ClassVar[str] = "circle"
    label: ClassVar[str] = "圆形"
    description: ClassVar[str] = "圆形端面，用来观察刀路在曲线边界上的收放"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("diameter_mm", "直径 D", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
        )
    )

    def __post_init__(self) -> None:
        if self.diameter_mm <= 0:
            raise ParameterError("圆形直径必须为正")

    def boundary(self) -> NDArray[np.float64]:
        radius = self.diameter_mm / 2.0
        angles = np.linspace(0.0, 2.0 * pi, CIRCLE_SEGMENTS, endpoint=False)
        return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class ModelRegion(RegionShape):
    """导入模型的 XY 投影轮廓，可以整体外扩或内缩。

    这样"导入一个模型"之后不需要再手工量尺寸：零件的投影轮廓就是加工区域。
    margin_mm 为正表示外扩（留出安全边距），为负表示内缩（只加工轮廓以内）。
    """

    outline: str = "hull"
    margin_mm: float = 0.0
    _model: Any = field(default=None, repr=False, compare=False)

    id: ClassVar[str] = "model"
    label: ClassVar[str] = "模型轮廓"
    description: ClassVar[str] = "用导入模型的 XY 投影轮廓作为加工区域，可外扩 / 内缩"
    needs_model: ClassVar[bool] = True
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("outline", "轮廓方式", K.CHOICE, "hull", group="区域", choices=(
                Choice("hull", "凸包轮廓"),
                Choice("box", "矩形包围盒"),
            ), help="凸包贴合零件外形；包围盒就是它的外接矩形"),
            spec("margin_mm", "轮廓偏置", K.FLOAT, 0.0, minimum=-50.0, maximum=50.0,
                 step=0.5, unit="mm", group="区域",
                 help="正值外扩（留边距），负值内缩（只加工轮廓以内）"),
        )
    )

    def boundary(self) -> NDArray[np.float64]:
        if self._model is None:
            raise ParameterError("模型轮廓需要先导入模型")
        base = self._model.mesh.xy_outline(self.outline)
        return offset_convex_polygon(base, self.margin_mm)


def offset_convex_polygon(
    polygon: NDArray[np.float64], distance: float
) -> NDArray[np.float64]:
    """把凸多边形整体外扩（distance > 0）或内缩（distance < 0）。

    做法是标准的"每条边沿外法向平移后与相邻边求交"。凸多边形不会自交，
    所以内缩过度时面积会退化成 0 或反向——这里直接当作参数错误报出来。
    """

    points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    if abs(float(distance)) <= 1e-12:
        return points
    if polygon_area(points) < 0.0:
        points = points[::-1]
    count = points.shape[0]
    if count < 3:
        raise ParameterError("轮廓至少要三个点才能偏置")
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    if np.any(lengths <= 1e-12):
        raise ParameterError("轮廓存在重合点，无法偏置")
    tangents = edges / lengths[:, None]
    normals = np.column_stack((tangents[:, 1], -tangents[:, 0]))  # 逆时针轮廓的外法向

    previous = np.roll(np.arange(count), 1)
    normal_a = normals[previous]
    normal_b = normals
    tangent_a = tangents[previous]
    tangent_b = tangents
    # 解 p + n_a·d + s·t_a = p + n_b·d + r·t_b，得到偏移后的交点。
    matrix = np.stack((tangent_a, -tangent_b), axis=2)
    rhs = float(distance) * (normal_b - normal_a)
    determinant = matrix[:, 0, 0] * matrix[:, 1, 1] - matrix[:, 0, 1] * matrix[:, 1, 0]
    parallel = np.abs(determinant) <= 1e-9

    result = points + float(distance) * normals
    if np.any(~parallel):
        det = np.where(parallel, 1.0, determinant)
        s = (rhs[:, 0] * matrix[:, 1, 1] - matrix[:, 0, 1] * rhs[:, 1]) / det
        solved = points + float(distance) * normal_a + s[:, None] * tangent_a
        result = np.where(parallel[:, None], result, solved)

    if polygon_area(result) <= 1e-9:
        raise ParameterError(
            f"轮廓偏置 {distance:g} mm 之后退化了：请减小内缩量或换一种轮廓方式"
        )
    return result


def build_region(
    shape_id: str,
    raw_parameters: Mapping[str, Any] | None = None,
    *,
    model: Any = None,
) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。

    model 是 core.mesh.StoredModel：只有"模型轮廓"这类 needs_model 的形状才会用到它。
    """

    cls = REGION_SHAPES.get(shape_id)
    values = cls.parameters.coerce(raw_parameters)
    if getattr(cls, "needs_model", False):
        if model is None:
            raise ParameterError(
                f"区域形状 {shape_id!r} 需要先导入模型：请在请求里给出 model.id，"
                "或先用 POST /api/models 上传 STL"
            )
        return cls(_model=model, **values)
    return cls(**values)


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
