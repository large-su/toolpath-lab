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

    def normal_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """返回曲面在 XY 点处的单位法向，方向朝向 +Z。"""

        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        step = 1e-3
        x_plus = points + np.array([step, 0.0])
        x_minus = points - np.array([step, 0.0])
        y_plus = points + np.array([0.0, step])
        y_minus = points - np.array([0.0, step])
        dzdx = (self.height_at(x_plus) - self.height_at(x_minus)) / (2.0 * step)
        dzdy = (self.height_at(y_plus) - self.height_at(y_minus)) / (2.0 * step)
        normals = np.column_stack((-dzdx, -dzdy, np.ones(points.shape[0], dtype=np.float64)))
        lengths = np.linalg.norm(normals, axis=1)
        return normals / np.maximum(lengths[:, None], 1e-12)

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
        if region is not None and self.id != "flat":
            payload["mesh"] = self.mesh_payload(region)
        return payload

    @property
    def sampling_spacing_mm(self) -> float:
        """曲面刀路沿扫描线采样的建议间距。"""

        return 4.0

    def mesh_payload(self, region: RegionShape, resolution: int = 48) -> dict[str, Any]:
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
    rotation_deg: float = 0.0

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
            spec("rotation_deg", "纹理旋转", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="曲面",
                 help="旋转正弦纹理方向，便于让刀路沿不同纹理方向加工"),
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
        x, y = _rotate_xy(points, self.rotation_deg)
        x_term = 2.0 * pi * x / self.wavelength_x_mm + phase
        y_term = 2.0 * pi * y / self.wavelength_y_mm
        return self.base_z_mm + self.amplitude_mm * np.sin(x_term) * np.cos(y_term)

    def height_bounds(self) -> tuple[float, float]:
        return self.base_z_mm - self.amplitude_mm, self.base_z_mm + self.amplitude_mm

    @property
    def sampling_spacing_mm(self) -> float:
        return max(min(self.wavelength_x_mm, self.wavelength_y_mm) / 24.0, 0.25)


@SURFACE_TYPES.register
@dataclass(frozen=True, slots=True)
class SaddleSurface(SurfaceShape):
    """具有正负曲率变化的鞍形曲面。"""

    base_z_mm: float = 0.0
    amplitude_mm: float = 8.0
    wavelength_x_mm: float = 80.0
    wavelength_y_mm: float = 60.0
    rotation_deg: float = 0.0

    id: ClassVar[str] = "saddle"
    label: ClassVar[str] = "鞍形曲面"
    description: ClassVar[str] = "中心附近同时包含凸起和凹陷的双向曲率曲面"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("base_z_mm", "基准高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("amplitude_mm", "鞍形幅值", K.FLOAT, 8.0, minimum=0.0, maximum=30.0,
                 step=0.5, unit="mm", group="曲面",
                 help="控制鞍形起伏的最大高度"),
            spec("wavelength_x_mm", "X 尺度", K.FLOAT, 80.0, minimum=10.0, maximum=1000.0,
                 step=5.0, unit="mm", group="曲面"),
            spec("wavelength_y_mm", "Y 尺度", K.FLOAT, 60.0, minimum=10.0, maximum=1000.0,
                 step=5.0, unit="mm", group="曲面"),
            spec("rotation_deg", "曲面旋转", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="曲面"),
        )
    )

    def __post_init__(self) -> None:
        if self.amplitude_mm < 0:
            raise ParameterError("鞍形曲面幅值不能为负")
        _require_wavelengths(self.wavelength_x_mm, self.wavelength_y_mm, "鞍形曲面")

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        x, y = _rotate_xy(points, self.rotation_deg)
        x_term = 2.0 * pi * x / self.wavelength_x_mm
        y_term = 2.0 * pi * y / self.wavelength_y_mm
        # sin(x)sin(y) 在原点附近近似 x*y，具有典型鞍形的正负曲率。
        return self.base_z_mm + self.amplitude_mm * np.sin(x_term) * np.sin(y_term)

    def height_bounds(self) -> tuple[float, float]:
        return self.base_z_mm - self.amplitude_mm, self.base_z_mm + self.amplitude_mm

    @property
    def sampling_spacing_mm(self) -> float:
        return max(min(self.wavelength_x_mm, self.wavelength_y_mm) / 28.0, 0.25)


@SURFACE_TYPES.register
@dataclass(frozen=True, slots=True)
class DomeSurface(SurfaceShape):
    """椭圆形凸台或凹坑，适合观察中心到边缘的连续曲率。"""

    base_z_mm: float = 0.0
    center_height_mm: float = 10.0
    radius_x_mm: float = 45.0
    radius_y_mm: float = 35.0
    rotation_deg: float = 0.0

    id: ClassVar[str] = "dome"
    label: ClassVar[str] = "球冠 / 凹坑"
    description: ClassVar[str] = "椭圆凸台或凹坑，中心到边缘平滑过渡"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("base_z_mm", "基准高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("center_height_mm", "中心高度", K.FLOAT, 10.0, minimum=-30.0, maximum=30.0,
                 step=0.5, unit="mm", group="曲面",
                 help="正值为凸台，负值为凹坑"),
            spec("radius_x_mm", "X 半径", K.FLOAT, 45.0, minimum=5.0, maximum=500.0,
                 step=1.0, unit="mm", group="曲面"),
            spec("radius_y_mm", "Y 半径", K.FLOAT, 35.0, minimum=5.0, maximum=500.0,
                 step=1.0, unit="mm", group="曲面"),
            spec("rotation_deg", "曲面旋转", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="曲面"),
        )
    )

    def __post_init__(self) -> None:
        if self.radius_x_mm <= 0 or self.radius_y_mm <= 0:
            raise ParameterError("球冠/凹坑的 X、Y 半径必须为正")

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        x, y = _rotate_xy(points, self.rotation_deg)
        radial = (x / self.radius_x_mm) ** 2 + (y / self.radius_y_mm) ** 2
        profile = np.maximum(0.0, 1.0 - radial)
        return self.base_z_mm + self.center_height_mm * profile

    def height_bounds(self) -> tuple[float, float]:
        return (
            min(self.base_z_mm, self.base_z_mm + self.center_height_mm),
            max(self.base_z_mm, self.base_z_mm + self.center_height_mm),
        )

    @property
    def sampling_spacing_mm(self) -> float:
        return max(min(self.radius_x_mm, self.radius_y_mm) / 24.0, 0.25)


@SURFACE_TYPES.register
@dataclass(frozen=True, slots=True)
class RadialRippleSurface(SurfaceShape):
    """以中心向外扩散的环形波纹曲面。"""

    base_z_mm: float = 0.0
    amplitude_mm: float = 5.0
    wavelength_mm: float = 45.0
    phase_deg: float = 0.0

    id: ClassVar[str] = "radial_ripple"
    label: ClassVar[str] = "径向波纹面"
    description: ClassVar[str] = "从中心向外扩散的同心环形起伏"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("base_z_mm", "基准高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("amplitude_mm", "波纹幅值", K.FLOAT, 5.0, minimum=0.0, maximum=30.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("wavelength_mm", "环纹波长", K.FLOAT, 45.0, minimum=10.0, maximum=1000.0,
                 step=5.0, unit="mm", group="曲面"),
            spec("phase_deg", "相位", K.FLOAT, 0.0, minimum=0.0, maximum=360.0,
                 step=5.0, unit="°", group="曲面"),
        )
    )

    def __post_init__(self) -> None:
        if self.amplitude_mm < 0:
            raise ParameterError("径向波纹幅值不能为负")
        if self.wavelength_mm <= 0:
            raise ParameterError("径向波纹波长必须为正")

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        radius = np.linalg.norm(points, axis=1)
        phase = np.deg2rad(self.phase_deg)
        return self.base_z_mm + self.amplitude_mm * np.sin(
            2.0 * pi * radius / self.wavelength_mm + phase
        )

    def height_bounds(self) -> tuple[float, float]:
        return self.base_z_mm - self.amplitude_mm, self.base_z_mm + self.amplitude_mm

    @property
    def sampling_spacing_mm(self) -> float:
        return max(self.wavelength_mm / 28.0, 0.25)


@SURFACE_TYPES.register
@dataclass(frozen=True, slots=True)
class CompositeSurface(SurfaceShape):
    """多尺度叠加曲面，用于表达更复杂的局部起伏。"""

    base_z_mm: float = 0.0
    primary_amplitude_mm: float = 5.0
    primary_wavelength_x_mm: float = 80.0
    primary_wavelength_y_mm: float = 60.0
    secondary_amplitude_mm: float = 2.5
    secondary_wavelength_x_mm: float = 32.0
    secondary_wavelength_y_mm: float = 42.0
    radial_amplitude_mm: float = 2.0
    radial_wavelength_mm: float = 70.0
    phase_deg: float = 0.0
    rotation_deg: float = 20.0

    id: ClassVar[str] = "composite"
    label: ClassVar[str] = "多尺度复合面"
    description: ClassVar[str] = "叠加长波、短波和径向起伏的复杂参数化曲面"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("base_z_mm", "基准高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面"),
            spec("primary_amplitude_mm", "主波幅值", K.FLOAT, 5.0, minimum=0.0, maximum=30.0,
                 step=0.5, unit="mm", group="长波起伏"),
            spec("primary_wavelength_x_mm", "主波 X 波长", K.FLOAT, 80.0, minimum=10.0,
                 maximum=1000.0, step=5.0, unit="mm", group="长波起伏"),
            spec("primary_wavelength_y_mm", "主波 Y 波长", K.FLOAT, 60.0, minimum=10.0,
                 maximum=1000.0, step=5.0, unit="mm", group="长波起伏"),
            spec("secondary_amplitude_mm", "细节幅值", K.FLOAT, 2.5, minimum=0.0, maximum=20.0,
                 step=0.5, unit="mm", group="短波细节"),
            spec("secondary_wavelength_x_mm", "细节 X 波长", K.FLOAT, 32.0, minimum=10.0,
                 maximum=500.0, step=2.0, unit="mm", group="短波细节"),
            spec("secondary_wavelength_y_mm", "细节 Y 波长", K.FLOAT, 42.0, minimum=10.0,
                 maximum=500.0, step=2.0, unit="mm", group="短波细节"),
            spec("radial_amplitude_mm", "径向幅值", K.FLOAT, 2.0, minimum=0.0, maximum=20.0,
                 step=0.5, unit="mm", group="径向起伏"),
            spec("radial_wavelength_mm", "径向波长", K.FLOAT, 70.0, minimum=10.0,
                 maximum=500.0, step=5.0, unit="mm", group="径向起伏"),
            spec("phase_deg", "相位", K.FLOAT, 0.0, minimum=0.0, maximum=360.0,
                 step=5.0, unit="°", group="曲面"),
            spec("rotation_deg", "纹理旋转", K.FLOAT, 20.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="曲面"),
        )
    )

    def __post_init__(self) -> None:
        if min(self.primary_amplitude_mm, self.secondary_amplitude_mm, self.radial_amplitude_mm) < 0:
            raise ParameterError("复合曲面的起伏幅值不能为负")
        _require_wavelengths(
            self.primary_wavelength_x_mm, self.primary_wavelength_y_mm, "复合曲面主波"
        )
        _require_wavelengths(
            self.secondary_wavelength_x_mm, self.secondary_wavelength_y_mm, "复合曲面细节波"
        )
        if self.radial_wavelength_mm <= 0:
            raise ParameterError("复合曲面的径向波长必须为正")

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        x, y = _rotate_xy(points, self.rotation_deg)
        phase = np.deg2rad(self.phase_deg)
        primary = self.primary_amplitude_mm * np.sin(
            2.0 * pi * x / self.primary_wavelength_x_mm + phase
        ) * np.cos(2.0 * pi * y / self.primary_wavelength_y_mm)
        secondary = self.secondary_amplitude_mm * np.cos(
            2.0 * pi * x / self.secondary_wavelength_x_mm - phase * 0.7
        ) * np.sin(2.0 * pi * y / self.secondary_wavelength_y_mm)
        radial = self.radial_amplitude_mm * np.sin(
            2.0 * pi * np.linalg.norm(points, axis=1) / self.radial_wavelength_mm + phase
        )
        return self.base_z_mm + primary + secondary + radial

    def height_bounds(self) -> tuple[float, float]:
        span = self.primary_amplitude_mm + self.secondary_amplitude_mm + self.radial_amplitude_mm
        return self.base_z_mm - span, self.base_z_mm + span

    @property
    def sampling_spacing_mm(self) -> float:
        shortest = min(
            self.primary_wavelength_x_mm, self.primary_wavelength_y_mm,
            self.secondary_wavelength_x_mm, self.secondary_wavelength_y_mm,
            self.radial_wavelength_mm,
        )
        return max(shortest / 28.0, 0.25)


def _require_wavelengths(x_value: float, y_value: float, label: str) -> None:
    if x_value <= 0 or y_value <= 0:
        raise ParameterError(f"{label}的 X、Y 波长必须为正")


def _rotate_xy(points: NDArray[np.float64], rotation_deg: float) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """将 XY 点绕原点旋转，返回曲面局部坐标。"""

    angle = np.deg2rad(rotation_deg)
    cosine, sine = np.cos(angle), np.sin(angle)
    x = cosine * points[:, 0] + sine * points[:, 1]
    y = -sine * points[:, 0] + cosine * points[:, 1]
    return x, y


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
