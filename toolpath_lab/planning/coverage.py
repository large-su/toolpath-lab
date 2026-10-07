"""刀路覆盖率分析：这条刀路把区域切干净了没有。

平面加工最容易出问题的往往不是"路径形状对不对"，而是"有没有漏掉一块"：切宽大于刀具直径会
留下残余条带，边界处理选成"贴轮廓"会切出区域，环切在窄区域里可能剩中心一块没走到。
这里把"刀具扫过的面积"和"区域面积"比一遍，给出未切除面积、比例，以及未切除连通块的位置。

做法：在区域包围盒上布一层网格，逐格判断

1. 格心是否在区域内——复用栅格刀路那套扫描线区间；
2. 格心到任意一段**去材料**的运动（切削 / 连接，快移不算）的距离是否 <= 刀具足迹半径。

刀具按足迹半径当作圆盘：平底刀在平面上的扫掠就是这样（球头 / 圆鼻刀将来启用时，
这里的半径要跟着改）。加工面固定是 Z = 0，所以只算平面距离。

放在 planning 层是因为它和策略共用同一套平面几何（`geometry2d`），而分层约定要求
`planning` / `simulation` / `export` 只依赖 `core`——把几何再抄一份到别的层并不划算。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, sqrt
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.geometry2d import (
    bounding_box,
    ensure_ccw,
    scanline_intervals,
    signed_area,
)

#: 默认网格边长（mm）：太小会拖慢一次规划，太大又看不出小块漏切。
DEFAULT_CELL_MM = 0.5
#: 网格格数上限：大区域会自动放大格边长，保证一次分析的开销有界。
MAX_CELLS = 400_000
#: 最多报告几块未切除区域（其余只计入总数）。
MAX_PATCHES = 8
#: 最多输出多少个未切除矩形（给三维叠加显示用）：成片漏切通常是几个大矩形，够用了。
MAX_RECTS = 800
#: 未切除面积占比超过这个值才提醒：圆刀在方形的尖角处、曲线区域的多边形逼近处总会留一点点，
#: 那是几何必然，不该每次都弹提醒。真正要提醒的是"成片漏切"（切宽过大、环切剩中心）。
WARN_UNCUT_RATIO = 0.02


@dataclass(frozen=True, slots=True)
class UncutPatch:
    """一块没切到的区域（都是网格量级的位置，够用来定位问题）。"""

    area_mm2: float
    centre_mm: tuple[float, float]
    bounds_mm: tuple[float, float, float, float]

    def describe(self) -> dict[str, Any]:
        return {
            "area_mm2": round(self.area_mm2, 4),
            "centre_mm": [round(value, 4) for value in self.centre_mm],
            "bounds_mm": [round(value, 4) for value in self.bounds_mm],
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    """一次覆盖率分析的结果。"""

    cell_mm: float
    region_area_mm2: float
    covered_area_mm2: float
    uncut_area_mm2: float
    patch_count: int
    patches: tuple[UncutPatch, ...]
    uncut_rects: tuple[tuple[float, float, float, float], ...] = ()
    uncut_rects_truncated: bool = False

    @property
    def ratio(self) -> float:
        return 1.0 if self.region_area_mm2 <= 0.0 else self.covered_area_mm2 / self.region_area_mm2

    def describe(self) -> dict[str, Any]:
        return {
            "cell_mm": round(self.cell_mm, 4),
            "region_area_mm2": round(self.region_area_mm2, 4),
            "covered_area_mm2": round(self.covered_area_mm2, 4),
            "uncut_area_mm2": round(self.uncut_area_mm2, 4),
            "ratio": round(self.ratio, 6),
            "patch_count": self.patch_count,
            "patches": [patch.describe() for patch in self.patches],
            # 未切除格子合并成的矩形（x0, y0, x1, y1，mm），给三维叠加显示用
            "uncut_rects": [
                [round(value, 4) for value in rect] for rect in self.uncut_rects
            ],
            "uncut_rects_truncated": self.uncut_rects_truncated,
        }


def _grid_step(polygon: NDArray[np.float64], cell_mm: float) -> float:
    """按区域面积把格边长限制在合理范围：大区域自动放大，免得格数爆掉。"""

    area = max(abs(signed_area(polygon)), 1.0)
    return max(float(cell_mm), sqrt(area / MAX_CELLS))


def _cutting_segments(toolpath: Toolpath) -> list[tuple[NDArray[np.float64], NDArray[np.float64]]]:
    """去材料的运动段（切削与连接）；快移不切材料，不参与覆盖。"""

    segments: list[tuple[NDArray[np.float64], NDArray[np.float64]]] = []
    for move in toolpath.moves:
        if not move.is_cutting:
            continue
        planar = move.points[:, :2]
        for index in range(planar.shape[0] - 1):
            segments.append((planar[index], planar[index + 1]))
    return segments


def _inside_mask(
    polygon: NDArray[np.float64],
    ys: NDArray[np.float64],
    xs: NDArray[np.float64],
) -> NDArray[np.bool_]:
    """逐行用扫描线区间标记"格心落在区域内"。"""

    mask = np.zeros((ys.shape[0], xs.shape[0]), dtype=bool)
    for row, y in enumerate(ys):
        for interval in scanline_intervals(polygon, float(y)):
            mask[row] |= (xs >= interval.start) & (xs <= interval.end)
    return mask


def _covered_mask(
    segments: list[tuple[NDArray[np.float64], NDArray[np.float64]]],
    radius: float,
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
    cell_mm: float,
) -> NDArray[np.bool_]:
    """刀具扫过的格子：每段运动只处理它外扩一个半径后的那小块网格。"""

    covered = np.zeros((ys.shape[0], xs.shape[0]), dtype=bool)
    if radius <= 0.0 or not segments:
        return covered
    for start, end in segments:
        span = end - start
        squared = float(span @ span)
        row_low = max(0, int(floor((min(start[1], end[1]) - radius - ys[0]) / cell_mm)))
        row_high = min(ys.shape[0] - 1,
                       int(ceil((max(start[1], end[1]) + radius - ys[0]) / cell_mm)))
        col_low = max(0, int(floor((min(start[0], end[0]) - radius - xs[0]) / cell_mm)))
        col_high = min(xs.shape[0] - 1,
                       int(ceil((max(start[0], end[0]) + radius - xs[0]) / cell_mm)))
        if row_high < row_low or col_high < col_low:
            continue
        block_x = xs[col_low:col_high + 1]
        block_y = ys[row_low:row_high + 1]
        grid_x, grid_y = np.meshgrid(block_x, block_y)
        points = np.column_stack((grid_x.ravel(), grid_y.ravel()))
        if squared <= 1e-12:
            distances = np.linalg.norm(points - start, axis=1)
        else:
            along = np.clip(((points - start) @ span) / squared, 0.0, 1.0)
            closest = start + along[:, None] * span
            distances = np.linalg.norm(points - closest, axis=1)
        covered[row_low:row_high + 1, col_low:col_high + 1] |= (
            distances <= radius
        ).reshape(block_y.shape[0], block_x.shape[0])
    return covered


def _label_patches(uncut: NDArray[np.bool_]) -> list[tuple[int, int, int, int, int]]:
    """把未切除格子按行切成游程并合并相邻行，返回 (面积格数, 行起, 行止, 列起, 列止)。"""

    rows = uncut.shape[0]
    runs: list[tuple[int, int, int]] = []
    per_row: list[list[int]] = []
    for row in range(rows):
        line = uncut[row]
        ids: list[int] = []
        if line.any():
            edges = np.diff(np.concatenate(([False], line, [False])).astype(np.int8))
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            for begin, finish in zip(starts, ends):
                ids.append(len(runs))
                runs.append((row, int(begin), int(finish)))
        per_row.append(ids)

    parent = list(range(len(runs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for row in range(1, rows):
        previous = per_row[row - 1]
        current = per_row[row]
        for left in previous:
            left_start, left_end = runs[left][1], runs[left][2]
            for right in current:
                right_start, right_end = runs[right][1], runs[right][2]
                if left_start < right_end and right_start < left_end:
                    root_left, root_right = find(left), find(right)
                    if root_left != root_right:
                        parent[root_right] = root_left

    groups: dict[int, list[int]] = {}
    for index in range(len(runs)):
        groups.setdefault(find(index), []).append(index)

    patches = []
    for members in groups.values():
        cells = sum(runs[index][2] - runs[index][1] for index in members)
        row_begin = min(runs[index][0] for index in members)
        row_end = max(runs[index][0] for index in members)
        col_begin = min(runs[index][1] for index in members)
        col_end = max(runs[index][2] for index in members)
        patches.append((cells, row_begin, row_end, col_begin, col_end))
    patches.sort(reverse=True)
    return patches


def _merge_rectangles(
    uncut: NDArray[np.bool_], limit: int
) -> tuple[list[tuple[int, int, int, int]], bool]:
    """把未切除格子贪心合并成尽量少的矩形，返回格坐标 (列起, 行起, 列止, 行止) 与是否被截断。

    先按行扫出连续段，再尽量往下扩；成片漏切（条带、中心残留）因此会变成几个大矩形，
    载荷小、画起来也简单。
    """

    rows, columns = uncut.shape
    remaining = uncut.copy()
    rects: list[tuple[int, int, int, int]] = []
    for row in range(rows):
        line = remaining[row]
        if not line.any():
            continue
        edges = np.diff(np.concatenate(([False], line, [False])).astype(np.int8))
        for begin, finish in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            span = remaining[row:, begin:finish]
            filled = span.all(axis=1)
            if filled.all():
                bottom = rows - 1
            else:
                bottom = row + int(np.argmin(filled)) - 1
            remaining[row:bottom + 1, begin:finish] = False
            rects.append((int(begin), row, int(finish), bottom + 1))
            if len(rects) >= limit:
                return rects, True
    return rects, False


def measure_coverage(
    toolpath: Toolpath,
    region: RegionShape,
    tool: Tool,
    *,
    cell_mm: float = DEFAULT_CELL_MM,
    max_rects: int = MAX_RECTS,
) -> Coverage:
    """量一下这条刀路在给定区域上切干净了没有。"""

    polygon = ensure_ccw(region.boundary())
    x_min, x_max, y_min, y_max = bounding_box(polygon)
    cell = _grid_step(polygon, cell_mm)
    columns = max(1, int(ceil((x_max - x_min) / cell)) + 1)
    rows = max(1, int(ceil((y_max - y_min) / cell)) + 1)
    xs = x_min + (np.arange(columns, dtype=np.float64) + 0.5) * cell
    ys = y_min + (np.arange(rows, dtype=np.float64) + 0.5) * cell
    area_per_cell = cell * cell

    inside = _inside_mask(polygon, ys, xs)
    covered = _covered_mask(
        _cutting_segments(toolpath), tool.footprint_radius_mm, xs, ys, cell
    )
    uncut = inside & ~covered
    inside_cells = int(inside.sum())
    uncut_cells = int(uncut.sum())

    labelled = _label_patches(uncut) if uncut_cells else []
    grid_rects, truncated = (
        _merge_rectangles(uncut, max_rects) if uncut_cells else ([], False)
    )
    patches = []
    for cells, row_begin, row_end, col_begin, col_end in labelled[:MAX_PATCHES]:
        patches.append(
            UncutPatch(
                area_mm2=cells * area_per_cell,
                centre_mm=(
                    float((xs[col_begin] + xs[col_end - 1]) / 2.0),
                    float((ys[row_begin] + ys[row_end]) / 2.0),
                ),
                bounds_mm=(
                    float(xs[col_begin] - cell / 2.0),
                    float(xs[col_end - 1] + cell / 2.0),
                    float(ys[row_begin] - cell / 2.0),
                    float(ys[row_end] + cell / 2.0),
                ),
            )
        )

    return Coverage(
        cell_mm=cell,
        region_area_mm2=inside_cells * area_per_cell,
        covered_area_mm2=(inside_cells - uncut_cells) * area_per_cell,
        uncut_area_mm2=uncut_cells * area_per_cell,
        patch_count=len(labelled),
        patches=tuple(patches),
        uncut_rects=tuple(
            (
                float(x_min + col_begin * cell),
                float(y_min + row_begin * cell),
                float(x_min + col_end * cell),
                float(y_min + row_end * cell),
            )
            for col_begin, row_begin, col_end, row_end in grid_rects
        ),
        uncut_rects_truncated=truncated,
    )


def coverage_warnings(coverage: Coverage) -> list[str]:
    """未切除得比较多时给一条提醒（和策略的提醒一起显示在界面上）。"""

    if coverage.uncut_area_mm2 <= 0.0:
        return []
    if coverage.ratio >= 1.0 - WARN_UNCUT_RATIO:
        return []
    biggest = coverage.patches[0] if coverage.patches else None
    where = ""
    if biggest is not None:
        where = (f"，最大一块 {biggest.area_mm2:.1f} mm²（约在 "
                 f"x {biggest.centre_mm[0]:.1f}、y {biggest.centre_mm[1]:.1f} 处）")
    return [
        f"有 {coverage.patch_count} 处共 {coverage.uncut_area_mm2:.1f} mm² 没有切到"
        f"（覆盖率 {coverage.ratio * 100:.1f}%）{where}"
    ]
