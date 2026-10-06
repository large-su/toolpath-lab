"""2.5D stock-removal simulation using a sampled height field."""

from __future__ import annotations

from math import ceil, isfinite

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import MoveKind, Toolpath


class HeightField:
    """A polygonal stock surface represented by a regular XY grid."""

    def __init__(
        self,
        boundary: NDArray[np.float64],
        *,
        resolution_mm: float = 1.0,
        top_z_mm: float = 2.0,
        bottom_z_mm: float = -20.0,
        max_cells: int = 250_000,
    ) -> None:
        polygon = np.asarray(boundary, dtype=np.float64)
        if polygon.ndim != 2 or polygon.shape[0] < 3 or polygon.shape[1] != 2:
            raise ParameterError("毛坯边界必须是至少包含三个二维点的多边形")
        if not np.all(np.isfinite(polygon)):
            raise ParameterError("毛坯边界坐标必须都是有限值")
        if not isfinite(resolution_mm) or resolution_mm <= 0:
            raise ParameterError("仿真网格分辨率必须是有限正数")
        if not isfinite(top_z_mm) or not isfinite(bottom_z_mm) or bottom_z_mm >= top_z_mm:
            raise ParameterError("毛坯底面高度必须低于顶面高度")
        if max_cells < 4:
            raise ParameterError("仿真网格至少需要四个单元")

        self.boundary = polygon.copy()
        self.top_z_mm = float(top_z_mm)
        self.bottom_z_mm = float(bottom_z_mm)
        x_min, y_min = polygon.min(axis=0)
        x_max, y_max = polygon.max(axis=0)
        span_x = float(x_max - x_min)
        span_y = float(y_max - y_min)
        if span_x <= 0 or span_y <= 0:
            raise ParameterError("毛坯边界必须具有正面积")

        spacing = float(resolution_mm)
        while True:
            columns = max(2, int(ceil(span_x / spacing)) + 1)
            rows = max(2, int(ceil(span_y / spacing)) + 1)
            if columns * rows <= max_cells:
                break
            spacing *= max(1.01, (columns * rows / max_cells) ** 0.5)

        self.x_mm = np.linspace(float(x_min), float(x_max), columns)
        self.y_mm = np.linspace(float(y_min), float(y_max), rows)
        self.cell_x_mm = float(self.x_mm[1] - self.x_mm[0])
        self.cell_y_mm = float(self.y_mm[1] - self.y_mm[0])
        self.resolution_mm = max(self.cell_x_mm, self.cell_y_mm)
        self.inside = self._points_inside_polygon()
        self.heights_mm = np.full((rows, columns), np.nan, dtype=np.float64)
        self.heights_mm[self.inside] = self.top_z_mm

    def _points_inside_polygon(self) -> NDArray[np.bool_]:
        xx, yy = np.meshgrid(self.x_mm, self.y_mm)
        inside = np.zeros(xx.shape, dtype=np.bool_)
        x0, y0 = self.boundary[-1]
        for x1, y1 in self.boundary:
            crosses = (y1 > yy) != (y0 > yy)
            intersection_x = (x0 - x1) * (yy - y1) / (
                y0 - y1 if abs(y0 - y1) > 1e-12 else 1e-12
            ) + x1
            inside ^= crosses & (xx < intersection_x)
            x0, y0 = x1, y1
        return inside

    def reset(self) -> None:
        """Restore the untouched stock top."""

        self.heights_mm.fill(np.nan)
        self.heights_mm[self.inside] = self.top_z_mm

    def cut_flat_tool(self, position: NDArray[np.float64], radius_mm: float) -> float:
        """Cut the stock below a flat tool tip and return removed volume in mm³."""

        center = np.asarray(position, dtype=np.float64).reshape(-1)
        if center.size != 3 or not np.all(np.isfinite(center)):
            raise ParameterError("刀具位置必须是三个有限坐标")
        if not isfinite(radius_mm) or radius_mm <= 0:
            raise ParameterError("平底刀半径必须是有限正数")

        cut_z = max(float(center[2]), self.bottom_z_mm)
        if cut_z >= self.top_z_mm:
            return 0.0

        column_start = max(0, int(np.searchsorted(self.x_mm, center[0] - radius_mm, side="left")))
        column_stop = min(
            self.x_mm.size,
            int(np.searchsorted(self.x_mm, center[0] + radius_mm, side="right")),
        )
        row_start = max(0, int(np.searchsorted(self.y_mm, center[1] - radius_mm, side="left")))
        row_stop = min(
            self.y_mm.size,
            int(np.searchsorted(self.y_mm, center[1] + radius_mm, side="right")),
        )
        if row_start >= row_stop or column_start >= column_stop:
            return 0.0

        x = self.x_mm[column_start:column_stop]
        y = self.y_mm[row_start:row_stop]
        within_tool = (
            (x[None, :] - center[0]) ** 2 + (y[:, None] - center[1]) ** 2
            <= radius_mm**2
        )
        heights = self.heights_mm[row_start:row_stop, column_start:column_stop]
        affected = within_tool & np.isfinite(heights)
        removed_depth = np.where(affected, np.maximum(heights - cut_z, 0.0), 0.0)
        heights[affected] -= removed_depth[affected]
        return float(removed_depth.sum() * self.cell_x_mm * self.cell_y_mm)

    def simulate_toolpath(self, toolpath: Toolpath, radius_mm: float) -> float:
        """Apply all cutting/link moves, ignoring rapid moves, and return removed mm³."""

        removed_volume = 0.0
        sample_step = self.resolution_mm * 0.5
        for move in toolpath.moves:
            if move.kind is MoveKind.RAPID:
                continue
            for start, end in zip(move.points[:-1], move.points[1:]):
                distance = float(np.linalg.norm(end - start))
                steps = max(1, int(ceil(distance / sample_step)))
                for index in range(steps + 1):
                    position = start + (end - start) * (index / steps)
                    removed_volume += self.cut_flat_tool(position, radius_mm)
        return removed_volume
