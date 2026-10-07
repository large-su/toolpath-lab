"""Toolpath coverage analysis: does this toolpath machine the region completely?

In flat machining the usual problem is not the shape of the path but a piece that was missed: a
stepover larger than the tool diameter leaves residual strips, "follow the contour" boundary mode
machines outside the region, and contouring a narrow region can leave the centre untouched. This
module compares "the area the tool swept" against "the area of the region" and reports the uncut
area, its ratio, and where the uncut patches are.

How it works: lay a grid over the region's bounding box and decide cell by cell

1. whether the cell centre is inside the region -- reusing the scanline intervals of the raster
   strategy;
2. whether the cell centre is within the tool's footprint radius of any **material removing** move
   (cut or link; rapids do not remove material).

The tool is treated as a disc of its footprint radius: that is what a flat end mill sweeps on a
flat face (once ball or bull nose tools are enabled, this radius has to change with the tool).
The machining plane is fixed at Z = 0, so only planar distances matter.

It lives in the planning layer because it shares the planar geometry (`geometry2d`) with the
strategies, and the layering rule says `planning` / `simulation` / `export` may only depend on
`core` -- copying the geometry into another layer would not pay off.
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

#: Default cell size (mm): too small slows a plan down, too large hides small missed spots.
DEFAULT_CELL_MM = 0.5
#: Upper bound on the number of cells: large regions grow the cell size so the cost stays bounded.
MAX_CELLS = 400_000
#: How many uncut patches are reported individually (the rest only count towards the total).
MAX_PATCHES = 8
#: How many uncut rectangles are emitted for the 3D overlay: a missed band is usually a few big
#: rectangles, and that is enough.
MAX_RECTS = 800
#: Only warn above this uncut ratio: a round tool always leaves a little at the sharp corners of a
#: square and where a curve is approximated by chords, which is unavoidable geometry and should not
#: pop up a warning every time. What deserves a warning is a whole missed band (too large a
#: stepover, contouring that leaves the centre).
WARN_UNCUT_RATIO = 0.02


@dataclass(frozen=True, slots=True)
class UncutPatch:
    """One patch that was not machined (positions are grid-accurate, enough to locate the problem)."""

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
    """Result of one coverage analysis."""

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
            # Uncut cells merged into rectangles (x0, y0, x1, y1 in mm) for the 3D overlay
            "uncut_rects": [
                [round(value, 4) for value in rect] for rect in self.uncut_rects
            ],
            "uncut_rects_truncated": self.uncut_rects_truncated,
        }


def _grid_step(polygon: NDArray[np.float64], cell_mm: float) -> float:
    """Keep the cell size sensible for the region area: large regions grow it, cells stay bounded."""

    area = max(abs(signed_area(polygon)), 1.0)
    return max(float(cell_mm), sqrt(area / MAX_CELLS))


def _cutting_segments(toolpath: Toolpath) -> list[tuple[NDArray[np.float64], NDArray[np.float64]]]:
    """Material removing segments (cut and link); rapids do not remove material and never count."""

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
    """Mark "cell centre inside the region" row by row, using scanline intervals."""

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
    """Cells swept by the tool: every move only touches the small grid block around it."""

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
    """Split uncut cells into row runs and union adjacent rows.

    Returns (cell count, first row, last row, first column, last column) per patch.
    """

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
    """Greedily merge uncut cells into as few rectangles as possible.

    Returns grid coordinates (first column, first row, last column, last row) and whether the limit
    truncated the list. Row runs are extended downwards as far as possible, so a whole missed band
    becomes a few big rectangles: small payload, easy to draw.
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
    """Measure whether this toolpath machines the given region completely."""

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
    """Warn when a lot is left uncut (shown together with the strategy's own warnings)."""

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
