"""Machining regions.

A region is "the area to be machined", which replaces the whole "import a model, extract features"
front end: give it a regular region and planning can start. Seven shapes are available:

- square: one side length;
- rectangle: width x height;
- circle: one diameter;
- ellipse: semi-major / semi-minor axis;
- u_shape: outer width / outer height / wall thickness, a concave polygon;
- dumbbell: two pads joined by a narrow neck; once the offset eats the neck, one layer splits into
  two loops;
- triangle: base and height, the apex angle can be made very sharp.

Every shape reduces to a single **counter-clockwise boundary polygon without a repeated first point**.
The raster strategy only needs "intersect a line with a polygon", and the 3D workpiece is extruded from
the same boundary, so a new shape (a stadium, a notched polygon, ...) only has to implement boundary()
to take part in planning and display -- no toolpath or front end change; a scanline through a concave
shape (the U) yields several intervals, so one row can carry several independent toolpaths.
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

#: How many polyline segments approximate a curved boundary (circle, ellipse). Fixed on purpose:
#: exposing the discretisation as a parameter would add a knob with very little meaning.
CURVE_SEGMENTS = 180


@dataclass(frozen=True, slots=True)
class RegionShape:
    """Base class of every region shape."""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()

    def boundary(self) -> NDArray[np.float64]:
        """Closed counter-clockwise boundary, shape (N, 2), without repeating the first point."""

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
    """Signed polygon area (positive for counter-clockwise winding)."""

    x = polygon[:, 0]
    y = polygon[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def polygon_bounds(polygon: NDArray[np.float64]) -> list[list[float]]:
    """Axis aligned bounding box as [[x_min, x_max], [y_min, y_max]]."""

    return [
        [float(polygon[:, 0].min()), float(polygon[:, 0].max())],
        [float(polygon[:, 1].min()), float(polygon[:, 1].max())],
    ]


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class SquareRegion(RegionShape):
    """Square region centred on the origin."""

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
    """Circular region centred on the origin."""

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
    """Rectangular region centred on the origin (width along X, height along Y)."""

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
    """Elliptical region centred on the origin (semi-major along X, semi-minor along Y)."""

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
    """U shaped region centred on the origin, opening upwards (a concave polygon)."""

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
    """Dumbbell centred on the origin: two pads joined by a narrow neck."""

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
    """Isosceles triangle centred on the origin (base at the bottom, apex at the top)."""

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


@dataclass(frozen=True, slots=True)
class ImportedOutlineRegion(RegionShape):
    """A region built from an imported drawing outline.

    Deliberately **not** registered in REGION_SHAPES: its geometry cannot come from the parameter
    system (a ParameterSpec has no list kind), so it is built by `region_from_points` from the points
    a caller got out of the DXF importer. Staying out of the catalogue means the UI can never offer a
    shape it has no points for.
    """

    points: tuple[tuple[float, float], ...] = ()
    id: ClassVar[str] = "imported"
    label: ClassVar[str] = "导入轮廓"
    description: ClassVar[str] = "从图纸导入的闭合轮廓（点串随请求给出）"

    def boundary(self) -> NDArray[np.float64]:
        polygon = np.asarray(self.points, dtype=np.float64).reshape(-1, 2)
        if polygon.shape[0] < 3:
            raise ParameterError("导入的轮廓至少需要 3 个点")
        # The contract is counter-clockwise; a drawing may be either. Signed area decides, and it is
        # computed here rather than imported from planning/geometry2d, which core must not depend on.
        if _signed_polygon_area(polygon) < 0.0:
            polygon = polygon[::-1]
        return np.ascontiguousarray(polygon)


def _signed_polygon_area(polygon: NDArray[np.float64]) -> float:
    x, y = polygon[:, 0], polygon[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def region_from_points(points) -> RegionShape:
    """Build an imported outline region from raw [x, y] points.

    This is the one place in the project where a region's geometry does not come through the
    parameter system: the DXF importer hands over a point list, and a ParameterSpec cannot describe
    one. Everything else (tool, strategy, all their parameters) still goes through the declarations,
    and the region still has to satisfy the same boundary contract.
    """

    cleaned: list[tuple[float, float]] = []
    for point in points:
        values = list(point)
        if len(values) < 2:
            raise ParameterError("导入轮廓的点必须是 [x, y]")
        cleaned.append((float(values[0]), float(values[1])))
    if len(cleaned) < 3:
        raise ParameterError("导入的轮廓至少需要 3 个点")
    first, last = cleaned[0], cleaned[-1]
    if ((first[0] - last[0]) ** 2 + (first[1] - last[1]) ** 2) ** 0.5 <= 1e-6:
        cleaned.pop()
    return ImportedOutlineRegion(points=tuple(cleaned))


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """Build a registered region shape from API parameters."""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
