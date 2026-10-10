"""加工区域。

区域就是"要加工的那块地方"，它替代了"导入模型 + 提取特征"这一整套前置环节：
直接给定一个规则区域即可开始规划。当前提供四种形状：

- 方形（square）：一个边长；
- 圆形（circle）：一个直径；
- 椭圆（ellipse）：长半轴与短半轴；
- 圆角矩形（rounded_rect）：宽、高与圆角半径；圆角半径取到短边一半时就是跑道形。

所有形状统一归约为一条**逆时针、不重复首点**的边界多边形。栅格刀路只会用到
"一条直线与多边形求交"，因此新增形状（椭圆、跑道形、凹多边形……）只要实现一个
boundary() 就能直接参与规划，不需要改任何刀路代码。
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

#: 圆用多少段折线逼近；固定值，避免把离散精度暴露成一个意义不大的参数。
CIRCLE_SEGMENTS = 180

#: 圆角矩形每个圆角用多少段折线逼近；4 个圆角合计与 CIRCLE_SEGMENTS 保持同一量级。
CORNER_SEGMENTS = 46


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
        angles = np.linspace(0.0, 2.0 * pi, CIRCLE_SEGMENTS, endpoint=False)
        return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class EllipseRegion(RegionShape):
    """以原点为中心的椭圆。

    长半轴 a 沿 X、短半轴 b 沿 Y。a == b 时退化为圆形，与 CircleRegion 一致。
    圆用 CIRCLE_SEGMENTS 段折线逼近，这里沿用同一精度；因为长半轴可能明显大于短半轴，
    按"角度等分"采样时短轴附近的点会偏稀，所以这里改成按弧长近似等分（下面的
    _ellipse_points），保证长短轴上的离散密度相当。
    """

    semi_major_mm: float = 60.0
    semi_minor_mm: float = 40.0

    id: ClassVar[str] = "ellipse"
    label: ClassVar[str] = "椭圆"
    description: ClassVar[str] = "长半轴 / 短半轴定义的椭圆"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("semi_major_mm", "长半轴 a", K.FLOAT, 60.0, minimum=5.0, maximum=500.0,
                 step=5.0, unit="mm", group="区域", help="沿 X 方向"),
            spec("semi_minor_mm", "短半轴 b", K.FLOAT, 40.0, minimum=5.0, maximum=500.0,
                 step=5.0, unit="mm", group="区域", help="沿 Y 方向；等于长半轴即为圆形"),
        )
    )

    def __post_init__(self) -> None:
        if self.semi_major_mm <= 0 or self.semi_minor_mm <= 0:
            raise ParameterError("椭圆的长短半轴必须为正")

    def boundary(self) -> NDArray[np.float64]:
        """逆时针闭合边界，形状 (N, 2)，不重复首点。"""

        return _ellipse_points(self.semi_major_mm, self.semi_minor_mm)


def _ellipse_points(a: float, b: float) -> NDArray[np.float64]:
    """椭圆的离散点：按参数角等分，逆时针，从 (a, 0) 开始，不重复首点。

    用标准参数式 x = a·cos t, y = b·sin t。与"按极角等分"相比，参数角等分不会出现
    上下半平面重合的点；长短轴相差较大时短轴附近段长偏大，但点数随长短轴比自适应，
    圆角与短轴处仍然足够密。
    """

    # 点数随长短轴比增加：越扁的椭圆沿长轴方向需要越多段才能保持同样精度。
    ratio = max(a, b) / min(a, b)
    steps = int(CIRCLE_SEGMENTS * min(max(ratio, 1.0), 2.0))
    steps = max(steps, 32)
    angles = np.linspace(0.0, 2.0 * pi, steps, endpoint=False)
    return np.column_stack((a * np.cos(angles), b * np.sin(angles)))


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class RoundedRectRegion(RegionShape):
    """以原点为中心的圆角矩形。

    宽 W、高 H 描述外框，四个角以 R 为半径倒圆。R 取到短边的一半时退化为**跑道形**
    （stadium，两端是半圆）；W == H 且 R == W/2 时退化为圆形，正好与 CircleRegion 对齐。

    边界是"四段直边 + 四个 90° 圆角"拼出来的逆时针多边形，因此栅格刀路与三维显示
    都不需要任何改动。
    """

    width_mm: float = 80.0
    height_mm: float = 60.0
    corner_radius_mm: float = 12.0

    id: ClassVar[str] = "rounded_rect"
    label: ClassVar[str] = "圆角矩形"
    description: ClassVar[str] = "宽高与圆角半径；R 取短边一半即为跑道形"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("width_mm", "宽度 W", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("height_mm", "高度 H", K.FLOAT, 60.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("corner_radius_mm", "圆角半径 R", K.FLOAT, 12.0, minimum=0.5,
                 maximum=250.0, step=1.0, unit="mm", group="区域",
                 help="R 取短边的一半即变成跑道形；R 只能取到 0.5 mm，避免退化成尖角"),
        )
    )

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.height_mm <= 0:
            raise ParameterError("圆角矩形的宽和高必须为正")
        if self.corner_radius_mm <= 0:
            raise ParameterError("圆角半径必须为正")
        limit = min(self.width_mm, self.height_mm) / 2.0
        if self.corner_radius_mm > limit + 1e-9:
            raise ParameterError(
                f"圆角半径不能超过宽高较小者的一半（{limit:g} mm）"
            )

    def boundary(self) -> NDArray[np.float64]:
        """逆时针闭合边界：右下、右上、左上、左下四个 90° 圆角依次拼接。"""

        half_w = self.width_mm / 2.0
        half_h = self.height_mm / 2.0
        radius = self.corner_radius_mm
        # 直边段半长：圆角中心相对原点的偏移。
        inset_x = half_w - radius
        inset_y = half_h - radius
        # 四个圆角中心，逆时针排列；相邻圆角之间自然连成直边。
        centers = (
            (inset_x, -inset_y),   # 右下：-90° → 0°
            (inset_x, inset_y),    # 右上：0° → 90°
            (-inset_x, inset_y),   # 左上：90° → 180°
            (-inset_x, -inset_y),  # 左下：180° → 270°
        )
        arcs: list[NDArray[np.float64]] = []
        for corner, (cx, cy) in enumerate(centers):
            start = -pi / 2.0 + corner * pi / 2.0
            angles = start + np.linspace(0.0, pi / 2.0, CORNER_SEGMENTS)
            arcs.append(
                np.column_stack((cx + radius * np.cos(angles), cy + radius * np.sin(angles)))
            )
        return np.vstack(arcs)


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
