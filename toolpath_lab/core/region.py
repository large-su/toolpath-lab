"""加工区域。

区域就是"要加工的那块地方"，它替代了"导入模型 + 提取特征"这一整套前置环节：
直接给定一个规则区域即可开始规划。当前提供四种形状：

- 方形（square）：一个边长，加工面是平面；
- 圆形（circle）：一个直径，加工面是平面；
- 斜坡（ramp）：XY 投影是方形，加工面沿 −X 抬起，最高截到 80 mm 后转成平顶；
- 柱面（cylinder）：XY 投影是方形，加工面是沿 Y 轴方向的圆柱面（拱高可调，0 即平面）。

所有形状统一归约为一条**逆时针、不重复首点**的边界多边形（XY 投影）。加工面由
``height_at`` 给出每个 (x, y) 处的 Z，于是"平面加工""斜面加工"与"曲面加工"走的是同一条
代码路径——策略只管 XY 投影，Z 由区域负责。

加工面是分片平面时，只在折角处补点就足够精确（``surface_breaks``）；是曲面时按
``surface_sample_step_mm`` 沿折线补点。两者都由 ``PlanningContext.to_positions()`` 自动完成，
策略仍然不必知道加工面长什么样。新增形状（椭圆、跑道形、凹多边形……）只要实现
``boundary()`` 就能直接参与规划。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite, pi, radians, tan
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
#: 斜坡的 Z 上限（mm）：斜面超过这个高度就取成平顶。
RAMP_CAP_MM = 80.0
#: 斜坡允许的最大斜度（度）。
RAMP_MAX_ANGLE_DEG = 80.0

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class RegionShape:
    """所有区域形状的基类。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()
    #: 加工面是曲面时，刀路与轮廓沿折线每走这么远就补一个点；平面 / 分片平面为 None（不加密）。
    surface_sample_step_mm: ClassVar[float | None] = None

    def boundary(self) -> NDArray[np.float64]:
        """逆时针闭合边界，形状 (N, 2)，不重复首点。"""

        raise NotImplementedError

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """加工面在这些 (x, y) 处的高度 Z；平面形状恒为 0。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.zeros(planar.shape[0], dtype=np.float64)

    def surface_breaks(
        self, start_xy: NDArray[np.float64], end_xy: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        """直线段上必须插点的位置（加工面出现折角的地方）。

        加工面是分片平面时，只在"平面接平面"的折痕处补点就足够精确；平面形状返回空，
        于是"一刀两个点"的约定保持不变。曲面形状不在这里处理——它们用
        ``surface_sample_step_mm`` 沿折线加密。
        """

        return np.empty((0, 2), dtype=np.float64)

    def surface_max_along(
        self, start_xy: NDArray[np.float64], end_xy: NDArray[np.float64]
    ) -> float | None:
        """线段上加工面的最大高度。

        能解析给出就给出（柱面的拱顶只可能在端点或 x = 0 处），否则返回 None，
        由调用方按采样步长自行采样——采样会漏掉光滑曲面的顶点，所以能解析就别偷懒。
        """

        return None

    def boundary_3d(self) -> NDArray[np.float64]:
        """贴合加工面的三维轮廓，形状 (N, 3)。"""

        planar = self.boundary()
        return np.column_stack((planar, self.height_at(planar)))

    def surface_patches(self) -> list[NDArray[np.float64]]:
        """顶面的分片（每片都是共面的凸多边形，带 Z）。

        前端据此拼出工件实体；平面形状就是轮廓本身一片。
        """

        return [self.boundary_3d()]

    def surface_payload(self) -> dict[str, Any]:
        """加工面的摘要，给界面与脚本看。"""

        outline = self.boundary_3d()
        return {
            "kind": "flat",
            "base_z_mm": float(outline[:, 2].min()),
            "top_z_mm": float(outline[:, 2].max()),
            "patch_count": len(self.surface_patches()),
        }

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
            "surface": self.surface_payload(),
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
class RampRegion(RegionShape):
    """XY 投影是方形、沿 +Z 抬起的斜面，Z 到 RAMP_CAP_MM 截成平顶。

    低边是 **+X 方向的最外侧边**（x = +边长/2，Z = 0），沿 −X 方向线性升高；
    `tan(斜度) · 边长` 超过 RAMP_CAP_MM 时，多出来的部分取平顶——斜度越大平顶越宽，
    最高的地方始终不超过 RAMP_CAP_MM。加工面以下仍保留与其它区域一样的基体厚度。
    """

    side_mm: float = 80.0
    angle_deg: float = 30.0

    id: ClassVar[str] = "ramp"
    label: ClassVar[str] = "斜坡"
    description: ClassVar[str] = (
        f"XY 投影为方形的斜面：以 +X 最外侧边为低边向 −X 抬起，最高到 {RAMP_CAP_MM:g} mm 后转平顶"
    )
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("side_mm", "边长", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="XY 投影方向的边长（默认 80 × 80）"),
            spec("angle_deg", "斜度", K.FLOAT, 30.0, minimum=0.0,
                 maximum=RAMP_MAX_ANGLE_DEG, step=5.0, unit="°", group="区域",
                 help=f"加工面与 XY 平面的夹角；0° 就是平面，最大 {RAMP_MAX_ANGLE_DEG:g}°"),
        )
    )

    def __post_init__(self) -> None:
        if not isfinite(self.side_mm) or self.side_mm <= 0:
            raise ParameterError("斜坡边长必须是有限正数")
        if not isfinite(self.angle_deg) or not 0.0 <= self.angle_deg <= RAMP_MAX_ANGLE_DEG:
            raise ParameterError(f"斜坡斜度必须在 0° 到 {RAMP_MAX_ANGLE_DEG:g}° 之间")

    # -- 加工面 ------------------------------------------------------------
    @property
    def slope(self) -> float:
        """tan(斜度)：沿 −X 每走 1 mm 抬起多少。"""

        return tan(radians(self.angle_deg))

    @property
    def crease_x_mm(self) -> float | None:
        """斜面转平顶的折痕所在的 x；整块都还是斜面时为 None。"""

        if self.slope <= _EPS:
            return None
        half = self.side_mm / 2.0
        crease = half - RAMP_CAP_MM / self.slope
        return crease if crease > -half + 1e-9 else None

    @property
    def peak_z_mm(self) -> float:
        """加工面实际达到的最高点（斜度不够时到不了 RAMP_CAP_MM）。"""

        return float(self.height_at(np.array([[-self.side_mm / 2.0, 0.0]]))[0])

    def boundary(self) -> NDArray[np.float64]:
        half = self.side_mm / 2.0
        return np.array(
            [(-half, -half), (half, -half), (half, half), (-half, half)],
            dtype=np.float64,
        )

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        rise = (self.side_mm / 2.0 - planar[:, 0]) * self.slope
        return np.clip(rise, 0.0, RAMP_CAP_MM)

    def surface_breaks(
        self, start_xy: NDArray[np.float64], end_xy: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        crease = self.crease_x_mm
        if crease is None:
            return np.empty((0, 2), dtype=np.float64)
        start = np.asarray(start_xy, dtype=np.float64).reshape(2)
        end = np.asarray(end_xy, dtype=np.float64).reshape(2)
        if (start[0] - crease) * (end[0] - crease) >= 0.0:
            return np.empty((0, 2), dtype=np.float64)
        fraction = (crease - start[0]) / (end[0] - start[0])
        return (start + fraction * (end - start)).reshape(1, 2)

    def surface_patches(self) -> list[NDArray[np.float64]]:
        """顶面分片：斜段一片（靠 +X 的低边）、平顶一片（靠 −X 的高边）。"""

        half = self.side_mm / 2.0
        crease = self.crease_x_mm
        if crease is None:
            corners = self.boundary()
            return [self._patch([tuple(corner) for corner in corners])]
        return [
            self._patch([(crease, -half), (half, -half), (half, half), (crease, half)]),
            self._patch([(-half, -half), (crease, -half), (crease, half), (-half, half)]),
        ]

    def _patch(self, corners: list[tuple[float, float]]) -> NDArray[np.float64]:
        planar = np.array(corners, dtype=np.float64)
        return np.column_stack((planar, self.height_at(planar)))

    def surface_payload(self) -> dict[str, Any]:
        return {
            **super().surface_payload(),
            "kind": "ramp",
            "angle_deg": self.angle_deg,
            "slope": self.slope,
            "cap_z_mm": RAMP_CAP_MM,
            "crease_x_mm": self.crease_x_mm,
        }


#: 柱面母线在 X 方向上的采样点数（默认边长 80 mm 时正好 1 mm 一个点）。
CROWN_PROFILE_POINTS = 81


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class CylinderRegion(RegionShape):
    """XY 投影是方形、沿 Y 轴方向拱起的柱面；拱高 0 即平面，最大是边长的一半（半圆柱）。

    加工面是一条圆弧母线沿 Y 轴扫出来的：圆心在 Z = 拱高 − 半径，圆弧同时经过
    (−边长/2, 0)、(0, 拱高)、(边长/2, 0) 三点，所以弦长与拱高唯一决定圆弧半径
    R = (边长²/4 + 拱高²) / (2 · 拱高)。
    """

    side_mm: float = 80.0
    crown_mm: float = 20.0

    id: ClassVar[str] = "cylinder"
    label: ClassVar[str] = "柱面"
    description: ClassVar[str] = "XY 投影为方形的圆柱面：沿 Y 轴拱起，拱高可调（0 即平面）"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("side_mm", "边长", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="XY 投影方向的边长（默认 80 × 80）"),
            spec("crown_mm", "拱高", K.FLOAT, 20.0, minimum=0.0, maximum=500.0,
                 step=1.0, unit="mm", group="区域",
                 help="圆弧最高点相对边缘的高差；0 就是平面，最大是边长的一半"),
        )
    )
    #: 曲面要加密：刀路与轮廓沿折线每 1 mm 取一个点。
    surface_sample_step_mm: ClassVar[float | None] = 1.0

    def __post_init__(self) -> None:
        if not isfinite(self.side_mm) or self.side_mm <= 0:
            raise ParameterError("柱面边长必须是有限正数")
        if not isfinite(self.crown_mm) or self.crown_mm < 0:
            raise ParameterError("柱面拱高必须是非负有限数")
        if self.crown_mm > self.side_mm / 2.0 + 1e-9:
            raise ParameterError(
                f"柱面拱高 {self.crown_mm:g} mm 不能超过边长的一半 "
                f"（{self.side_mm / 2.0:g} mm，否则这段圆弧不存在）"
            )

    # -- 加工面 ------------------------------------------------------------
    @property
    def crown_radius_mm(self) -> float:
        """圆弧半径；拱高为 0 时返回 inf（退化成平面）。"""

        if self.crown_mm <= _EPS:
            return float("inf")
        half = self.side_mm / 2.0
        return (half * half + self.crown_mm * self.crown_mm) / (2.0 * self.crown_mm)

    def boundary(self) -> NDArray[np.float64]:
        """方形边界，但沿 X 方向加密——拱形曲面的侧壁就是这条母线。"""

        half = self.side_mm / 2.0
        step = self.surface_sample_step_mm or self.side_mm
        xs = np.arange(-half, half + 1e-9, step)
        if abs(float(xs[-1]) - half) > 1e-9:
            xs = np.append(xs, half)
        lower = np.column_stack((xs, np.full_like(xs, -half)))
        upper = np.column_stack((xs[::-1], np.full_like(xs, half)))
        return np.vstack((lower, upper))

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        if self.crown_mm <= _EPS:
            return np.zeros(planar.shape[0], dtype=np.float64)
        radius = self.crown_radius_mm
        x = np.clip(planar[:, 0], -self.side_mm / 2.0, self.side_mm / 2.0)
        height = (self.crown_mm - radius) + np.sqrt(np.maximum(radius * radius - x * x, 0.0))
        # 边缘处两项相减会有 ~1e-15 的负零头，夹掉以免 G-code 里出现 Z-0.000。
        return np.maximum(height, 0.0)

    def surface_patches(self) -> list[NDArray[np.float64]]:
        """曲面没有共面的分片：顶面交给前端的"母线扫掠"（见 surface_profile）。"""

        return []

    def surface_max_along(
        self, start_xy: NDArray[np.float64], end_xy: NDArray[np.float64]
    ) -> float | None:
        """母线沿 X 单调升到 x = 0 的拱顶，所以最大值只可能在两端或 x = 0 处。"""

        if self.crown_mm <= _EPS:
            return 0.0
        x_start = float(np.asarray(start_xy, dtype=np.float64).reshape(2)[0])
        x_end = float(np.asarray(end_xy, dtype=np.float64).reshape(2)[0])
        if min(x_start, x_end) <= 0.0 <= max(x_start, x_end):
            return self.crown_mm
        closer = x_start if abs(x_start) <= abs(x_end) else x_end
        return float(self.height_at(np.array([[closer, 0.0]]))[0])

    def surface_profile(self) -> list[list[float]]:
        """母线（X–Z 平面的轮廓），等间距采样，供前端扫掠成曲面。"""

        xs = np.linspace(-self.side_mm / 2.0, self.side_mm / 2.0, CROWN_PROFILE_POINTS)
        zs = self.height_at(np.column_stack((xs, np.zeros_like(xs))))
        return [[float(x), float(z)] for x, z in zip(xs, zs)]

    def surface_payload(self) -> dict[str, Any]:
        outline = self.boundary_3d()
        radius = self.crown_radius_mm
        return {
            "kind": "sweep",
            "axis": "y",
            "from_mm": -self.side_mm / 2.0,
            "to_mm": self.side_mm / 2.0,
            "profile": self.surface_profile(),
            "crown_mm": self.crown_mm,
            "crown_radius_mm": None if radius == float("inf") else radius,
            "base_z_mm": float(outline[:, 2].min()),
            "top_z_mm": float(outline[:, 2].max()),
        }


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
