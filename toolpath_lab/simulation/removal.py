"""材料切除仿真（新增功能）。

刀路是"走一刀"，加工结果是"留下一块料"。这个模块把刀路真正走一遍，算出**加工后还剩什么**，
于是"这条刀路好不好"第一次有了可量化的依据，而不是只看长度和时间：

- 建一张高度场（Z-map）：每个网格单元记录"目标面之上还剩下多少材料"，
  初始值 = 轴向切深 ap（即需要去除的一层）；
- 逐段扫掠切削运动：按刀尖形状（平底/球头/圆鼻）与轴向切深算出**实际咬入半径**，
  被咬入的网格直接归零——材料沿刀具包络被切掉；
- 统计残余面积、残余体积、最大残余高度、区域外切出面积与覆盖率。

模型与假设（写清楚比"看起来像"更重要）
--------------------------------------
1. 加工面是平面（与本工程 2.D 的栅格/环切刀路一致），目标面取 Z = 0，毛坯高 aр；
2. 刀具沿刀路平移，切削包络取"刀具下沉 ap 后的水平截面圆"，不建模进给方向的偏摆、
   刀具跳动与让刀（这些是五轴与力学的范畴，留给后续扩展）；
3. 网格分辨率默认 0.5 mm：既能让残余脊线可见，又能把一次仿真的耗时控制在百毫秒级。

残余脊线就是这么来的：两条相邻刀线之间，只要切宽大于刀具的实际咬入直径，中间就会
留下一条没切到的高度带——把切宽调小、或换用平底刀，脊线立刻变窄，这正是仿真的意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.geometry2d import ensure_ccw, points_inside_polygon

#: 高度小于该值（mm）就认为该处材料已经切净。
CLEAN_TOLERANCE_MM = 1e-6
#: 打包成 JSON 时高度场的最大边长（超过就做块平均降采样）。
PAYLOAD_MAX_CELLS = 48


@dataclass(frozen=True, slots=True)
class RemovalReport:
    """一次材料切除仿真的结果。"""

    resolution_mm: float
    bounds: tuple[float, float, float, float]
    heights: NDArray[np.float64]
    inside: NDArray[np.bool_]
    metrics: dict[str, float]

    @property
    def grid_shape(self) -> tuple[int, int]:
        return (int(self.heights.shape[0]), int(self.heights.shape[1]))

    def to_payload(self, *, max_cells: int = PAYLOAD_MAX_CELLS) -> dict[str, Any]:
        """给接口/界面用的紧凑结果：降采样后的高度场 + 全部指标。"""

        heights = self.heights
        inside = self.inside
        ny, nx = heights.shape
        step = max(1, int(ceil(max(ny, nx) / max(1, max_cells))))
        if step > 1:
            rows = ny // step * step
            cols = nx // step * step
            heights = heights[:rows, :cols].reshape(rows // step, step, cols // step, step).mean(axis=(1, 3))
            inside = inside[:rows, :cols].reshape(rows // step, step, cols // step, step).all(axis=(1, 3))
        return {
            "resolution_mm": round(self.resolution_mm, 4),
            "downsample_step": step,
            "bounds": [round(float(value), 4) for value in self.bounds],
            "heights": [[round(float(value), 4) for value in row] for row in heights],
            "inside": [[bool(value) for value in row] for row in inside],
            "metrics": {key: round(float(value), 6) for key, value in self.metrics.items()},
        }


def _cutting_samples(points: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """把一段折线按最大弦长 step_mm 离散成采样点（含两端）。"""

    planar = np.asarray(points, dtype=np.float64).reshape(-1, 3)[:, :2]
    if planar.shape[0] < 2:
        return planar
    steps = np.linalg.norm(np.diff(planar, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(steps)))
    total = float(cumulative[-1])
    if total <= 1e-9:
        return planar[:1]
    count = max(1, int(ceil(total / max(step_mm, 1e-6))))
    targets = np.linspace(0.0, total, count + 1)
    return np.column_stack(
        [np.interp(targets, cumulative, planar[:, axis]) for axis in range(2)]
    )


def simulate_removal(
    toolpath: Toolpath,
    tool: Tool,
    region: RegionShape,
    *,
    resolution_mm: float = 0.5,
    axial_depth_mm: float = 1.0,
    margin_mm: float | None = None,
) -> RemovalReport:
    """走一遍刀路，返回高度场与残余统计。

    参数
    ----
    resolution_mm   高度场网格边长（越小越精细、越慢）
    axial_depth_mm  轴向切深 ap：既是要去除的层厚，也是决定刀具咬入半径的量
    margin_mm       仿真窗口相对区域外扩的距离（默认取刀具半径，用于统计切出区域外的面积）
    """

    if resolution_mm <= 0:
        raise ParameterError("仿真分辨率必须是正数")
    if axial_depth_mm <= 0:
        raise ParameterError("轴向切深必须是正数")

    boundary = ensure_ccw(region.boundary())
    reach = tool.cutting_footprint_radius_mm(axial_depth_mm)
    margin = tool.radius_mm if margin_mm is None else max(0.0, margin_mm)

    x_min = float(boundary[:, 0].min()) - margin
    x_max = float(boundary[:, 0].max()) + margin
    y_min = float(boundary[:, 1].min()) - margin
    y_max = float(boundary[:, 1].max()) + margin
    nx = max(2, int(ceil((x_max - x_min) / resolution_mm)))
    ny = max(2, int(ceil((y_max - y_min) / resolution_mm)))
    xs = x_min + (np.arange(nx, dtype=np.float64) + 0.5) * resolution_mm
    ys = y_min + (np.arange(ny, dtype=np.float64) + 0.5) * resolution_mm
    grid_x, grid_y = np.meshgrid(xs, ys)

    inside = points_inside_polygon(
        np.column_stack((grid_x.ravel(), grid_y.ravel())), boundary
    ).reshape(ny, nx)

    heights = np.full((ny, nx), float(axial_depth_mm), dtype=np.float64)
    # 采样间隔同时受网格与咬入半径约束：太稀疏会在扫掠方向留下假的"锯齿"。
    sample_step = min(resolution_mm, max(reach * 0.5, 1e-3))

    cell = resolution_mm
    for move in toolpath.moves:
        if not move.is_cutting or reach <= 0.0:
            continue
        for px, py in _cutting_samples(move.points, sample_step):
            i0 = max(0, int(np.floor((px - reach - x_min) / cell - 0.5)))
            i1 = min(nx - 1, int(np.ceil((px + reach - x_min) / cell - 0.5)))
            j0 = max(0, int(np.floor((py - reach - y_min) / cell - 0.5)))
            j1 = min(ny - 1, int(np.ceil((py + reach - y_min) / cell - 0.5)))
            if i1 < i0 or j1 < j0:
                continue
            window_x = grid_x[j0:j1 + 1, i0:i1 + 1] - px
            window_y = grid_y[j0:j1 + 1, i0:i1 + 1] - py
            hit = (window_x * window_x + window_y * window_y) <= reach * reach
            block = heights[j0:j1 + 1, i0:i1 + 1]
            np.minimum(block, np.where(hit, 0.0, block), out=block)

    cell_area = resolution_mm * resolution_mm
    cleaned = heights <= CLEAN_TOLERANCE_MM
    inside_area = float(inside.sum()) * cell_area
    region_volume = inside_area * float(axial_depth_mm)
    residual_heights = heights[inside]
    residual_volume = float(residual_heights.sum()) * cell_area

    metrics = {
        "region_area_mm2": inside_area,
        "cutting_footprint_radius_mm": reach,
        "grid_cells": float(nx * ny),
        "cleaned_area_mm2": float((cleaned & inside).sum()) * cell_area,
        "coverage_ratio": float((cleaned & inside).sum()) / max(1.0, float(inside.sum())),
        "residual_area_mm2": float((~cleaned & inside).sum()) * cell_area,
        "residual_ratio": float((~cleaned & inside).sum()) / max(1.0, float(inside.sum())),
        "residual_volume_mm3": residual_volume,
        "material_removal_ratio": 1.0 - residual_volume / max(1e-9, region_volume),
        "max_residual_mm": float(residual_heights.max()) if residual_heights.size else 0.0,
        "mean_residual_mm": float(residual_heights.mean()) if residual_heights.size else 0.0,
        "overcut_outside_area_mm2": float((cleaned & ~inside).sum()) * cell_area,
    }
    return RemovalReport(
        resolution_mm=float(resolution_mm),
        bounds=(x_min, x_max, y_min, y_max),
        heights=heights,
        inside=inside,
        metrics=metrics,
    )
