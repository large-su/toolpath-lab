"""加工区域。

区域就是"要加工的那块地方"，它替代了"导入模型 + 提取特征"这一整套前置环节：
直接给定一个规则区域即可开始规划。当前提供三种形状：

- 方形（square）：一个边长；
- 圆形（circle）：一个直径。
- 椭圆（ellipse）：长轴、短轴和旋转角度。

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


@dataclass(frozen=True, slots=True)
class PolygonRegion(RegionShape):
    """由导入模型的 XY 投影轮廓构成的加工区域。

    该类型不注册到常规参数目录中，而是由模型导入流程按请求动态创建。
    这样原有的方形、圆形和椭圆控件仍保持简洁，同时导入模型可以携带任意
    简单多边形边界进入同一套扫描线规划器。
    """

    points: tuple[tuple[float, float], ...] = ()

    id: ClassVar[str] = "polygon"
    label: ClassVar[str] = "导入模型投影区域"
    description: ClassVar[str] = "由导入模型的 XY 投影轮廓生成的加工区域"

    def __post_init__(self) -> None:
        raw = np.asarray(self.points, dtype=np.float64)
        if raw.ndim != 2 or raw.shape[1] != 2 or raw.shape[0] < 3:
            raise ParameterError("导入模型区域至少需要 3 个二维边界点")
        if not np.isfinite(raw).all():
            raise ParameterError("导入模型区域不能包含非有限坐标")
        cleaned: list[tuple[float, float]] = []
        for point in raw:
            value = (float(point[0]), float(point[1]))
            if not cleaned or value != cleaned[-1]:
                cleaned.append(value)
        if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
            cleaned.pop()
        if len(cleaned) < 3:
            raise ParameterError("导入模型区域的有效边界点不足")
        polygon = np.asarray(cleaned, dtype=np.float64)
        if abs(polygon_area(polygon)) <= 1e-9:
            raise ParameterError("导入模型区域面积必须大于 0")
        if polygon_area(polygon) < 0:
            cleaned.reverse()
        object.__setattr__(self, "points", tuple(cleaned))

    def boundary(self) -> NDArray[np.float64]:
        return np.asarray(self.points, dtype=np.float64).copy()

    def to_params(self) -> dict[str, Any]:
        return {"boundary": [list(point) for point in self.points]}


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
    """以原点为中心、可旋转的椭圆区域。"""

    major_mm: float = 120.0
    minor_mm: float = 80.0
    rotation_deg: float = 0.0

    id: ClassVar[str] = "ellipse"
    label: ClassVar[str] = "椭圆"
    description: ClassVar[str] = "可旋转的椭圆端面，用来观察刀路在非圆曲线边界上的收放"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("major_mm", "长轴", K.FLOAT, 120.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("minor_mm", "短轴", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("rotation_deg", "旋转角度", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="区域"),
        )
    )

    def __post_init__(self) -> None:
        if self.major_mm <= 0 or self.minor_mm <= 0:
            raise ParameterError("椭圆长轴和短轴必须为正")

    def boundary(self) -> NDArray[np.float64]:
        angles = np.linspace(0.0, 2.0 * pi, CIRCLE_SEGMENTS, endpoint=False)
        local = np.column_stack(
            ((self.major_mm / 2.0) * np.cos(angles),
             (self.minor_mm / 2.0) * np.sin(angles))
        )
        radians = np.deg2rad(self.rotation_deg)
        rotation = np.array(
            [[np.cos(radians), -np.sin(radians)],
             [np.sin(radians), np.cos(radians)]],
            dtype=np.float64,
        )
        return local @ rotation.T


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def build_polygon_region(raw_boundary: Any) -> PolygonRegion:
    """从模型导入请求中的二维边界创建动态区域。"""

    return PolygonRegion(tuple(tuple(point) for point in raw_boundary))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
