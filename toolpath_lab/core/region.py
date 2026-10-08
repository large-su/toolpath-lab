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
from math import isfinite, pi
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

#: Upper bound on the cells of the coarse stock-top grid that travels to the 3D view (curved blanks
#: only); the square root of it caps the cells per axis.
TOP_MAP_CELLS = 1024


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

    def islands(self) -> tuple[NDArray[np.float64], ...]:
        """Material inside the region that must **not** be machined: holes, each a CCW polygon.

        Empty for every solid region, which is all of them until a shape or an imported drawing brings
        one along. An island is *meant* to stay, so it is not "uncut material": the tool is kept a
        cutting radius away from it, the analysis counts the region as `boundary() - islands`, and the
        3D workpiece is extruded with them as holes.
        """

        return ()

    @property
    def is_flat_top(self) -> bool:
        """Whether the stock's top face is the machining plane (Z = 0) everywhere.

        Every region this project has ever had is flat; a curved blank (see `DomeRegion`) overrides
        this and the two places that care ask here: the removal measurement starts its height map from
        `top_height_mm` instead of assuming zero, and the plan notes say that 2.5D layers cut air above
        the curve before they reach it.
        """

        return True

    def top_height_mm(self, x: Any, y: Any) -> Any:
        """Z of the **stock's top face** at (x, y); NaN means "no material at this XY".

        Flat regions answer zero for any point, which is what every number in this project was
        computed with. A curved blank overrides it and the callers adapt: the removal sweep seeds its
        grid with these heights (so volumes are measured against the real blank), and the viewport
        draws the top face from `top_map`. Takes scalars or arrays and returns a plain float for a
        scalar, so it can go straight into a payload.
        """

        values = np.asarray(x, dtype=np.float64) * 0.0 + np.asarray(y, dtype=np.float64) * 0.0
        return float(values) if np.ndim(values) == 0 else values

    def top_map(self, max_cells: int = TOP_MAP_CELLS) -> dict[str, Any] | None:
        """Coarse grid of the stock's top face for the 3D view: None while the top is flat.

        The grid has the same shape as the removal height map (cell centres plus `cell_size_mm`), so the
        viewport draws it with the geometry builder it already has; cells without material are None.
        """

        if self.is_flat_top:
            return None
        polygon = self.boundary()
        x_min, x_max = float(polygon[:, 0].min()), float(polygon[:, 0].max())
        y_min, y_max = float(polygon[:, 1].min()), float(polygon[:, 1].max())
        span_x, span_y = max(x_max - x_min, 1e-9), max(y_max - y_min, 1e-9)
        count = max(2, min(int(max_cells**0.5), 64))
        step_x, step_y = span_x / count, span_y / count
        xs = x_min + (np.arange(count) + 0.5) * step_x
        ys = y_min + (np.arange(count) + 0.5) * step_y
        grid_x, grid_y = np.meshgrid(xs, ys)
        heights = np.asarray(self.top_height_mm(grid_x, grid_y), dtype=np.float64)
        cells = [
            [None if not np.isfinite(value) else round(float(value), 4) for value in row]
            for row in heights
        ]
        return {
            "rows": count,
            "cols": count,
            "origin_mm": [round(x_min, 4), round(y_min, 4)],
            "cell_size_mm": [round(step_x, 4), round(step_y, 4)],
            # The height map's shape, so the viewport can draw this with the builder it already has.
            "floor_mm": 0.0,
            "cells": cells,
        }

    def to_params(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def header_text(self) -> str:
        """One line describing the region in a self-describing export header.

        A shape whose description is not a handful of numbers overrides this: an imported outline
        states how many points it has instead of printing the whole list into the file.
        """

        return f"{self.id} - {_format_parameters(self.to_params())}"

    def describe(self) -> dict[str, Any]:
        polygon = self.boundary()
        islands = self.islands()
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "parameters": self.to_params(),
            "area_mm2": polygon_area(polygon)
            - sum(abs(polygon_area(island)) for island in islands),
            "bounds_mm": polygon_bounds(polygon),
            # A flat top sends no map at all, so nothing about the old payloads changes.
            "flat_top": self.is_flat_top,
            "top_map": self.top_map(),
            # Islands travel as polygons too: the 3D view needs them to cut the holes it extrudes.
            "islands": [
                {
                    "boundary": [[round(float(x), 4), round(float(y), 4)] for x, y in island],
                    "point_count": int(island.shape[0]),
                    "area_mm2": round(abs(polygon_area(island)), 4),
                }
                for island in islands
            ],
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


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class RingRegion(RegionShape):
    """A round pocket with a concentric round boss left standing in it: the first region with an island.

    This is the analytic island case: the machinable band is an annulus `(D - d) / 2` wide, so every
    number a planner or the analysis produces can be checked against a closed form (the island is never
    machined, the tool stays a radius away from it, and the ring it walks around it is a circle).
    """

    diameter_mm: float = 100.0
    island_diameter_mm: float = 30.0

    id: ClassVar[str] = "ring"
    label: ClassVar[str] = "圆环"
    description: ClassVar[str] = "圆形型腔 + 中心圆岛：刀路要绕开岛屿，覆盖率与切除仿真也不再算岛屿那块料"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("diameter_mm", "外径 D", K.FLOAT, 100.0, minimum=10.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域"),
            spec("island_diameter_mm", "岛直径 d", K.FLOAT, 30.0, minimum=2.0, maximum=900.0,
                 step=5.0, unit="mm", group="区域",
                 help="中心圆岛的直径：这块料要留下，刀路绕它走、分析也不算它"
                      "（必须小于外径，且带宽 (D−d)/2 要放得下刀具）"),
        )
    )

    def __post_init__(self) -> None:
        if self.diameter_mm <= 0:
            raise ParameterError("圆环外径必须为正")
        if self.island_diameter_mm <= 0:
            raise ParameterError("圆岛直径必须为正")
        if self.island_diameter_mm >= self.diameter_mm:
            raise ParameterError(
                f"圆岛直径 {self.island_diameter_mm:g} mm 必须小于外径 {self.diameter_mm:g} mm"
            )

    @property
    def annulus_width_mm(self) -> float:
        """Width of the machinable band between the outline and the island."""

        return (self.diameter_mm - self.island_diameter_mm) / 2.0

    def boundary(self) -> NDArray[np.float64]:
        radius = self.diameter_mm / 2.0
        angles = np.linspace(0.0, 2.0 * pi, CURVE_SEGMENTS, endpoint=False)
        return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))

    def islands(self) -> tuple[NDArray[np.float64], ...]:
        radius = self.island_diameter_mm / 2.0
        angles = np.linspace(0.0, 2.0 * pi, CURVE_SEGMENTS, endpoint=False)
        return (np.column_stack((radius * np.cos(angles), radius * np.sin(angles))),)


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class DomeRegion(RegionShape):
    """A round blank with a **spherical cap** on top: the first region whose top face is not flat.

    The boundary is the same disc as `CircleRegion`, so every planner walks it unchanged -- and that is
    the honest part of this shape: the toolpath stays 2.5D (constant Z layers), which on a curved blank
    means the upper layers cut air until they reach the surface. What the curve changes is everything
    measured *against* the blank: the removal sweep starts its height map on this surface instead of at
    Z = 0, so the volumes and the height map describe the real part, and the 3D view draws it.
    """

    diameter_mm: float = 80.0
    #: Height of the cap above the rim plane.
    dome_height_mm: float = 12.0

    id: ClassVar[str] = "dome"
    label: ClassVar[str] = "球冠"
    description: ClassVar[str] = "圆形毛坯 + 球冠上表面：2.5D 等高分层照旧，但切除仿真与三维显示按曲面起算"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("diameter_mm", "直径 D", K.FLOAT, 80.0, minimum=5.0, maximum=1000.0,
                 step=5.0, unit="mm", group="区域", help="底面圆的直径，也是球冠的边缘"),
            spec("dome_height_mm", "球冠高度 h", K.FLOAT, 12.0, minimum=0.5, maximum=200.0,
                 step=0.5, unit="mm", group="区域",
                 help="上表面是过一个圆边缘的球冠：中心比边缘高 h。h 越大越鼓，"
                      "球面半径 Rc = (R² + h²) / (2h) 随参数一起算出"),
        )
    )

    def __post_init__(self) -> None:
        if self.diameter_mm <= 0:
            raise ParameterError("球冠的直径必须为正")
        if self.dome_height_mm <= 0:
            raise ParameterError("球冠高度必须为正（0 就不是球冠了）")

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def sphere_radius_mm(self) -> float:
        """Radius of the sphere the cap is cut from: the one through the rim, centre h above it."""

        radius = self.radius_mm
        return (radius * radius + self.dome_height_mm**2) / (2.0 * self.dome_height_mm)

    @property
    def cap_volume_mm3(self) -> float:
        """Volume of the cap above the rim plane: `pi h^2 (3 Rc - h) / 3`."""

        height, sphere = self.dome_height_mm, self.sphere_radius_mm
        return float(pi * height * height * (3.0 * sphere - height) / 3.0)

    @property
    def is_flat_top(self) -> bool:
        return False

    def top_height_mm(self, x: Any, y: Any) -> Any:
        radial = np.sqrt(
            np.asarray(x, dtype=np.float64) ** 2 + np.asarray(y, dtype=np.float64) ** 2
        )
        sphere = self.sphere_radius_mm
        # The cap surface, clamped at the rim plane; outside the disc there is no material at all.
        surface = np.sqrt(np.maximum(sphere * sphere - radial * radial, 0.0)) - (sphere - self.dome_height_mm)
        surface = np.maximum(surface, 0.0)
        values = np.where(radial <= self.radius_mm, surface, np.nan)
        return float(values) if np.ndim(values) == 0 else values

    def boundary(self) -> NDArray[np.float64]:
        radius = self.radius_mm
        angles = np.linspace(0.0, 2.0 * pi, CURVE_SEGMENTS, endpoint=False)
        return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))


@dataclass(frozen=True, slots=True)
class ImportedOutlineRegion(RegionShape):
    """A region built from an imported drawing outline.

    Deliberately **not** registered in REGION_SHAPES: its geometry cannot come from the parameter
    system (a ParameterSpec has no list kind), so it is built by `region_from_points` from the points
    a caller got out of the DXF importer. Staying out of the catalogue means the UI can never offer a
    shape it has no points for.
    """

    points: tuple[tuple[float, float], ...] = ()
    #: Further closed outlines of the same drawing, kept as islands (holes) inside the region.
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()
    id: ClassVar[str] = "imported"
    label: ClassVar[str] = "导入轮廓"
    description: ClassVar[str] = "从图纸导入的闭合轮廓（点串随请求给出）"

    def to_params(self) -> dict[str, Any]:
        """Just the counts: the lists themselves are already in the boundary of the same response."""

        return {"point_count": len(self.points), "island_count": len(self.holes)}

    def header_text(self) -> str:
        text = f"{self.id} - {len(self.points)} points"
        return f"{text} + {len(self.holes)} islands" if self.holes else text

    def boundary(self) -> NDArray[np.float64]:
        return _closed_polygon(self.points, "导入的轮廓至少需要 3 个点")

    def islands(self) -> tuple[NDArray[np.float64], ...]:
        return tuple(
            _closed_polygon(hole, "导入的岛屿至少需要 3 个点") for hole in self.holes
        )


def _closed_polygon(points, message: str) -> NDArray[np.float64]:
    """A CCW boundary polygon from raw pairs (the contract every consumer relies on)."""

    polygon = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if polygon.shape[0] < 3:
        raise ParameterError(message)
    # The contract is counter-clockwise; a drawing may be either. Signed area decides, and it is
    # computed here rather than imported from planning/geometry2d, which core must not depend on.
    if _signed_polygon_area(polygon) < 0.0:
        polygon = polygon[::-1]
    return np.ascontiguousarray(polygon)


def _format_parameters(parameters: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in parameters.items())


def _signed_polygon_area(polygon: NDArray[np.float64]) -> float:
    x, y = polygon[:, 0], polygon[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def region_from_points(points, islands=()) -> RegionShape:
    """Build an imported outline region from raw [x, y] points, with optional islands.

    This is the one place in the project where a region's geometry does not come through the
    parameter system: the DXF importer hands over a point list, and a ParameterSpec cannot describe
    one. Everything else (tool, strategy, all their parameters) still goes through the declarations,
    and the region still has to satisfy the same boundary contract.

    `islands` are further closed outlines of the same drawing: they become holes in the region, which
    the planners keep away from and the analysis does not count as uncut material (a drawing of a pocket
    with a boss in it is exactly two outlines).

    Points are pairs, because a region is planar: a third coordinate is refused here rather than
    silently dropped, since the boundary is built by reshaping the list to (N, 2) and a "flattened"
    list could otherwise pass this check and only fail later, deeper inside planning. That also means
    the boundary a plan response carries has to lose its trailing Z before it can be sent back.
    """

    cleaned = _clean_points(points, "导入轮廓")
    if len(cleaned) < 3:
        raise ParameterError("导入的轮廓至少需要 3 个点")
    first, last = cleaned[0], cleaned[-1]
    if ((first[0] - last[0]) ** 2 + (first[1] - last[1]) ** 2) ** 0.5 <= 1e-6:
        cleaned.pop()
    holes = tuple(
        tuple(_clean_points(island, "导入的岛屿"))
        for island in islands
    )
    return ImportedOutlineRegion(points=tuple(cleaned), holes=holes)


def _clean_points(points, label: str) -> list[tuple[float, float]]:
    """Validate a raw point list, dropping a repeated closing point."""

    cleaned: list[tuple[float, float]] = []
    for point in points:
        values = list(point)
        if len(values) != 2:
            raise ParameterError(f"{label}的点必须是 [x, y]（2.5D 区域是平面，不要带 Z）")
        x, y = float(values[0]), float(values[1])
        # JSON can carry NaN and infinity, and every other number in the project is checked against
        # that (ParameterSpec._coerce_number); a non-finite point would poison the whole plan.
        if not (isfinite(x) and isfinite(y)):
            raise ParameterError(f"{label}的点坐标必须是有限值")
        cleaned.append((x, y))
    if len(cleaned) < 3:
        raise ParameterError(f"{label}至少需要 3 个点")
    first, last = cleaned[0], cleaned[-1]
    if ((first[0] - last[0]) ** 2 + (first[1] - last[1]) ** 2) ** 0.5 <= 1e-6:
        cleaned.pop()
    return cleaned


def build_region(shape_id: str, raw_parameters: Mapping[str, Any] | None = None) -> RegionShape:
    """Build a registered region shape from API parameters."""

    cls = REGION_SHAPES.get(shape_id)
    return cls(**cls.parameters.coerce(raw_parameters))


def region_catalog() -> list[dict[str, Any]]:
    return REGION_SHAPES.catalog()
