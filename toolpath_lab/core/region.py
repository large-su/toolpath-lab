"""加工区域。

区域就是"要加工的那块地方"，它替代了"导入模型 + 提取特征"这一整套前置环节：
直接给定一个规则区域即可开始规划。当前提供两种形状：

- 方形（square）：一个边长；
- 圆形（circle）：一个直径。

所有形状统一归约为一条**逆时针、不重复首点**的边界多边形。栅格刀路只会用到
"一条直线与多边形求交"，因此新增形状（椭圆、跑道形、凹多边形……）只要实现一个
boundary() 就能直接参与规划，不需要改任何刀路代码。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import cos, pi, sin
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
class RoundedRectangleRegion(RegionShape):
    """圆角矩形区域（新增形状）。

    实际零件上最常见的"方腔/方台"其实都带圆角，纯方形会高估可加工面积，
    也会让环切/螺旋的角落处理失真。圆角半径取到边长的一半就是跑道形，
    因此这一个形状同时覆盖了"方"、"圆角矩形"与"跑道"三种工况。
    """

    side_x_mm: float = 80.0
    side_y_mm: float = 60.0
    corner_radius_mm: float = 10.0

    id: ClassVar[str] = "rounded_rect"
    label: ClassVar[str] = "圆角矩形"
    description: ClassVar[str] = "带圆角的方腔/方台；圆角半径取到边长一半即为跑道形"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("side_x_mm", "X 向边长", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("side_y_mm", "Y 向边长", K.FLOAT, 60.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("corner_radius_mm", "圆角半径 R", K.FLOAT, 10.0, minimum=0.0,
                 maximum=500.0, step=1.0, unit="mm", group="区域",
                 help="必须小于短边的一半；等于短边一半时即为跑道形"),
        )
    )

    def __post_init__(self) -> None:
        if self.side_x_mm <= 0 or self.side_y_mm <= 0:
            raise ParameterError("圆角矩形的边长必须为正")
        limit = min(self.side_x_mm, self.side_y_mm) / 2.0
        if self.corner_radius_mm < 0 or self.corner_radius_mm > limit + 1e-9:
            raise ParameterError(
                f"圆角半径 {self.corner_radius_mm:g} mm 必须不超过短边的一半（{limit:g} mm）"
            )

    def boundary(self) -> NDArray[np.float64]:
        half_x = self.side_x_mm / 2.0
        half_y = self.side_y_mm / 2.0
        radius = self.corner_radius_mm
        if radius <= 1e-9:
            return np.array(
                [(-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)],
                dtype=np.float64,
            )
        # 每段圆弧按弧长分配段数，避免大圆角上出现折线感。
        arc_segments = max(2, int(round(CIRCLE_SEGMENTS * (radius / max(half_x, half_y)) / 4.0)))
        arc_segments = max(2, min(arc_segments, CIRCLE_SEGMENTS // 4))
        # 逆时针：右下 → 右上 → 左上 → 左下
        centers = [
            (half_x - radius, -(half_y - radius), -pi / 2.0),
            (half_x - radius, half_y - radius, 0.0),
            (-(half_x - radius), half_y - radius, pi / 2.0),
            (-(half_x - radius), -(half_y - radius), pi),
        ]
        points: list[tuple[float, float]] = []
        for cx, cy, start in centers:
            angles = np.linspace(start, start + pi / 2.0, arc_segments + 1)[:-1]
            for angle in angles:
                points.append((cx + radius * cos(angle), cy + radius * sin(angle)))
        return np.array(points, dtype=np.float64)


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
