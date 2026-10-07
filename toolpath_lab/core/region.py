"""加工区域。

区域就是"要加工的那块地方"，它替代了"导入模型 + 提取特征"这一整套前置环节：
直接给定一个规则区域即可开始规划。当前提供四种形状：

- 方形（square）：一个边长；
- 矩形（rectangle）：宽 × 高；
- 圆形（circle）：一个直径；
- 椭圆（ellipse）：长半轴 / 短半轴；
- U 形（u_shape）：外宽 / 外高 / 壁厚，一个凹多边形；
- 哑铃形（dumbbell）：两端方头 + 细颈，细颈被偏置吃掉后同一层会分裂成两条环；
- 三角形（triangle）：底边与高，顶角可以调得很尖。

所有形状统一归约为一条**逆时针、不重复首点**的边界多边形。栅格刀路只会用到
"一条直线与多边形求交"，三维工件也直接按这条边界挤出，因此新增形状（跑道形、带缺口的多边形……）
只要实现一个 boundary() 就能直接参与规划与显示，不需要改任何刀路或前端代码；
凹形状（U 形）一条扫描线会得到多段，于是同一行里出现多条独立刀轨。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import pi
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.registry import Registry

REGION_SHAPES: Registry[type["RegionShape"]] = Registry("region shape")

#: 曲线边界（圆、椭圆）用多少段折线逼近；固定值，避免把离散精度暴露成一个意义不大的参数。
CURVE_SEGMENTS = 180


@dataclass(frozen=True, slots=True)
class RegionShape:
    """所有区域形状的基类。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()

    def boundary(self) -> NDArray[np.float64]:
        """逆时针闭合边界，形状 (N, 2)，不重复首点。"""

        raise NotImplementedError

    def to_params(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

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
        angles = np.linspace(0.0, 2.0 * pi, CURVE_SEGMENTS, endpoint=False)
        return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class RectangleRegion(RegionShape):
    """以原点为中心的矩形区域（宽沿 X 轴，高沿 Y 轴）。"""

    width_mm: float = 100.0
    height_mm: float = 60.0

    id: ClassVar[str] = "rectangle"
    label: ClassVar[str] = "矩形"
    description: ClassVar[str] = "宽高不等的长方形，用来看刀路在两个方向上的收放"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("width_mm", "宽 W", K.FLOAT, 100.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="沿 X 轴的尺寸"),
            spec("height_mm", "高 H", K.FLOAT, 60.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="沿 Y 轴的尺寸"),
        )
    )

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.height_mm <= 0:
            raise ParameterError("矩形的宽和高都必须为正")

    def boundary(self) -> NDArray[np.float64]:
        half_width = self.width_mm / 2.0
        half_height = self.height_mm / 2.0
        return np.array(
            [
                (-half_width, -half_height),
                (half_width, -half_height),
                (half_width, half_height),
                (-half_width, half_height),
            ],
            dtype=np.float64,
        )


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class EllipseRegion(RegionShape):
    """以原点为中心的椭圆区域（长半轴沿 X 轴，短半轴沿 Y 轴）。"""

    semi_major_mm: float = 60.0
    semi_minor_mm: float = 40.0

    id: ClassVar[str] = "ellipse"
    label: ClassVar[str] = "椭圆"
    description: ClassVar[str] = "长半轴 / 短半轴定义的椭圆，刀线是一族弦长各不相同的弦"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("semi_major_mm", "长半轴 a", K.FLOAT, 60.0, minimum=1.0, maximum=500.0,
                 step=1.0, unit="mm", group="区域", help="沿 X 轴"),
            spec("semi_minor_mm", "短半轴 b", K.FLOAT, 40.0, minimum=1.0, maximum=500.0,
                 step=1.0, unit="mm", group="区域", help="沿 Y 轴；与长半轴互换只是把椭圆转 90°"),
        )
    )

    def __post_init__(self) -> None:
        if self.semi_major_mm <= 0 or self.semi_minor_mm <= 0:
            raise ParameterError("椭圆的两个半轴都必须为正")

    def boundary(self) -> NDArray[np.float64]:
        angles = np.linspace(0.0, 2.0 * pi, CURVE_SEGMENTS, endpoint=False)
        return np.column_stack(
            (self.semi_major_mm * np.cos(angles), self.semi_minor_mm * np.sin(angles))
        )


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class UShapeRegion(RegionShape):
    """以原点为中心、开口朝上的 U 形区域（凹多边形）。"""

    width_mm: float = 100.0
    height_mm: float = 80.0
    wall_mm: float = 25.0

    id: ClassVar[str] = "u_shape"
    label: ClassVar[str] = "U 形"
    description: ClassVar[str] = "开口槽：一条扫描线会在两条臂上切出两段刀轨，用来验证凹形状"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("width_mm", "外宽 W", K.FLOAT, 100.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="沿 X 轴的总宽"),
            spec("height_mm", "外高 H", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="沿 Y 轴的总高"),
            spec("wall_mm", "壁厚 t", K.FLOAT, 25.0, minimum=1.0, maximum=500.0,
                 step=1.0, unit="mm", group="区域",
                 help="两侧臂与底部的厚度；必须小于外宽的一半，槽口才不会被填满"),
        )
    )

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.height_mm <= 0 or self.wall_mm <= 0:
            raise ParameterError("U 形的外宽、外高与壁厚都必须为正")
        if self.wall_mm >= self.width_mm / 2.0:
            raise ParameterError(
                f"壁厚 {self.wall_mm:g} mm 不能大于等于外宽的一半 "
                f"（{self.width_mm / 2.0:g} mm），否则两条臂会贴在一起"
            )
        if self.wall_mm >= self.height_mm:
            raise ParameterError(f"壁厚 {self.wall_mm:g} mm 必须小于外高 {self.height_mm:g} mm")

    def boundary(self) -> NDArray[np.float64]:
        half_width = self.width_mm / 2.0
        half_height = self.height_mm / 2.0
        inner_x = half_width - self.wall_mm
        inner_y = -half_height + self.wall_mm
        return np.array(
            [
                (-half_width, -half_height),
                (half_width, -half_height),
                (half_width, half_height),
                (inner_x, half_height),
                (inner_x, inner_y),
                (-inner_x, inner_y),
                (-inner_x, half_height),
                (-half_width, half_height),
            ],
            dtype=np.float64,
        )


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class DumbbellRegion(RegionShape):
    """以原点为中心的哑铃形：两端方块用一根细颈连起来。"""

    width_mm: float = 160.0
    pad_mm: float = 60.0
    neck_mm: float = 20.0

    id: ClassVar[str] = "dumbbell"
    label: ClassVar[str] = "哑铃形"
    description: ClassVar[str] = "两端方块 + 细颈：偏置到细颈被吃掉时，一层会分裂成两条环"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("width_mm", "总长 W", K.FLOAT, 160.0, minimum=20.0, maximum=1000.0,
                 step=10.0, unit="mm", group="区域", help="沿 X 轴的总长"),
            spec("pad_mm", "方头边长 p", K.FLOAT, 60.0, minimum=5.0, maximum=500.0,
                 step=5.0, unit="mm", group="区域", help="两端方块的边长（也是总高）"),
            spec("neck_mm", "细颈宽 n", K.FLOAT, 20.0, minimum=1.0, maximum=500.0,
                 step=1.0, unit="mm", group="区域",
                 help="中间细颈的宽度；必须小于方头边长，偏置超过它的一半时细颈消失"),
        )
    )

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.pad_mm <= 0 or self.neck_mm <= 0:
            raise ParameterError("哑铃形的总长、方头边长与细颈宽都必须为正")
        if self.neck_mm >= self.pad_mm:
            raise ParameterError(
                f"细颈宽 {self.neck_mm:g} mm 必须小于方头边长 {self.pad_mm:g} mm"
            )
        if 2.0 * self.pad_mm >= self.width_mm:
            raise ParameterError(
                f"两端方头一共 {2.0 * self.pad_mm:g} mm，必须小于总长 {self.width_mm:g} mm，"
                "否则中间没有细颈"
            )

    def boundary(self) -> NDArray[np.float64]:
        half_width = self.width_mm / 2.0
        half_pad = self.pad_mm / 2.0
        half_neck = self.neck_mm / 2.0
        inner = half_width - self.pad_mm
        return np.array(
            [
                (-half_width, -half_pad),
                (-inner, -half_pad),
                (-inner, -half_neck),
                (inner, -half_neck),
                (inner, -half_pad),
                (half_width, -half_pad),
                (half_width, half_pad),
                (inner, half_pad),
                (inner, half_neck),
                (-inner, half_neck),
                (-inner, half_pad),
                (-half_width, half_pad),
            ],
            dtype=np.float64,
        )


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class TriangleRegion(RegionShape):
    """以原点为中心的等腰三角形（底边在下、顶点朝上）。"""

    width_mm: float = 80.0
    height_mm: float = 60.0

    id: ClassVar[str] = "triangle"
    label: ClassVar[str] = "三角形"
    description: ClassVar[str] = "等腰三角形；顶角可以调得很尖，用来验证偏置在尖角处也能闭合"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("width_mm", "底边 W", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="沿 X 轴的底边长度"),
            spec("height_mm", "高 H", K.FLOAT, 60.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域",
                 help="沿 Y 轴的高度；底边越窄、高越大，顶角越尖"),
        )
    )

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.height_mm <= 0:
            raise ParameterError("三角形的底边与高都必须为正")

    def boundary(self) -> NDArray[np.float64]:
        half_width = self.width_mm / 2.0
        half_height = self.height_mm / 2.0
        return np.array(
            [
                (-half_width, -half_height),
                (half_width, -half_height),
                (0.0, half_height),
            ],
            dtype=np.float64,
        )


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
