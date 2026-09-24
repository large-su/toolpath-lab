"""加工曲面。

曲面与区域分开建模：区域决定 XY 平面内的加工边界，曲面决定每个 XY
位置对应的 Z 高度。这样现有扫描线规划可以继续复用区域裁剪，同时让
刀路点和三维工件跟随曲面起伏。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import cos, pi, sin
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.registry import Registry
from toolpath_lab.core.region import RegionShape

SURFACE_TYPES: Registry[type["SurfaceShape"]] = Registry("surface")


@dataclass(frozen=True, slots=True)
class SurfaceShape:
    """所有加工曲面的共同接口。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """返回与 XY 点一一对应的 Z 高度。"""

        raise NotImplementedError

    def height_bounds(self) -> tuple[float, float]:
        """返回曲面可能达到的最低和最高高度。"""

        raise NotImplementedError

    def to_params(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def describe(self, region: RegionShape | None = None) -> dict[str, Any]:
        lower, upper = self.height_bounds()
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "parameters": self.to_params(),
            "height_bounds_mm": [lower, upper],
        }
        if region is not None and self.id == "freeform":
            payload["mesh"] = self.mesh_payload(region)
        return payload

    def mesh_payload(self, region: RegionShape, resolution: int = 36) -> dict[str, Any]:
        """将曲面采样成轻量三角网格，供 three.js 显示。"""

        boundary = region.boundary()
        x_min, x_max = float(boundary[:, 0].min()), float(boundary[:, 0].max())
        y_min, y_max = float(boundary[:, 1].min()), float(boundary[:, 1].max())
        xs = np.linspace(x_min, x_max, resolution + 1)
        ys = np.linspace(y_min, y_max, resolution + 1)
        grid = np.array([[x, y] for y in ys for x in xs], dtype=np.float64)
        heights = self.height_at(grid)
        vertices = np.column_stack((grid, heights))

        inside = np.array([_point_in_polygon(point, boundary) for point in grid])
        indices: list[int] = []
        row_width = resolution + 1
        for row in range(resolution):
            for col in range(resolution):
                a = row * row_width + col
                b = a + 1
                c = a + row_width
                d = c + 1
                if inside[a] and inside[b] and inside[c]:
                    indices.extend((a, b, c))
                if inside[b] and inside[c] and inside[d]:
                    indices.extend((b, d, c))

        return {
            "vertices": [[round(float(value), 4) for value in row] for row in vertices],
            "indices": indices,
            "resolution": resolution,
        }


@SURFACE_TYPES.register
@dataclass(frozen=True, slots=True)
class FlatSurface(SurfaceShape):
    """水平加工面。"""

    base_z_mm: float = 0.0

    id: ClassVar[str] = "flat"
    label: ClassVar[str] = "平面"
    description: ClassVar[str] = "恒定 Z 高度的平面加工面"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("base_z_mm", "基准高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面"),
        )
    )

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.full(points.shape[0], self.base_z_mm, dtype=np.float64)

    def height_bounds(self) -> tuple[float, float]:
        return self.base_z_mm, self.base_z_mm


@SURFACE_TYPES.register
@dataclass(frozen=True, slots=True)
class FreeformSurface(SurfaceShape):
    """可调幅值和波长的解析自由曲面高度场。

    该曲面用于验证曲面刀路链路，表达式为：
    ``z = base + A sin(2πx/Lx + phase) cos(2πy/Ly)``。
    """

    base_z_mm: float = 0.0
    amplitude_mm: float = 4.0
    wavelength_x_mm: float = 80.0
    wavelength_y_mm: float = 60.0
    phase_deg: float = 0.0

    id: ClassVar[str] = "freeform"
    label: ClassVar[str] = "自由曲面"
    description: ClassVar[str] = "连续起伏的解析高度场，用于曲面刀路规划验证"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("base_z_mm", "基准高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("amplitude_mm", "起伏幅值", K.FLOAT, 4.0, minimum=0.0, maximum=30.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("wavelength_x_mm", "X 波长", K.FLOAT, 80.0, minimum=10.0, maximum=1000.0,
                 step=5.0, unit="mm", group="曲面"),
            spec("wavelength_y_mm", "Y 波长", K.FLOAT, 60.0, minimum=10.0, maximum=1000.0,
                 step=5.0, unit="mm", group="曲面"),
            spec("phase_deg", "相位", K.FLOAT, 0.0, minimum=0.0, maximum=360.0,
                 step=5.0, unit="°", group="曲面"),
        )
    )

    def __post_init__(self) -> None:
        if self.amplitude_mm < 0:
            raise ParameterError("自由曲面起伏幅值不能为负")
        if self.wavelength_x_mm <= 0 or self.wavelength_y_mm <= 0:
            raise ParameterError("自由曲面波长必须为正")

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        phase = np.deg2rad(self.phase_deg)
        x_term = 2.0 * pi * points[:, 0] / self.wavelength_x_mm + phase
        y_term = 2.0 * pi * points[:, 1] / self.wavelength_y_mm
        return self.base_z_mm + self.amplitude_mm * np.sin(x_term) * np.cos(y_term)

    def height_bounds(self) -> tuple[float, float]:
        return self.base_z_mm - self.amplitude_mm, self.base_z_mm + self.amplitude_mm


def _point_in_polygon(point: NDArray[np.float64], polygon: NDArray[np.float64]) -> bool:
    """射线法判断网格点是否在区域内。"""

    x, y = float(point[0]), float(point[1])
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        crosses = (y1 > y) != (y2 > y)
        if crosses and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def build_surface(surface_id: str, raw_parameters: Mapping[str, Any] | None = None) -> SurfaceShape:
    cls = SURFACE_TYPES.get(surface_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def surface_catalog() -> list[dict[str, Any]]:
    return SURFACE_TYPES.catalog()
