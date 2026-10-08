"""材料切除仿真的毛坯规格与高度场状态。

这里采用规则 XY 网格保存毛坯顶部高度。它比真正的三角网格扫掠体
简单，但能清楚演示“播放刀路 -> 材料逐步减少”的过程，也便于在前端
按时间轴回退和重放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.region import RegionShape, polygon_bounds
from toolpath_lab.core.surface import SurfaceShape
from toolpath_lab.core.tool import Tool


@dataclass(frozen=True, slots=True)
class StockSpec:
    """前端材料切除仿真所需的毛坯规格。"""

    bounds_mm: tuple[tuple[float, float], tuple[float, float]]
    boundary: tuple[tuple[float, float], ...]
    bottom_z_mm: float
    initial_top_z_mm: float
    resolution_mm: float
    tool_radius_mm: float

    @property
    def grid_shape(self) -> tuple[int, int]:
        width = self.bounds_mm[0][1] - self.bounds_mm[0][0]
        height = self.bounds_mm[1][1] - self.bounds_mm[1][0]
        return (
            int(np.ceil(width / self.resolution_mm)) + 1,
            int(np.ceil(height / self.resolution_mm)) + 1,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "bounds_mm": [list(axis) for axis in self.bounds_mm],
            "boundary": [list(point) for point in self.boundary],
            "bottom_z_mm": self.bottom_z_mm,
            "initial_top_z_mm": self.initial_top_z_mm,
            "resolution_mm": self.resolution_mm,
            "tool_radius_mm": self.tool_radius_mm,
            "grid_shape": list(self.grid_shape),
        }


def stock_spec_for(
    region: RegionShape,
    surface: SurfaceShape,
    tool: Tool,
    *,
    resolution_mm: float = 2.0,
    top_allowance_mm: float = 1.0,
    bottom_allowance_mm: float = 4.0,
) -> StockSpec:
    """根据当前区域、曲面和刀具生成教学仿真的毛坯规格。

    毛坯顶部只保留一层薄余量，避免五轴倾斜刀具在曲面起伏上方
    穿入过厚的透明毛坯；底部余量用于显示切削后的承托层。
    """

    if resolution_mm <= 0:
        raise ValueError("毛坯网格分辨率必须为正")
    boundary = region.boundary()
    bounds = polygon_bounds(boundary)
    lower, upper = surface.height_bounds()
    top = upper + max(float(top_allowance_mm), 0.5)
    bottom = lower - max(float(bottom_allowance_mm), 0.5)
    return StockSpec(
        bounds_mm=(tuple(bounds[0]), tuple(bounds[1])),
        boundary=tuple((float(point[0]), float(point[1])) for point in boundary),
        bottom_z_mm=float(bottom),
        initial_top_z_mm=float(top),
        resolution_mm=float(resolution_mm),
        tool_radius_mm=float(tool.radius_mm),
    )


class StockState:
    """可回放的高度场毛坯状态，刀具点会降低其圆形足迹内的高度。"""

    def __init__(self, spec: StockSpec) -> None:
        self.spec = spec
        nx, ny = spec.grid_shape
        xs = np.linspace(spec.bounds_mm[0][0], spec.bounds_mm[0][1], nx)
        ys = np.linspace(spec.bounds_mm[1][0], spec.bounds_mm[1][1], ny)
        self.xs, self.ys = xs, ys
        self.xx, self.yy = np.meshgrid(xs, ys)
        self.heights = np.full((ny, nx), spec.initial_top_z_mm, dtype=np.float64)
        self.active = _inside_grid(xs, ys, spec.boundary)
        self.heights[~self.active] = spec.bottom_z_mm

    def reset(self) -> None:
        self.heights.fill(self.spec.initial_top_z_mm)
        self.heights[~self.active] = self.spec.bottom_z_mm

    def remove_tool_point(
        self,
        point_xyz: NDArray[np.float64] | list[float] | tuple[float, float, float],
        *,
        radius_mm: float | None = None,
    ) -> None:
        """按刀具圆形足迹切除一个刀位处的材料。"""

        x, y, z = (float(value) for value in point_xyz)
        radius = self.spec.tool_radius_mm if radius_mm is None else float(radius_mm)
        if radius <= 0:
            return
        mask = self.active & ((self.xx - x) ** 2 + (self.yy - y) ** 2 <= radius * radius)
        self.heights[mask] = np.minimum(self.heights[mask], max(z, self.spec.bottom_z_mm))

    def remove_tool_segment(
        self,
        start_xyz: NDArray[np.float64] | list[float] | tuple[float, float, float],
        end_xyz: NDArray[np.float64] | list[float] | tuple[float, float, float],
        *,
        radius_mm: float | None = None,
    ) -> None:
        """按圆形刀具扫掠一条直线段，降低线段圆柱扫掠范围内的毛坯。"""

        start = np.asarray(start_xyz, dtype=np.float64).reshape(3)
        end = np.asarray(end_xyz, dtype=np.float64).reshape(3)
        radius = self.spec.tool_radius_mm if radius_mm is None else float(radius_mm)
        if radius <= 0:
            return
        delta = end - start
        length_squared = float(np.dot(delta[:2], delta[:2]))
        if length_squared <= 1e-12:
            self.remove_tool_point(end, radius_mm=radius)
            return
        t = ((self.xx - start[0]) * delta[0] + (self.yy - start[1]) * delta[1])
        t = np.clip(t / length_squared, 0.0, 1.0)
        closest_x = start[0] + t * delta[0]
        closest_y = start[1] + t * delta[1]
        distance_squared = (self.xx - closest_x) ** 2 + (self.yy - closest_y) ** 2
        target_z = start[2] + t * delta[2]
        mask = self.active & (distance_squared <= radius * radius)
        target_z = np.maximum(target_z, self.spec.bottom_z_mm)
        mask &= target_z < self.heights
        self.heights[mask] = target_z[mask]

    def remaining_volume_mm3(self) -> float:
        cell_area = self.spec.resolution_mm * self.spec.resolution_mm
        return float(np.maximum(self.heights - self.spec.bottom_z_mm, 0.0).sum() * cell_area)


def _inside_grid(
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
    boundary: tuple[tuple[float, float], ...],
) -> NDArray[np.bool_]:
    """生成网格内区域掩码。"""

    xx, yy = np.meshgrid(xs, ys)
    points = np.column_stack((xx.ravel(), yy.ravel()))
    polygon = np.asarray(boundary, dtype=np.float64)
    inside = np.zeros(points.shape[0], dtype=bool)
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        crosses = (y1 > points[:, 1]) != (y2 > points[:, 1])
        denominator = y2 - y1
        safe = np.where(np.abs(denominator) > 1e-12, denominator, 1.0)
        crossing_x = (x2 - x1) * (points[:, 1] - y1) / safe + x1
        inside ^= crosses & (points[:, 0] < crossing_x)
        previous = current
    return inside.reshape(yy.shape)


__all__ = ["StockSpec", "StockState", "stock_spec_for"]
