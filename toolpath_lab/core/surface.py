"""加工曲面。

项目最初把加工面写死成 XY 平面。这里把它推广成一个**高度场**：曲面就是一条
z = f(x, y) 的函数，而区域轮廓仍然定义在 XY 平面上。这样做有三条理由：

1. 刀路只需要"把平面点抬到曲面上"这一步，栅格、环切、螺旋等策略都能直接复用；
2. 三维显示只需要把 f 采样成网格，不需要真正的实体建模；
3. 切宽、残留高度这些工艺量只取决于刀尖形状与曲面高度，与曲面是怎么来的无关。

当前提供四种形状：平面（flat）、斜面（incline）、圆柱面（cylinder）、球冠（dome）。
新增一种曲面 = 写一个 height()，按需要覆盖 covers()，然后注册到 SURFACES。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite, radians, tan
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.mathutil import direction_2d
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.region import points_in_polygon, polygon_bounds
from toolpath_lab.core.registry import Registry

SURFACES: Registry[type["Surface"]] = Registry("surface")

#: 三维显示的采样网格分辨率；只影响载荷大小与观感，不影响刀路精度。
SURFACE_MESH_RESOLUTION = 64


def _broadcast(x: Any, y: Any) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """把标量或数组形式的 x / y 广播成同形状的 float64 数组。"""

    pair = np.broadcast_arrays(
        np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    )
    return np.array(pair[0], dtype=np.float64), np.array(pair[1], dtype=np.float64)


@dataclass(frozen=True, slots=True)
class Surface:
    """所有加工曲面的基类。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: 平面类曲面一条刀线只要两个端点，不需要按步长离散。
    is_planar: ClassVar[bool] = False
    #: 恰好就是 XY 平面（z = 0）：三维视图用工件实体本身表示，不需要额外网格。
    is_xy_plane: ClassVar[bool] = False
    parameters: ClassVar[ParameterSet] = ParameterSet()

    def height(self, x: Any, y: Any) -> NDArray[np.float64]:
        """曲面高度 z = f(x, y)，接受标量或数组。"""

        raise NotImplementedError

    @property
    def domain_radius_mm(self) -> float:
        """曲面有定义的半径；平面类曲面处处有定义。"""

        return float("inf")

    def covers(self, x: Any, y: Any) -> NDArray[np.bool_]:
        """逐点判断是否落在曲面定义域内。"""

        grid_x, _ = _broadcast(x, y)
        return np.ones(grid_x.shape, dtype=bool)

    def ensure_covers(self, boundary: NDArray[np.float64]) -> None:
        """区域轮廓超出曲面定义域时，抛出可读的几何错误。"""

        polygon = np.asarray(boundary, dtype=np.float64).reshape(-1, 2)
        if not bool(np.all(self.covers(polygon[:, 0], polygon[:, 1]))):
            raise PlanningError(
                f"{self.label}的定义域半径是 {self.domain_radius_mm:g} mm，"
                "盖不住整个区域；请加大曲面半径或者缩小区域"
            )

    def sample_grid(
        self, boundary: NDArray[np.float64], resolution: int = SURFACE_MESH_RESOLUTION
    ) -> dict[str, Any]:
        """在区域包围盒上采样成网格，供三维视图画出被加工面。"""

        polygon = np.asarray(boundary, dtype=np.float64).reshape(-1, 2)
        (x_min, x_max), (y_min, y_max) = polygon_bounds(polygon)
        xs = np.linspace(x_min, x_max, resolution)
        ys = np.linspace(y_min, y_max, resolution)
        grid_x, grid_y = np.meshgrid(xs, ys)
        heights = np.asarray(self.height(grid_x, grid_y), dtype=np.float64)
        inside = points_in_polygon(
            polygon, np.column_stack((grid_x.ravel(), grid_y.ravel()))
        ).reshape(grid_x.shape)
        return {
            "resolution": int(resolution),
            "x": [round(float(value), 4) for value in xs],
            "y": [round(float(value), 4) for value in ys],
            "z": [[round(float(value), 4) for value in row] for row in heights],
            "inside": [[bool(value) for value in row] for row in inside],
        }

    def to_params(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "is_planar": bool(self.is_planar),
            "parameters": self.to_params(),
        }


@SURFACES.register
@dataclass(frozen=True, slots=True)
class FlatSurface(Surface):
    """水平加工面，也就是项目最初的行为。"""

    id: ClassVar[str] = "flat"
    is_xy_plane: ClassVar[bool] = True
    label: ClassVar[str] = "平面"
    description: ClassVar[str] = "水平加工面：一条刀线只要两个端点，兼容性最好的默认值"
    is_planar: ClassVar[bool] = True

    def height(self, x: Any, y: Any) -> NDArray[np.float64]:
        grid_x, _ = _broadcast(x, y)
        return np.zeros(grid_x.shape, dtype=np.float64)


@SURFACES.register
@dataclass(frozen=True, slots=True)
class InclineSurface(Surface):
    """过原点的等斜度单斜面。"""

    angle_deg: float = 8.0
    direction_deg: float = 0.0

    id: ClassVar[str] = "incline"
    label: ClassVar[str] = "斜面"
    description: ClassVar[str] = "沿给定方向倾斜的平面（过原点），一条刀线仍然只要两个端点"
    is_planar: ClassVar[bool] = True
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("angle_deg", "倾角", K.FLOAT, 8.0, minimum=0.1, maximum=60.0,
                 step=0.5, unit="°", group="曲面"),
            spec("direction_deg", "倾斜方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="曲面", help="升高的方向，从 +X 轴起算"),
        )
    )

    def __post_init__(self) -> None:
        if not isfinite(self.angle_deg) or not 0.0 < self.angle_deg < 90.0:
            raise ParameterError("斜面倾角必须在 0° 到 90° 之间")

    def height(self, x: Any, y: Any) -> NDArray[np.float64]:
        grid_x, grid_y = _broadcast(x, y)
        axis = direction_2d(float(self.direction_deg))
        along = grid_x * axis[0] + grid_y * axis[1]
        return tan(radians(float(self.angle_deg))) * along


@SURFACES.register
@dataclass(frozen=True, slots=True)
class CylinderSurface(Surface):
    """轴线平行于 Y 轴的凸圆弧面。"""

    radius_mm: float = 120.0
    crown_mm: float = 12.0

    id: ClassVar[str] = "cylinder"
    label: ClassVar[str] = "圆柱面"
    description: ClassVar[str] = "沿 Y 轴展开的凸圆弧面，用来看平面刀路在曲面上的跟随"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("radius_mm", "圆弧半径 R", K.FLOAT, 120.0, minimum=10.0,
                 maximum=2000.0, step=10.0, unit="mm", group="曲面"),
            spec("crown_mm", "冠顶高度", K.FLOAT, 12.0, minimum=0.5, maximum=200.0,
                 step=0.5, unit="mm", group="曲面",
                 help="曲面最高点的 Z；圆弧中心在冠顶下方 R 处，因此必须小于半径"),
        )
    )

    def __post_init__(self) -> None:
        _check_curved(self.radius_mm, self.crown_mm)

    @property
    def domain_radius_mm(self) -> float:
        return float(self.radius_mm)

    def covers(self, x: Any, y: Any) -> NDArray[np.bool_]:
        grid_x, _ = _broadcast(x, y)
        return np.abs(grid_x) <= float(self.radius_mm) + 1e-9

    def height(self, x: Any, y: Any) -> NDArray[np.float64]:
        grid_x, _ = _broadcast(x, y)
        radius = float(self.radius_mm)
        inside = np.clip(radius * radius - grid_x * grid_x, 0.0, None)
        return np.sqrt(inside) - (radius - float(self.crown_mm))


@SURFACES.register
@dataclass(frozen=True, slots=True)
class DomeSurface(Surface):
    """球冠：球面被水平面截出来的一块。"""

    radius_mm: float = 120.0
    crown_mm: float = 12.0

    id: ClassVar[str] = "dome"
    label: ClassVar[str] = "球冠面"
    description: ClassVar[str] = "球面的一段（凸球冠），球头刀三维精加工的典型对象"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("radius_mm", "球面半径 R", K.FLOAT, 120.0, minimum=10.0,
                 maximum=2000.0, step=10.0, unit="mm", group="曲面"),
            spec("crown_mm", "冠顶高度", K.FLOAT, 12.0, minimum=0.5, maximum=200.0,
                 step=0.5, unit="mm", group="曲面",
                 help="曲面最高点的 Z；球心在冠顶下方 R 处，因此必须小于半径"),
        )
    )

    def __post_init__(self) -> None:
        _check_curved(self.radius_mm, self.crown_mm)

    @property
    def domain_radius_mm(self) -> float:
        return float(self.radius_mm)

    def covers(self, x: Any, y: Any) -> NDArray[np.bool_]:
        grid_x, grid_y = _broadcast(x, y)
        return np.hypot(grid_x, grid_y) <= float(self.radius_mm) + 1e-9

    def height(self, x: Any, y: Any) -> NDArray[np.float64]:
        grid_x, grid_y = _broadcast(x, y)
        radius = float(self.radius_mm)
        inside = np.clip(radius * radius - grid_x * grid_x - grid_y * grid_y, 0.0, None)
        return np.sqrt(inside) - (radius - float(self.crown_mm))


def _check_curved(radius_mm: float, crown_mm: float) -> None:
    """凸圆弧类曲面的共同校验：冠高必须小于半径。"""

    if not isfinite(radius_mm) or radius_mm <= 0.0:
        raise ParameterError("曲面半径必须是有限正数")
    if not isfinite(crown_mm) or crown_mm <= 0.0:
        raise ParameterError("曲面冠高必须是有限正数")
    if crown_mm >= radius_mm:
        raise ParameterError(f"冠高 {crown_mm:g} mm 必须小于曲面半径 {radius_mm:g} mm")


def build_surface(surface_id: str, raw_parameters: Mapping[str, Any] | None = None) -> Surface:
    """由接口参数构造一个已注册的加工曲面。"""

    cls = SURFACES.get(surface_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def surface_catalog() -> list[dict[str, Any]]:
    return SURFACES.catalog()
