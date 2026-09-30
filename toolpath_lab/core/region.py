"""加工区域。

区域就是"要加工的那块地方"，它替代了"导入模型 + 提取特征"这一整套前置环节：
直接给定一个规则区域即可开始规划。当前提供三种形状：

- 方形（square）：一个边长；
- 圆形（circle）：一个直径；
- 斜坡（ramp）：XY 投影是方形，加工面沿 −X 抬起，升到「Z 上限」（默认 80 mm，可设）后转成平顶。

所有形状统一归约为一条**逆时针、不重复首点**的边界多边形（XY 投影）。加工面由
``height_at`` 给出每个 (x, y) 处的 Z：平面形状恒为 0，斜坡是一个被截断的斜面。
因此
"平面加工"与"斜面加工"走的是同一条代码路径——策略只管 XY 投影，Z 由区域负责。
新增形状（椭圆、跑道形、凹多边形……）只要实现 boundary() 就能直接参与规划。
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
#: 斜坡 Z 上限的默认值（mm）：斜面沿 Z 轴升到这个高度就取成平顶。可以在区域参数里改。
DEFAULT_RAMP_CAP_MM = 80.0
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
        于是"一刀两个点"的约定保持不变。
        """

        return np.empty((0, 2), dtype=np.float64)

    def boundary_3d(self) -> NDArray[np.float64]:
        """贴合加工面的三维轮廓，形状 (N, 3)。"""

        planar = self.boundary()
        return np.column_stack((planar, self.height_at(planar)))

    def machining_boundary(self, footprint_mm: float) -> NDArray[np.float64]:
        """刀路的可取范围（XY 逆时针多边形），默认就是区域轮廓。

        只在"加工面在区域内部转折"时才需要重写：斜坡只加工斜面段时，斜面与平顶的分界处
        并不是零件的边（那边还有材料），所以要把那条边往外挪一个足迹半径——按足迹内缩之后
        刀路正好停在折痕上，既不会走到平顶，也不会在分界处留下一条没切到的窄条。
        """

        return self.boundary()

    def machining_boundary_3d(self, footprint_mm: float) -> NDArray[np.float64]:
        """贴合加工面的刀路覆盖范围，形状 (N, 3)。"""

        planar = self.machining_boundary(footprint_mm)
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
            # 工件实体的厚度（加工面以下那块基体）：纯几何，不参与刀路计算。
            # 新形状没声明这个字段时退回默认值，扩展路径因此不会被打断。
            "thickness_mm": float(
                getattr(self, "thickness_mm", DEFAULT_REGION_THICKNESS_MM)
            ),
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


#: 平面区域共用的"加工面高度"参数：加工面（连同它下面的基体）整体抬到该高度。
PLANAR_HEIGHT_SPEC = spec(
    "height_mm", "加工面高度", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
    step=1.0, unit="mm", group="区域",
    help="加工面相对基准面 Z = 0 的高度；刀路 Z、安全面与 G-code 都跟着它走",
)

#: 部件厚度的默认值（mm）。
DEFAULT_REGION_THICKNESS_MM = 20.0

#: 所有区域共用的"部件厚度"参数：加工面以下那块基体有多厚。
REGION_THICKNESS_SPEC = spec(
    "thickness_mm", "部件厚度", K.FLOAT, DEFAULT_REGION_THICKNESS_MM,
    minimum=1.0, maximum=500.0, step=1.0, unit="mm", group="区域",
    help="加工面以下那块基体的厚度；只影响工件实体与显示，不参与刀路计算",
)


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class SquareRegion(RegionShape):
    """以原点为中心的方形区域，加工面是可以设高度的水平面。"""

    side_mm: float = 80.0
    height_mm: float = 0.0
    thickness_mm: float = DEFAULT_REGION_THICKNESS_MM

    id: ClassVar[str] = "square"
    label: ClassVar[str] = "方形"
    description: ClassVar[str] = "面铣最常见的形状，用来对比往复与单向；加工面高度与部件厚度可设"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("side_mm", "边长", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            PLANAR_HEIGHT_SPEC,
            REGION_THICKNESS_SPEC,
        )
    )

    def __post_init__(self) -> None:
        if self.side_mm <= 0:
            raise ParameterError("方形边长必须为正")
        if not isfinite(self.height_mm):
            raise ParameterError("方形加工面高度必须是有限数")
        if not isfinite(self.thickness_mm) or self.thickness_mm <= 0:
            raise ParameterError("方形部件厚度必须是有限正数")

    def boundary(self) -> NDArray[np.float64]:
        half = self.side_mm / 2.0
        return np.array(
            [(-half, -half), (half, -half), (half, half), (-half, half)],
            dtype=np.float64,
        )

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.full(planar.shape[0], self.height_mm, dtype=np.float64)


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class CircleRegion(RegionShape):
    """以原点为中心的圆形区域，加工面是可以设高度的水平面。"""

    diameter_mm: float = 80.0
    height_mm: float = 0.0
    thickness_mm: float = DEFAULT_REGION_THICKNESS_MM

    id: ClassVar[str] = "circle"
    label: ClassVar[str] = "圆形"
    description: ClassVar[str] = "圆形端面，用来观察刀路在曲线边界上的收放；加工面高度与部件厚度可设"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("diameter_mm", "直径 D", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            PLANAR_HEIGHT_SPEC,
            REGION_THICKNESS_SPEC,
        )
    )

    def __post_init__(self) -> None:
        if self.diameter_mm <= 0:
            raise ParameterError("圆形直径必须为正")
        if not isfinite(self.height_mm):
            raise ParameterError("圆形加工面高度必须是有限数")
        if not isfinite(self.thickness_mm) or self.thickness_mm <= 0:
            raise ParameterError("圆形部件厚度必须是有限正数")

    def boundary(self) -> NDArray[np.float64]:
        radius = self.diameter_mm / 2.0
        angles = np.linspace(0.0, 2.0 * pi, CIRCLE_SEGMENTS, endpoint=False)
        return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))

    def height_at(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.full(planar.shape[0], self.height_mm, dtype=np.float64)


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class RampRegion(RegionShape):
    """XY 投影是方形、沿 +Z 抬起的斜面，升到 Z 上限后截成平顶。

    低边是 **+X 方向的最外侧边**（x = +边长/2，Z = 0），沿 −X 方向线性升高；
    `tan(斜度) · 边长` 超过 Z 上限（`cap_z_mm`，默认 80 mm）时，多出来的部分取平顶——
    上限越小、斜度越大，平顶越宽；加工面的最高处始终不超过它。加工面以下保留部件厚度。
    """

    side_mm: float = 80.0
    angle_deg: float = 30.0
    cap_z_mm: float = DEFAULT_RAMP_CAP_MM
    include_plateau: bool = False
    thickness_mm: float = DEFAULT_REGION_THICKNESS_MM

    id: ClassVar[str] = "ramp"
    label: ClassVar[str] = "斜坡"
    description: ClassVar[str] = (
        f"XY 投影为方形的斜面：以 +X 最外侧边为低边向 −X 抬起，"
        f"升到 Z 上限（默认 {DEFAULT_RAMP_CAP_MM:g} mm，可设）后转平顶"
    )
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("side_mm", "边长", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="XY 投影方向的边长（默认 80 × 80）"),
            spec("angle_deg", "斜度", K.FLOAT, 30.0, minimum=0.0,
                 maximum=RAMP_MAX_ANGLE_DEG, step=5.0, unit="°", group="区域",
                 help=f"加工面与 XY 平面的夹角；0° 就是平面，最大 {RAMP_MAX_ANGLE_DEG:g}°"),
            spec("cap_z_mm", "Z 上限", K.FLOAT, DEFAULT_RAMP_CAP_MM, minimum=1.0,
                 maximum=1000.0, step=5.0, unit="mm", group="区域",
                 help="斜面沿 Z 轴能升到的最大高度，也就是斜面在 Z 方向的投影长度上限；升到它就转平顶"),
            spec("include_plateau", "加工平顶", K.BOOL, False, group="区域",
                 help="关（默认）：刀路只覆盖斜面段，升到 Z 上限后的平顶留给别的工序；开：平顶一起加工"),
            REGION_THICKNESS_SPEC,
        )
    )

    def __post_init__(self) -> None:
        if not isfinite(self.side_mm) or self.side_mm <= 0:
            raise ParameterError("斜坡边长必须是有限正数")
        if not isfinite(self.angle_deg) or not 0.0 <= self.angle_deg <= RAMP_MAX_ANGLE_DEG:
            raise ParameterError(f"斜坡斜度必须在 0° 到 {RAMP_MAX_ANGLE_DEG:g}° 之间")
        if not isfinite(self.cap_z_mm) or self.cap_z_mm <= 0:
            raise ParameterError("斜坡 Z 上限必须是有限正数")
        if not isfinite(self.thickness_mm) or self.thickness_mm <= 0:
            raise ParameterError("斜坡部件厚度必须是有限正数")

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
        crease = half - self.cap_z_mm / self.slope
        return crease if crease > -half + 1e-9 else None

    @property
    def peak_z_mm(self) -> float:
        """加工面实际达到的最高点（斜度不够时到不了 Z 上限）。"""

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
        return np.clip(rise, 0.0, self.cap_z_mm)

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

    def machining_boundary(self, footprint_mm: float) -> NDArray[np.float64]:
        """只加工斜面段时，把分界处（x = 折痕）的那条边往平顶方向挪一个足迹半径。

        这样按足迹内缩之后，刀路正好停在折痕上：既不会走上平顶，也不会在分界处留下一条
        足迹宽度的窄条没切到。传 footprint = 0 得到的就是斜面段本身的范围（界面用它画轮廓）。
        """

        crease = self.crease_x_mm
        if self.include_plateau or crease is None:
            return self.boundary()
        half = self.side_mm / 2.0
        limit = max(crease - max(float(footprint_mm), 0.0), -half)
        return np.array(
            [(limit, -half), (half, -half), (half, half), (limit, half)],
            dtype=np.float64,
        )

    def surface_payload(self) -> dict[str, Any]:
        return {
            **super().surface_payload(),
            "kind": "ramp",
            "angle_deg": self.angle_deg,
            "slope": self.slope,
            "cap_z_mm": self.cap_z_mm,
            "crease_x_mm": self.crease_x_mm,
        }


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """由接口参数构造一个已注册的区域形状。"""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
