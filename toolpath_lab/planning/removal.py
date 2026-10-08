"""2.5D material removal: a height map of the stock as the toolpath passes over it.

Coverage answers "did the tool pass over this cell" in the plane. This module answers the next
question: **how deep** did it cut there. Every cell inside the region starts at the top face (Z = 0)
and is lowered to the Z of any material removing move that sweeps over it, so the result is a height
map you can read volumes off:

- `removed_volume_mm3`: material actually taken away;
- `remaining_volume_mm3`: material still above the deepest floor the toolpath reaches;
- `uncut_area_mm2`: cells the tool never touched at all;
- `floor_ratio`: the fraction of the region that reached the floor -- the depth aware version of the
  coverage ratio, and the number that says whether a pocket is really finished.

The tool is the same flat-bottomed disc the rest of the project assumes (footprint radius on its
bottom): a ball nose tool therefore removes material only under its tip, which is exactly what the
planar model can honestly say about it.

Long moves are sampled along their length at half a cell, and short ones are not subsampled at all:
a cell is only lowered by a sample whose disc actually covers it, so no material is credited to a
tool position the machine never visits.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.coverage import DEFAULT_CELL_MM, MAX_CELLS

_EPS = 1e-9

#: Below this floor ratio the plan is reported as not having reached the floor everywhere.
WARN_FLOOR_RATIO = 0.9

#: Upper bound on the cells of the grid that travels to the 3D view. The measurement grid keeps its
#: own resolution (it is what the volumes are summed from); the display grid is block reduced from it.
MAX_HEIGHT_CELLS = 4096


def removal_warnings(removal: "Removal") -> list[str]:
    """Warnings for a plan whose floor is not finished (they go into the response banner)."""

    if removal.removed_volume_mm3 <= 0.0:
        return []  # a single layer on the top face has nothing to measure against
    if removal.floor_ratio >= WARN_FLOOR_RATIO:
        return []
    return [
        f"到面率只有 {removal.floor_ratio * 100:.1f} %：目标地面（Z = {removal.floor_mm:g} mm）"
        f"之上还留着 {removal.remaining_volume_mm3:.0f} mm³，"
        f"其中 {removal.uncut_area_mm2:.0f} mm² 完全没有切到"
    ]


@dataclass(frozen=True, slots=True)
class Removal:
    """What the toolpath removed, cell by cell."""

    cell_mm: float
    floor_mm: float
    removed_volume_mm3: float
    remaining_volume_mm3: float
    uncut_area_mm2: float
    floor_ratio: float
    region_area_mm2: float
    heights_mm: NDArray[np.float64]
    inside_mask: NDArray[np.bool_]
    bounds_mm: tuple[float, float, float, float]

    def height_map(self, max_cells: int = MAX_HEIGHT_CELLS) -> dict[str, Any]:
        """The coarse grid the 3D view colours by depth (not part of `describe` when there is none).

        The measurement grid is reduced in blocks until it fits `max_cells`, and a displayed cell
        takes the **deepest** cut recorded anywhere in its block -- how deep the tool reached there.
        That is what "colour by depth of cut" asks for, and it degrades gracefully: a coarse cell
        holding one
        machined strip shows the strip's depth instead of averaging it away against the material
        beside it (with the opposite rule, a region whose strips are thinner than a display cell
        would go blank while the volumes say a lot of material was removed). The price is that a
        coarse cell may show floor depth next to material the tool missed; which cells were missed
        *entirely* is the uncut overlay's answer, computed at the full measurement resolution.
        `floor_mm` and `measured_cell_mm` say how fine the picture is. Cells with nothing inside the
        region are `None`, so what the view paints is the region and not its bounding box; the cells
        tile the region bounds exactly, because the block edges are placed on the bounds rather than
        on the measurement grid (a padded last block would otherwise stick out by up to one cell).
        """

        rows, cols = self.heights_mm.shape
        # Grow the factor until the reduced grid really fits: the two axes have to share one factor
        # (the aspect is part of the picture), and a long thin region would otherwise blow the cap.
        factor = 1
        while -(-rows // factor) * -(-cols // factor) > max_cells:
            factor += 1
        out_rows, out_cols = -(-rows // factor), -(-cols // factor)

        x_min, x_max, y_min, y_max = self.bounds_mm
        span_x, span_y = x_max - x_min, y_max - y_min
        step_x, step_y = span_x / out_cols, span_y / out_rows
        xs = x_min + (np.arange(cols) + 0.5) * (span_x / cols)
        ys = y_min + (np.arange(rows) + 0.5) * (span_y / rows)
        column_edges = np.searchsorted(xs, x_min + np.arange(out_cols + 1) * step_x, side="left")
        row_edges = np.searchsorted(ys, y_min + np.arange(out_rows + 1) * step_y, side="left")

        # Outside cells become +inf so they can never win the minimum; a block with no inside cell
        # stays +inf, which is what marks it empty below (no NaN warnings involved).
        filled = np.where(self.inside_mask, self.heights_mm, np.inf)
        reduced = np.minimum.reduceat(filled, row_edges[:-1], axis=0)
        reduced = np.minimum.reduceat(reduced, column_edges[:-1], axis=1)

        cells = [
            [None if value == np.inf else round(float(value), 4) for value in row]
            for row in reduced
        ]
        return {
            "rows": out_rows,
            "cols": out_cols,
            "origin_mm": [round(x_min, 4), round(y_min, 4)],
            "cell_size_mm": [round(step_x, 4), round(step_y, 4)],
            "floor_mm": round(self.floor_mm, 4),
            "measured_cell_mm": round(self.cell_mm, 4),
            "cells": cells,
        }

    def describe(self) -> dict[str, Any]:
        """Compact JSON form.

        The height map goes out reduced (see `height_map`) because the 3D view is the only reader
        that needs it; it is `None` when nothing was removed, since a single pass on the top face has
        no depth to colour.
        """

        return {
            "cell_mm": round(self.cell_mm, 4),
            "floor_mm": round(self.floor_mm, 4),
            "region_area_mm2": round(self.region_area_mm2, 4),
            "removed_volume_mm3": round(self.removed_volume_mm3, 4),
            "remaining_volume_mm3": round(self.remaining_volume_mm3, 4),
            "uncut_area_mm2": round(self.uncut_area_mm2, 4),
            "floor_ratio": round(self.floor_ratio, 6),
            "bounds_mm": [round(value, 4) for value in self.bounds_mm],
            "height_map": self.height_map() if self.floor_mm < -_EPS else None,
        }


def _grid(size: float, cell_mm: float) -> tuple[NDArray[np.float64], float]:
    """Cell centres along one axis, and the cell size actually used."""

    count = max(1, int(ceil(size / max(cell_mm, _EPS))))
    cell = size / count
    return (np.arange(count) + 0.5) * cell, cell


def _inside_mask(
    xs: NDArray[np.float64], ys: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.bool_]:
    """Cells whose centre is inside the polygon, by the even-odd rule row by row."""

    mask = np.zeros((ys.size, xs.size), dtype=bool)
    x0, y0 = polygon[:, 0], polygon[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    for row, y in enumerate(ys):
        crossing = ((y0 <= y) & (y1 > y)) | ((y1 <= y) & (y0 > y))
        if not crossing.any():
            continue
        ratio = (y - y0[crossing]) / (y1[crossing] - y0[crossing])
        hits = np.sort(x0[crossing] + ratio * (x1[crossing] - x0[crossing]))
        for start in range(0, hits.size - 1, 2):
            mask[row, (xs > hits[start]) & (xs < hits[start + 1])] = True
    return mask


def _sample_segment(
    first: NDArray[np.float64], second: NDArray[np.float64], step: float
) -> NDArray[np.float64]:
    """Points along one segment, at most `step` apart (the endpoints are always included)."""

    length = float(np.linalg.norm(second - first))
    count = 1 if length <= step else max(2, int(ceil(length / max(step, _EPS))) + 1)
    if count == 1:
        return np.vstack([first, second])
    ratios = np.linspace(0.0, 1.0, count)
    return first + ratios[:, None] * (second - first)


def measure_removal(
    toolpath: Toolpath,
    region: RegionShape,
    tool: Tool,
    *,
    cell_mm: float = DEFAULT_CELL_MM,
    max_cells: int = MAX_CELLS,
) -> Removal:
    """Sweep the toolpath over a height map of the region and report what is left."""

    polygon = np.asarray(region.boundary(), dtype=np.float64).reshape(-1, 2)
    x_min, y_min = polygon.min(axis=0)
    x_max, y_max = polygon.max(axis=0)
    span_x, span_y = max(x_max - x_min, _EPS), max(y_max - y_min, _EPS)
    cell = max(cell_mm, sqrt(span_x * span_y / max(max_cells, 1)))
    xs, cell_x = _grid(span_x, cell)
    ys, cell_y = _grid(span_y, cell)
    while xs.size * ys.size > max_cells:  # ceil() can overshoot the budget: grow and re-grid
        cell *= 1.02
        xs, cell_x = _grid(span_x, cell)
        ys, cell_y = _grid(span_y, cell)
    xs = xs + x_min
    ys = ys + y_min
    inside = _inside_mask(xs, ys, polygon)

    heights = np.zeros((ys.size, xs.size), dtype=np.float64)
    # "Touched" is tracked separately from the height: a single layer cutting at Z = 0 leaves every
    # height at zero, and reading that as "nothing was cut" would be wrong.
    touched = np.zeros_like(heights, dtype=bool)
    radius = float(tool.footprint_radius_mm)
    sample_step = max(min(cell_x, cell_y) / 2.0, _EPS)
    for move in toolpath.moves:
        if not move.is_cutting:
            continue
        points = np.asarray(move.points, dtype=np.float64)
        for index in range(points.shape[0] - 1):
            for sample in _sample_segment(points[index], points[index + 1], sample_step):
                if radius <= _EPS:
                    # A pointed tool (a ball nose at its tip) removes only under its axis: credit the
                    # cell that contains the sample instead of demanding a centre coincidence.
                    column = int(np.clip(np.searchsorted(xs, sample[0]), 0, xs.size - 1))
                    row = int(np.clip(np.searchsorted(ys, sample[1]), 0, ys.size - 1))
                    touched[row, column] = True
                    heights[row, column] = min(heights[row, column], sample[2])
                    continue
                row_start = int(np.searchsorted(ys, sample[1] - radius, side="left"))
                row_end = int(np.searchsorted(ys, sample[1] + radius, side="right"))
                column_start = int(np.searchsorted(xs, sample[0] - radius, side="left"))
                column_end = int(np.searchsorted(xs, sample[0] + radius, side="right"))
                if row_start >= row_end or column_start >= column_end:
                    continue
                block_y = ys[row_start:row_end, None] - sample[1]
                block_x = xs[None, column_start:column_end] - sample[0]
                covered = block_x * block_x + block_y * block_y <= radius * radius + _EPS
                heights[row_start:row_end, column_start:column_end][covered] = np.minimum(
                    heights[row_start:row_end, column_start:column_end][covered], sample[2]
                )
                touched[row_start:row_end, column_start:column_end] |= covered

    inside_heights = heights[inside]
    cell_area = cell_x * cell_y
    floor_mm = float(inside_heights.min()) if inside_heights.size else 0.0
    cut_something = floor_mm < -_EPS
    region_area = float(inside.sum()) * cell_area
    removed = float(np.sum(-inside_heights)) * cell_area
    remaining = float(np.sum(inside_heights - floor_mm)) * cell_area
    uncut_cells = int(np.sum(~touched[inside]))
    at_floor = (
        int(np.sum(np.abs(inside_heights - floor_mm) <= 1e-6)) if cut_something else 0
    )
    return Removal(
        cell_mm=cell,
        floor_mm=floor_mm,
        removed_volume_mm3=removed,
        remaining_volume_mm3=remaining,
        uncut_area_mm2=uncut_cells * cell_area,
        floor_ratio=(at_floor / inside_heights.size) if inside_heights.size else 0.0,
        region_area_mm2=region_area,
        heights_mm=heights,
        inside_mask=inside,
        bounds_mm=(float(x_min), float(x_max), float(y_min), float(y_max)),
    )
