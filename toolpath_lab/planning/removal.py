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

The tool is swept as **its own cross section**, not as a flat-bottomed disc: every sample lowers each
cell it covers to `z + profile_height(distance)`, so a ball nose leaves the curved trough it really
leaves, a bull nose its flat bottom plus corner torus, and the height map shows the **ridges** (the
scallops) between passes instead of a flat floor. Flat and tapered tools keep the flat bottom this
project has always drawn (a tapered flank is modelled for wall clearance, not for the floor it leaves).

Long moves are sampled along their length at half a cell, and short ones are not subsampled at all:
a cell is only lowered by a sample whose surface actually covers it, so no material is credited to a
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
from toolpath_lab.planning.geometry2d import region_inside_mask

_EPS = 1e-9

#: Below this floor ratio the plan is reported as not having reached the floor everywhere.
WARN_FLOOR_RATIO = 0.9

#: Upper bound on the cells of the grid that travels to the 3D view. The measurement grid keeps its
#: own resolution (it is what the volumes are summed from); the display grid is block reduced from it.
MAX_HEIGHT_CELLS = 4096

#: Progressive (playback linked) snapshots: how many of them are taken during one sweep, and how coarse
#: each one is. They are cut from the same measurement grid as the final map, so a snapshot costs one
#: block reduction, not another sweep -- the linkage is nearly free. Kept small on purpose: this travels
#: in the JSON response.
MAX_CHECKPOINTS = 8
CHECKPOINT_CELLS = 1024


def _reduce_map(
    heights: NDArray[np.float64],
    inside: NDArray[np.bool_],
    bounds_mm: tuple[float, float, float, float],
    floor_mm: float,
    cell_mm: float,
    max_cells: int,
) -> dict[str, Any]:
    """The coarse grid the 3D view colours by depth: block reduced until it fits `max_cells`.

    A displayed cell takes the **deepest** cut recorded anywhere in its block -- how deep the tool
    reached there. That is what "colour by depth of cut" asks for, and it degrades gracefully: a coarse
    cell holding one machined strip shows the strip's depth instead of averaging it away against the
    material beside it (with the opposite rule, a region whose strips are thinner than a display cell
    would go blank while the volumes say a lot of material was removed). The price is that a coarse cell
    may show floor depth next to material the tool missed; which cells were missed *entirely* is the
    uncut overlay's answer, computed at the full measurement resolution.

    Cells with nothing inside the region are `None`, so what the view paints is the region and not its
    bounding box; the cells tile the region bounds exactly, because the block edges are placed on the
    bounds rather than on the measurement grid (a padded last block would otherwise stick out by up to
    one cell).
    """

    rows, cols = heights.shape
    # Grow the factor until the reduced grid really fits: the two axes have to share one factor (the
    # aspect is part of the picture), and a long thin region would otherwise blow the cap.
    factor = 1
    while -(-rows // factor) * -(-cols // factor) > max_cells:
        factor += 1
    out_rows, out_cols = -(-rows // factor), -(-cols // factor)

    x_min, x_max, y_min, y_max = bounds_mm
    span_x, span_y = x_max - x_min, y_max - y_min
    step_x, step_y = span_x / out_cols, span_y / out_rows
    xs = x_min + (np.arange(cols) + 0.5) * (span_x / cols)
    ys = y_min + (np.arange(rows) + 0.5) * (span_y / rows)
    column_edges = np.searchsorted(xs, x_min + np.arange(out_cols + 1) * step_x, side="left")
    row_edges = np.searchsorted(ys, y_min + np.arange(out_rows + 1) * step_y, side="left")

    # Outside cells become +inf so they can never win the minimum; a block with no inside cell stays
    # +inf, which is what marks it empty below (no NaN warnings involved).
    filled = np.where(inside, heights, np.inf)
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
        "floor_mm": round(floor_mm, 4),
        "measured_cell_mm": round(cell_mm, 4),
        "cells": cells,
    }


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
    #: Height of the ridge the tool's own profile leaves between two passes at the stepover it was told
    #: about (the classic scallop height); `None` when there is no stepover or the passes never overlap.
    cusp_mm: float | None
    #: Tolerance `floor_ratio` counted "at the floor" with: the cusp above, so a curved bottom is not
    #: reported as unfinished for the waviness it always leaves.
    floor_tolerance_mm: float
    #: Requested stepover; only used to report the scallop the tool leaves between two passes.
    stepover_mm: float | None
    #: Progressive snapshots for the playback: "what the floor looked like once the tool had got this
    #: far". Each entry has `move_index` (index into the toolpath's moves, the same numbering the
    #: timeline uses), `progress` (share of the cutting moves done) and `map` (a reduced height map).
    checkpoints: tuple[dict[str, Any], ...]
    heights_mm: NDArray[np.float64]
    inside_mask: NDArray[np.bool_]
    bounds_mm: tuple[float, float, float, float]
    def height_map(self, max_cells: int = MAX_HEIGHT_CELLS) -> dict[str, Any]:
        """The coarse grid the 3D view colours by depth (not part of `describe` when there is none).

        A thin wrapper over `_reduce_map`; see it for what a reduced cell means.
        """

        return _reduce_map(
            self.heights_mm, self.inside_mask, self.bounds_mm, self.floor_mm, self.cell_mm, max_cells
        )

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
            "floor_tolerance_mm": round(self.floor_tolerance_mm, 4),
            "cusp_mm": None if self.cusp_mm is None else round(self.cusp_mm, 4),
            "stepover_mm": round(self.stepover_mm, 4) if self.stepover_mm is not None else None,
            "checkpoints": [dict(entry) for entry in self.checkpoints],
            "bounds_mm": [round(value, 4) for value in self.bounds_mm],
            "height_map": self.height_map() if self.floor_mm < -_EPS else None,
        }


def _grid(size: float, cell_mm: float) -> tuple[NDArray[np.float64], float]:
    """Cell centres along one axis, and the cell size actually used."""

    count = max(1, int(ceil(size / max(cell_mm, _EPS))))
    cell = size / count
    return (np.arange(count) + 0.5) * cell, cell


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
    stepover_mm: float | None = None,
    checkpoints: int = 0,
    checkpoint_cells: int = CHECKPOINT_CELLS,
) -> Removal:
    """Sweep the toolpath over a height map of the region and report what is left.

    `stepover_mm` is what the toolpath was planned with. It is used for one thing: the ridge height the
    tool's own profile leaves between two passes, which is also the tolerance `floor_ratio` counts "at
    the floor" with. A curved bottom always leaves a wavy floor, and calling that unfinished would be a
    false alarm, so the floor counts as reached where what is left is no taller than the ridge that is
    inherent to the stepover. A flat bottom's ridge is 0, so its numbers do not move at all.

    `checkpoints` asks for progressive snapshots of the floor (see `Removal.checkpoints`): at most
    `MAX_CHECKPOINTS` of them, evenly spaced over the cutting moves. They are cut from the same grid
    during the same sweep, so the playback linkage costs a few block reductions rather than another
    removal measurement.
    """

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
    # Region minus islands: an island's material stays, so it is not region to machine.
    inside = region_inside_mask(region, xs, ys)

    grid_x, grid_y = np.meshgrid(xs, ys)
    # The blank starts at the region's own top face: zero everywhere for every flat shape (so those
    # numbers are exactly what they always were), or the surface itself for a curved blank. NaN from
    # the surface means "no material here", and those cells are outside `inside` anyway.
    top = np.nan_to_num(
        np.asarray(region.top_height_mm(grid_x, grid_y), dtype=np.float64), nan=0.0
    )
    heights = top.copy()
    # "Touched" is tracked separately from the height: a single layer cutting at Z = 0 leaves every
    # height at zero, and reading that as "nothing was cut" would be wrong.
    touched = np.zeros_like(heights, dtype=bool)
    reach = float(tool.reach_radius_mm)
    sample_step = max(min(cell_x, cell_y) / 2.0, _EPS)
    bounds = (float(x_min), float(x_max), float(y_min), float(y_max))

    # Progressive snapshots: which cutting move ends each one, evenly spaced over the path. The last
    # snapshot of a run is the finished floor, which already travels as the full map, so the snapshots
    # sit at 1/(n+1) ... n/(n+1) of the cutting moves.
    cutting_total = sum(1 for move in toolpath.moves if move.is_cutting)
    wanted = min(checkpoints, MAX_CHECKPOINTS) if checkpoints > 0 else 0
    snapshot_step: dict[int, int] = {}
    if wanted and cutting_total:
        cutting_seen = 0
        for move_index, move in enumerate(toolpath.moves):
            if not move.is_cutting:
                continue
            cutting_seen += 1
            for step in range(1, wanted + 1):
                if cutting_seen == max(1, (cutting_total * step) // (wanted + 1)):
                    snapshot_step[move_index] = step
    snapshots: list[tuple[int, float, dict[str, Any]]] = []
    cutting_done = 0

    for move_index, move in enumerate(toolpath.moves):
        if not move.is_cutting:
            continue
        points = np.asarray(move.points, dtype=np.float64)
        for index in range(points.shape[0] - 1):
            for sample in _sample_segment(points[index], points[index + 1], sample_step):
                if reach <= _EPS:
                    # A pointed tool (a needle, or a tool of radius 0) removes only under its axis:
                    # credit the cell that contains the sample instead of demanding a centre coincidence.
                    column = int(np.clip(np.searchsorted(xs, sample[0]), 0, xs.size - 1))
                    row = int(np.clip(np.searchsorted(ys, sample[1]), 0, ys.size - 1))
                    touched[row, column] = True
                    heights[row, column] = min(heights[row, column], sample[2])
                    continue
                row_start = int(np.searchsorted(ys, sample[1] - reach, side="left"))
                row_end = int(np.searchsorted(ys, sample[1] + reach, side="right"))
                column_start = int(np.searchsorted(xs, sample[0] - reach, side="left"))
                column_end = int(np.searchsorted(xs, sample[0] + reach, side="right"))
                if row_start >= row_end or column_start >= column_end:
                    continue
                block_y = ys[row_start:row_end, None] - sample[1]
                block_x = xs[None, column_start:column_end] - sample[0]
                squared = block_x * block_x + block_y * block_y
                covered = squared <= reach * reach + _EPS
                # The surface sits `profile_height` above the tip, so the cell ends up that much
                # higher: that height *is* the residual ridge a ball or bull nose leaves behind.
                offsets = tool.profile_height_mm(np.sqrt(np.maximum(squared, 0.0)))
                heights[row_start:row_end, column_start:column_end][covered] = np.minimum(
                    heights[row_start:row_end, column_start:column_end][covered],
                    sample[2] + offsets[covered],
                )
                # "Touched" means the surface actually went down to the top face: a curved nose grazes
                # the material only near its axis, and counting the rest as machined would hide real
                # untouched area. A flat bottom cutting at Z = 0 still counts (the top face pass takes
                # nothing off, but the tool was there at depth), which is the rule the volumes use.
                touched[row_start:row_end, column_start:column_end] |= covered & (
                    sample[2] + offsets <= _EPS
                )
        cutting_done += 1
        if move_index in snapshot_step:
            # Reduced now (cheap) with a placeholder floor; the real floor is patched in below, so every
            # snapshot is coloured on the same scale as the finished map.
            snapshots.append(
                (
                    move_index,
                    cutting_done / cutting_total,
                    _reduce_map(heights, inside, bounds, 0.0, cell, checkpoint_cells),
                )
            )

    inside_heights = heights[inside]
    cell_area = cell_x * cell_y
    floor_mm = float(inside_heights.min()) if inside_heights.size else 0.0
    cut_something = floor_mm < -_EPS
    region_area = float(inside.sum()) * cell_area
    # Material taken off is measured from the blank's own surface, not from a flat zero: on a domed
    # blank the crown comes off even by a pass at Z = 0, and a flat blank's numbers are unchanged.
    removed = float(np.sum(top[inside] - inside_heights)) * cell_area
    remaining = float(np.sum(inside_heights - floor_mm)) * cell_area
    uncut_cells = int(np.sum(~touched[inside]))
    # The ridge between two passes is the profile height half a stepover from the axis. When the passes
    # do not overlap it is infinite, and only then does a curved floor count the strict way.
    cusp = tool.cusp_height_mm(float(stepover_mm)) if stepover_mm else None
    if cusp is None or cusp == float("inf"):
        cusp = None
        tolerance = 1e-6
    else:
        tolerance = cusp
    at_floor = (
        int(np.sum(inside_heights - floor_mm <= tolerance + 1e-9)) if cut_something else 0
    )
    # Snapshots are coloured on the finished floor's scale, so the animation does not re-scale itself.
    entries = tuple(
        {
            "move_index": move_index,
            "progress": round(progress, 6),
            "map": {**snapshot, "floor_mm": round(floor_mm, 4)},
        }
        for move_index, progress, snapshot in snapshots
    )
    return Removal(
        cell_mm=cell,
        floor_mm=floor_mm,
        removed_volume_mm3=removed,
        remaining_volume_mm3=remaining,
        uncut_area_mm2=uncut_cells * cell_area,
        floor_ratio=(at_floor / inside_heights.size) if inside_heights.size else 0.0,
        region_area_mm2=region_area,
        cusp_mm=cusp,
        floor_tolerance_mm=tolerance,
        stepover_mm=float(stepover_mm) if stepover_mm else None,
        checkpoints=entries,
        heights_mm=heights,
        inside_mask=inside,
        bounds_mm=(float(x_min), float(x_max), float(y_min), float(y_max)),
    )
