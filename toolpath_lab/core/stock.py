"""毛坯（stock）。

毛坯只有两种形状，但它是"零件 → 加工"之间唯一必须由人确认的环节，因此这里做三件事：

1. **包容盒计算**：默认按零件包围盒外扩给定的 X/Y/Z 偏移量，向上留出的余量就是端面余量；
2. **坐标定位**：毛坯底面固定在 Z = 0，XY 与零件同心——与导入后的零件坐标系一致，
   后续刀路、仿真不需要再做任何坐标换算；
3. **网格生成**：给界面预览与实时切削仿真一块闭合的三角网格。

圆柱毛坯按"能包住零件的直径"取，可取零件 XY 尺寸的对角线，也允许手动指定直径。

参数全部走 :class:`~toolpath_lab.core.parameters.ParameterSpec` 声明，
界面控件与接口校验因此共用同一份定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import pi
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.part import PartBounds, PartModel
from toolpath_lab.core.registry import Registry

STOCK_TYPES: Registry[type["StockShape"]] = Registry("stock shape")

#: 圆柱毛坯预览与仿真用的圆周分段数（够平滑，又不至于让网格过大）。
CYLINDER_SEGMENTS = 96


@dataclass(frozen=True, slots=True)
class Mesh:
    """一块用于渲染/仿真的三角网格。"""

    positions: NDArray[np.float64]
    indices: NDArray[np.int64]

    @property
    def triangle_count(self) -> int:
        return int(self.indices.shape[0])

    def bounds(self) -> PartBounds:
        low = self.positions.min(axis=0)
        high = self.positions.max(axis=0)
        return PartBounds.from_array(low, high)

    def to_payload(self, *, decimals: int = 4) -> dict[str, Any]:
        return {
            "positions": [[round(float(value), decimals) for value in row] for row in self.positions],
            "indices": self.indices.reshape(-1).astype(int).tolist(),
        }


@dataclass(frozen=True, slots=True)
class Stock:
    """一块毛坯。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()

    bounds: PartBounds

    def to_params(self) -> dict[str, Any]:
        return {}

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "parameters": self.to_params(),
            "bounds": self.bounds.to_payload(),
            "volume_mm3": round(self.volume_mm3(), 3),
        }

    def volume_mm3(self) -> float:  # pragma: no cover - 抽象
        raise NotImplementedError

    def build_mesh(self, *, segments: int = 72) -> Mesh:  # pragma: no cover - 抽象
        raise NotImplementedError

    def residual_mm(self, part: PartModel) -> tuple[float, float, float]:
        """毛坯相对零件在 X/Y/Z 上的单边余量（供界面显示与实际切削量参考）。"""

        size = self.bounds.size
        part_size = part.size
        return (
            max(0.0, 0.5 * (size[0] - part_size[0])),
            max(0.0, 0.5 * (size[1] - part_size[1])),
            max(0.0, size[2] - part_size[2]),
        )


def _box_mesh(bounds: PartBounds) -> Mesh:
    """长方体网格：8 个顶点、12 个三角形，法向朝外。"""

    x0, x1 = bounds.x_min, bounds.x_max
    y0, y1 = bounds.y_min, bounds.y_max
    z0, z1 = bounds.z_min, bounds.z_max
    positions = np.array(
        [
            [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
            [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
        ],
        dtype=np.float64,
    )
    # 每个面两个三角形，按逆时针（朝外）排列
    faces = [
        (0, 3, 2), (0, 2, 1),          # 底 -Z
        (4, 5, 6), (4, 6, 7),          # 顶 +Z
        (0, 1, 5), (0, 5, 4),          # 前 -Y
        (2, 3, 7), (2, 7, 6),          # 后 +Y
        (1, 2, 6), (1, 6, 5),          # 右 +X
        (3, 0, 4), (3, 4, 7),          # 左 -X
    ]
    return Mesh(positions, np.asarray(faces, dtype=np.int64))


@STOCK_TYPES.register
@dataclass(frozen=True, slots=True)
class RectangularStock(Stock):
    """矩形块毛坯。"""

    offset_x_mm: float = 2.0
    offset_y_mm: float = 2.0
    offset_z_mm: float = 1.0

    id: ClassVar[str] = "rectangular"
    label: ClassVar[str] = "矩形块"
    description: ClassVar[str] = "按零件包容盒各轴向外偏移，最常见的板料毛坯"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("offset_x_mm", "X 向偏移", K.FLOAT, 2.0, minimum=0.0, maximum=200.0,
                 step=0.5, unit="mm", group="毛坯", help="左右各向外加多少余量"),
            spec("offset_y_mm", "Y 向偏移", K.FLOAT, 2.0, minimum=0.0, maximum=200.0,
                 step=0.5, unit="mm", group="毛坯", help="前后各向外加多少余量"),
            spec("offset_z_mm", "Z 向余量", K.FLOAT, 1.0, minimum=0.0, maximum=200.0,
                 step=0.5, unit="mm", group="毛坯", help="顶面留出的加工余量，底面保持齐平"),
        )
    )

    def to_params(self) -> dict[str, Any]:
        return {
            "offset_x_mm": self.offset_x_mm,
            "offset_y_mm": self.offset_y_mm,
            "offset_z_mm": self.offset_z_mm,
        }

    def volume_mm3(self) -> float:
        size = self.bounds.size
        return float(size[0] * size[1] * size[2])

    def build_mesh(self, *, segments: int = 72) -> Mesh:
        return _box_mesh(self.bounds)


@STOCK_TYPES.register
@dataclass(frozen=True, slots=True)
class CylindricalStock(Stock):
    """圆柱毛坯。"""

    offset_radial_mm: float = 2.0
    offset_z_mm: float = 1.0
    #: 直径为 0 时按"包住零件 XY 尺寸的圆"自动取。
    diameter_mm: float = 0.0
    segments: int = CYLINDER_SEGMENTS

    id: ClassVar[str] = "cylindrical"
    label: ClassVar[str] = "圆柱形"
    description: ClassVar[str] = "回转体零件常用；直径可自动取包容圆，也可手动指定"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("diameter_mm", "直径 D", K.FLOAT, 0.0, minimum=0.0, maximum=2000.0,
                 step=1.0, unit="mm", group="毛坯", help="0 表示自动取零件的包容圆直径"),
            spec("offset_radial_mm", "径向偏移", K.FLOAT, 2.0, minimum=0.0, maximum=200.0,
                 step=0.5, unit="mm", group="毛坯"),
            spec("offset_z_mm", "Z 向余量", K.FLOAT, 1.0, minimum=0.0, maximum=200.0,
                 step=0.5, unit="mm", group="毛坯"),
        )
    )

    def to_params(self) -> dict[str, Any]:
        return {
            "diameter_mm": self.diameter_mm,
            "offset_radial_mm": self.offset_radial_mm,
            "offset_z_mm": self.offset_z_mm,
        }

    def volume_mm3(self) -> float:
        radius = 0.5 * self.bounds.size[0]
        return float(pi * radius * radius * self.bounds.size[2])

    def build_mesh(self, *, segments: int = 64) -> Mesh:
        bounds = self.bounds
        radius = 0.5 * (bounds.x_max - bounds.x_min)
        center = bounds.center
        count = max(8, int(segments or self.segments))
        angles = np.linspace(0.0, 2.0 * pi, count, endpoint=False)
        ring = np.column_stack((np.cos(angles), np.sin(angles))) * radius
        low = np.column_stack((ring, np.full(count, bounds.z_min)))
        high = np.column_stack((ring, np.full(count, bounds.z_max)))
        positions = np.vstack([
            low, high,
            np.array([[center[0], center[1], bounds.z_min]]),
            np.array([[center[0], center[1], bounds.z_max]]),
        ])
        bottom_center = 2 * count
        top_center = bottom_center + 1
        indices: list[tuple[int, int, int]] = []
        for index in range(count):
            nxt = (index + 1) % count
            # 侧面（逆时针，法向朝外）
            indices.append((index, nxt, count + nxt))
            indices.append((index, count + nxt, count + index))
            # 底面（法向 -Z）与顶面（法向 +Z）
            indices.append((bottom_center, nxt, index))
            indices.append((top_center, count + index, count + nxt))
        return Mesh(positions, np.asarray(indices, dtype=np.int64))


def build_stock(shape_id: str, part: PartModel,
                raw_parameters: Mapping[str, Any] | None = None) -> Stock:
    """按零件与参数构造毛坯。"""

    cls = STOCK_TYPES.get(shape_id)
    values = cls.parameters.coerce(raw_parameters)
    bounds = part.bounds
    if cls is RectangularStock:
        dx = float(values["offset_x_mm"])
        dy = float(values["offset_y_mm"])
        dz = float(values["offset_z_mm"])
        return RectangularStock(
            bounds=PartBounds(bounds.x_min - dx, bounds.y_min - dy, bounds.z_min,
                              bounds.x_max + dx, bounds.y_max + dy, bounds.z_max + dz),
            **values,
        )
    if cls is CylindricalStock:
        radial = float(values["offset_radial_mm"])
        dz = float(values["offset_z_mm"])
        diameter = float(values["diameter_mm"])
        if diameter <= 0.0:
            # 自动：包容零件 XY 尺寸的最小圆
            diagonal = float(np.hypot(bounds.size[0], bounds.size[1]))
            diameter = diagonal + 2.0 * radial
        if diameter <= 0.0:
            raise ParameterError("圆柱毛坯直径必须为正")
        radius = 0.5 * diameter
        center_x, center_y = 0.5 * (bounds.x_min + bounds.x_max), 0.5 * (bounds.y_min + bounds.y_max)
        return CylindricalStock(
            bounds=PartBounds(center_x - radius, center_y - radius, bounds.z_min,
                              center_x + radius, center_y + radius, bounds.z_max + dz),
            **values,
        )
    raise ParameterError(f"未知的毛坯类型 {shape_id!r}")


def stock_catalog() -> list[dict[str, Any]]:
    return STOCK_TYPES.catalog()


__all__ = [
    "CYLINDER_SEGMENTS",
    "CylindricalStock",
    "Mesh",
    "RectangularStock",
    "STOCK_TYPES",
    "Stock",
    "build_stock",
    "stock_catalog",
]
