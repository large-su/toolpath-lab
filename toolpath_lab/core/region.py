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
    """以原点为中心的圆角矩形区域（四角圆弧过渡）。"""

    width_mm: float = 80.0
    height_mm: float = 60.0
    corner_radius_mm: float = 10.0

    id: ClassVar[str] = "rounded_rectangle"
    label: ClassVar[str] = "圆角矩形"
    description: ClassVar[str] = "带圆角过渡的矩形型腔，数控加工最常见的型腔形状之一"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("width_mm", "宽度 W", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("height_mm", "高度 H", K.FLOAT, 60.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("corner_radius_mm", "圆角半径 Rr", K.FLOAT, 10.0, minimum=0.0,
                 maximum=1000.0, step=1.0, unit="mm", group="区域",
                 help="四角的过渡圆弧半径，不能超过短边的一半；为 0 时退化为方形"),
        )
    )

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.height_mm <= 0:
            raise ParameterError("圆角矩形宽高必须为正")
        if self.corner_radius_mm < 0:
            raise ParameterError("圆角半径必须非负")
        if 2.0 * self.corner_radius_mm > min(self.width_mm, self.height_mm) + 1e-9:
            raise ParameterError("圆角半径不能超过短边的一半")

    def boundary(self) -> NDArray[np.float64]:
        w = self.width_mm / 2.0
        h = self.height_mm / 2.0
        r = self.corner_radius_mm
        if r <= 1e-9:
            return np.array([(-w, -h), (w, -h), (w, h), (-w, h)], dtype=np.float64)

        # 四个 1/4 圆弧：圆心 + 角度范围（逆时针，从右侧直边转上去）
        arcs = (
            ((w - r, h - r), 0.0, 0.5 * pi),
            ((-w + r, h - r), 0.5 * pi, pi),
            ((-w + r, -h + r), pi, 1.5 * pi),
            ((w - r, -h + r), 1.5 * pi, 2.0 * pi),
        )
        segments = 16
        xs: list[float] = []
        ys: list[float] = []
        for (cx, cy), a0, a1 in arcs:
            angles = np.linspace(a0, a1, segments + 1, endpoint=True)
            arc_x = (cx + r * np.cos(angles)).tolist()
            arc_y = (cy + r * np.sin(angles)).tolist()
            if not xs:
                xs.append(arc_x[0])
                ys.append(arc_y[0])
            else:
                xs.append(arc_x[0])  # 直线段：上一弧终点 → 本弧起点
                ys.append(arc_y[0])
            xs.extend(arc_x[1:])
            ys.extend(arc_y[1:])
        points = np.column_stack((np.asarray(xs, dtype=np.float64),
                                  np.asarray(ys, dtype=np.float64)))
        # 末弧终点回到首点，去掉避免首尾重复
        if np.linalg.norm(points[-1] - points[0]) <= 1e-9:
            points = points[:-1]
        return points


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
