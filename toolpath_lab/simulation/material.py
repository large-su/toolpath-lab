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

    def cut_oriented_flat_tool(
        self,
        position: NDArray[np.float64],
        radius_mm: float,
        flute_length_mm: float,
        rotary_axes_deg: NDArray[np.float64],
    ) -> float:
        """Remove only the stock area actually intersected by the tilted cutter face."""

        center = np.asarray(position, dtype=np.float64).reshape(-1)
        angles = np.asarray(rotary_axes_deg, dtype=np.float64).reshape(-1)
        if center.size != 3 or not np.all(np.isfinite(center)):
            raise ParameterError("刀具位置必须是三个有限坐标")
        if angles.size != 2 or not np.all(np.isfinite(angles)):
            raise ParameterError("A/B 轴姿态必须是两个有限角度")
        if not isfinite(radius_mm) or radius_mm <= 0:
            raise ParameterError("平底刀半径必须是有限正数")
        if not isfinite(flute_length_mm) or flute_length_mm <= 0:
            raise ParameterError("刀刃长度必须是有限正数")

        a_angle, b_angle = np.deg2rad(angles)
        axis = np.array(
            [
                np.sin(b_angle) * np.cos(a_angle),
                -np.sin(a_angle),
                np.cos(b_angle) * np.cos(a_angle),
            ],
            dtype=np.float64,
        )
        axis_norm = float(np.linalg.norm(axis))
        if axis_norm <= 1e-12:
            raise ParameterError("刀具轴方向不能为零向量")
        axis /= axis_norm

        # 2.5D stock only has a single height at each XY cell, so the cutter face
        # must be approximated by its contact plane. Remove stock only where the
        # tilted disk actually crosses the surface, not by treating the whole
        # cylinder as an infinite volume.
        x_lower = max(0, int(np.searchsorted(self.x_mm, center[0] - radius_mm - 1.0, side="left")))
        x_upper = min(
            self.x_mm.size,
            int(np.searchsorted(self.x_mm, center[0] + radius_mm + 1.0, side="right")),
        )
        y_lower = max(0, int(np.searchsorted(self.y_mm, center[1] - radius_mm - 1.0, side="left")))
        y_upper = min(
            self.y_mm.size,
            int(np.searchsorted(self.y_mm, center[1] + radius_mm + 1.0, side="right")),
        )
        if x_lower >= x_upper or y_lower >= y_upper:
            return 0.0

        xs = self.x_mm[x_lower:x_upper][None, :] - center[0]
        ys = self.y_mm[y_lower:y_upper][:, None] - center[1]
        if abs(axis[2]) <= 1e-9:
            # Near-vertical cutter face is not representable as a single height field.
            # Fall back to a conservative projected-disk check and leave the rest of the
            # 2.5D model unchanged.
            local_radius_sq = xs * xs + ys * ys
            affected = local_radius_sq <= radius_mm * radius_mm
            heights = self.heights_mm[y_lower:y_upper, x_lower:x_upper]
            if not np.any(affected):
                return 0.0
            contact_height = center[2]
            keep = affected & np.isfinite(heights) & (heights > contact_height)
            if not np.any(keep):
                return 0.0
            removed = np.maximum(heights[keep] - contact_height, 0.0)
            heights[keep] = np.minimum(heights[keep], contact_height)
            return float(removed.sum() * self.cell_x_mm * self.cell_y_mm)

        contact_z = center[2] - (axis[0] * xs + axis[1] * ys) / axis[2]
        dx, dy = np.broadcast_arrays(xs, ys)
        radial_xy = np.hypot(dx, dy)
        heights = self.heights_mm[y_lower:y_upper, x_lower:x_upper]
        affected = (
            np.isfinite(heights)
            & (radial_xy <= radius_mm + 1e-9)
            & (heights > contact_z)
        )
        if not np.any(affected):
            return 0.0

        new_heights = np.minimum(heights, contact_z)
        removed_depth = heights - new_heights
        heights[affected] = new_heights[affected]
        return float(removed_depth[affected].sum() * self.cell_x_mm * self.cell_y_mm)

    def cut_oriented_shaft(
        self,
        position: NDArray[np.float64],
        radius_mm: float,
        shaft_length_mm: float,
        rotary_axes_deg: NDArray[np.float64],
    ) -> float:
        """Remove stock swept by the non-cutting cylindrical shaft behind the flute."""

        center = np.asarray(position, dtype=np.float64).reshape(-1)
        angles = np.asarray(rotary_axes_deg, dtype=np.float64).reshape(-1)
        if center.size != 3 or not np.all(np.isfinite(center)):
            raise ParameterError("刀具位置必须是三个有限坐标")
        if angles.size != 2 or not np.all(np.isfinite(angles)):
            raise ParameterError("A/B 轴姿态必须是两个有限角度")
        if not isfinite(radius_mm) or radius_mm <= 0:
            raise ParameterError("刀柄半径必须是有限正数")
        if not isfinite(shaft_length_mm) or shaft_length_mm <= 0:
            raise ParameterError("刀柄长度必须是有限正数")

        a_angle, b_angle = np.deg2rad(angles)
        axis = np.array(
            [
                np.sin(b_angle) * np.cos(a_angle),
                -np.sin(a_angle),
                np.cos(b_angle) * np.cos(a_angle),
            ],
            dtype=np.float64,
        )
        axis_norm = float(np.linalg.norm(axis))
        if axis_norm <= 1e-12:
            raise ParameterError("刀具轴方向不能为零向量")
        axis /= axis_norm

        shaft_start = center - axis * shaft_length_mm
        x_lower = max(0, int(np.searchsorted(self.x_mm, center[0] - radius_mm - 1.0, side="left")))
        x_upper = min(
            self.x_mm.size,
            int(np.searchsorted(self.x_mm, center[0] + radius_mm + 1.0, side="right")),
        )
        y_lower = max(0, int(np.searchsorted(self.y_mm, center[1] - radius_mm - 1.0, side="left")))
        y_upper = min(
            self.y_mm.size,
            int(np.searchsorted(self.y_mm, center[1] + radius_mm + 1.0, side="right")),
        )
        if x_lower >= x_upper or y_lower >= y_upper:
            return 0.0

        xs = self.x_mm[x_lower:x_upper][None, :]
        ys = self.y_mm[y_lower:y_upper][:, None]
        points = np.empty((ys.shape[0], xs.shape[1], 3), dtype=np.float64)
        points[..., 0] = xs
        points[..., 1] = ys
        points[..., 2] = 0.0
        delta = points - shaft_start
        t = np.clip(np.sum(delta * axis, axis=-1), 0.0, shaft_length_mm)
        closest = shaft_start + axis * t[..., None]
        dist_sq = np.sum((points - closest) ** 2, axis=-1)
        heights = self.heights_mm[y_lower:y_upper, x_lower:x_upper]
        inside = np.isfinite(heights) & (dist_sq <= radius_mm * radius_mm + 1e-9)
        if not np.any(inside):
            return 0.0

        target_z = shaft_start[2] + axis[2] * t
        new_heights = np.minimum(heights, target_z)
        removed_depth = np.where(inside, np.maximum(heights - new_heights, 0.0), 0.0)
        heights[inside] = new_heights[inside]
        return float(removed_depth[inside].sum() * self.cell_x_mm * self.cell_y_mm)

    @staticmethod
    def _cutter_profile_segments(
        tool_kind: str,
        radius_mm: float,
        corner_radius_mm: float | None,
        flute_length_mm: float,
        resolution_mm: float,
    ) -> list[tuple[float, float, float]]:
        if tool_kind == "flat":
            return [(0.0, flute_length_mm, radius_mm)]

        if tool_kind == "ball":
            transition_length = min(radius_mm, flute_length_mm)
            step_mm = max(resolution_mm * 0.5, 0.05)
            count = max(4, int(ceil(transition_length / step_mm)))
            segments = []
            for index in range(count):
                start = transition_length * index / count
                end = transition_length * (index + 1) / count
                profile_radius = np.sqrt(max(0.0, 2.0 * radius_mm * end - end**2))
                if profile_radius > 1e-9:
                    segments.append((start, end, float(profile_radius)))
            if flute_length_mm > radius_mm:
                segments.append((radius_mm, flute_length_mm, radius_mm))
            return segments

        if tool_kind == "bull":
            corner = corner_radius_mm
            if corner is None or not isfinite(corner) or corner <= 0 or corner > radius_mm:
                raise ParameterError("圆鼻刀圆角半径必须大于 0 且不大于刀具半径")
            transition_length = min(corner, flute_length_mm)
            step_mm = max(resolution_mm * 0.25, 0.025)
            count = max(4, int(ceil(transition_length / step_mm)))
            flat_radius = radius_mm - corner
            segments = []
            for index in range(count):
                start = transition_length * index / count
                end = transition_length * (index + 1) / count
                profile_radius = flat_radius + np.sqrt(
                    max(0.0, 2.0 * corner * end - end**2)
                )
                if profile_radius > 1e-9:
                    segments.append((start, end, float(profile_radius)))
            if flute_length_mm > corner:
                segments.append((corner, flute_length_mm, radius_mm))
            return segments

        raise ParameterError(f"不支持的刀具类型：{tool_kind}")

    def cut_oriented_tool(
        self,
        position: NDArray[np.float64],
        radius_mm: float,
        flute_length_mm: float,
        rotary_axes_deg: NDArray[np.float64],
        *,
        tool_kind: str = "flat",
        corner_radius_mm: float | None = None,
    ) -> float:
        """Remove stock intersected by a tilted flat, ball, or bull-nose flute."""

        center = np.asarray(position, dtype=np.float64).reshape(-1)
        if center.size != 3 or not np.all(np.isfinite(center)):
            raise ParameterError("刀具位置必须是三个有限坐标")
        angles = np.asarray(rotary_axes_deg, dtype=np.float64).reshape(-1)
        if angles.size != 2 or not np.all(np.isfinite(angles)):
            raise ParameterError("A/B 轴姿态必须是两个有限角度")
        if not isfinite(radius_mm) or radius_mm <= 0:
            raise ParameterError("刀具半径必须是有限正数")
        if not isfinite(flute_length_mm) or flute_length_mm <= 0:
            raise ParameterError("刀刃长度必须是有限正数")
        segments = self._cutter_profile_segments(
            tool_kind,
            radius_mm,
            corner_radius_mm,
            flute_length_mm,
            self.resolution_mm,
        )
        a_angle, b_angle = np.deg2rad(angles)
        axis = np.array(
            [
                np.sin(b_angle) * np.cos(a_angle),
                -np.sin(a_angle),
                np.cos(b_angle) * np.cos(a_angle),
            ],
            dtype=np.float64,
        )
        removed_volume = 0.0
        for start, end, segment_radius in reversed(segments):
            segment_position = center + axis * start
            removed_volume += self.cut_oriented_flat_tool(
                segment_position,
                segment_radius,
                end - start,
                angles,
            )

        shaft_length = max(flute_length_mm * 2.0, radius_mm * 8.0)
        removed_volume += self.cut_oriented_shaft(
            center,
            radius_mm,
            shaft_length,
            angles,
        )
        return removed_volume

    def simulate_toolpath(
        self,
        toolpath: Toolpath,
        radius_mm: float,
        *,
        flute_length_mm: float | None = None,
        tool_kind: str = "flat",
        corner_radius_mm: float | None = None,
    ) -> float:
        """Simulate exposed-surface removal by the selected oriented cutter flute."""

        removed_volume = 0.0
        sample_step = self.resolution_mm * 0.5
        flute_length = (
            float(flute_length_mm)
            if flute_length_mm is not None
            else self.top_z_mm - self.bottom_z_mm
        )
        for move in toolpath.moves:
            if move.kind is MoveKind.RAPID:
                continue
            for segment_index, (start, end) in enumerate(
                zip(move.points[:-1], move.points[1:])
            ):
                distance = float(np.linalg.norm(end - start))
                if move.rotary_axes is None:
                    start_angles = end_angles = np.zeros(2)
                else:
                    start_angles = move.rotary_axes[segment_index]
                    end_angles = move.rotary_axes[segment_index + 1]
                angle_delta = float(np.max(np.abs(end_angles - start_angles)))
                steps = max(
                    1,
                    int(ceil(distance / sample_step)),
                    int(ceil(angle_delta / 1.0)),
                )
                for index in range(steps + 1):
                    ratio = index / steps
                    position = start + (end - start) * ratio
                    angles = start_angles + (end_angles - start_angles) * ratio
                    removed_volume += self.cut_oriented_tool(
                        position,
                        radius_mm,
                        flute_length,
                        angles,
                        tool_kind=tool_kind,
                        corner_radius_mm=corner_radius_mm,
                    )
        return removed_volume
