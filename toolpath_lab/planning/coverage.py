"""Estimate complete finishing-path coverage in XY, not material removal.

Inspired by U202310887's grid coverage analysis; independently adapted for
curved surfaces, oriented paths and the roughing/finishing split. See
docs/coverage.md for provenance and deliberately limited interpretation.
"""

from __future__ import annotations

from math import ceil, isfinite, sqrt
from typing import Any

import numpy as np

from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import RegionShape, polygon_area
from toolpath_lab.core.surface import SurfaceShape
from toolpath_lab.core.tool import Tool

DEFAULT_CELL_MM = 0.5
MAX_GRID_CELLS = 40_000
MAX_GRID_SIDE = 2048


def _inside_polygon(x: np.ndarray, y: np.ndarray, boundary: np.ndarray) -> np.ndarray:
    """Classify cell centers with an even/odd rule, including concave regions."""
    inside = np.zeros(x.shape, dtype=bool)
    for a, b in zip(boundary, np.roll(boundary, -1, axis=0)):
        if abs(float(b[1] - a[1])) < 1e-12:
            continue
        crossing = (a[1] > y) != (b[1] > y)
        edge_x = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
        inside ^= crossing & (x < edge_x)
    return inside


def analyze_coverage(
    toolpath: Toolpath, region: RegionShape, tool: Tool, surface: SurfaceShape,
    *, cell_mm: float = DEFAULT_CELL_MM, max_cells: int = MAX_GRID_CELLS,
) -> dict[str, Any]:
    """Union radius-R capsules of finishing CUT/LINK segments on a bounded grid.

    R is the *outer* cutter radius, not its floor-contact radius. Z, cutter
    shape and tilt are intentionally ignored; even 100% is not evidence of
    reaching the target surface. RAPID and prefixed roughing moves are excluded.
    """
    if not isfinite(cell_mm) or cell_mm <= 0 or max_cells < 4:
        raise ValueError("Coverage grid requires a positive cell size and at least four cells")
    boundary = region.boundary()
    low, high = boundary.min(axis=0), boundary.max(axis=0)
    width, height = high - low
    # The loop also bounds very thin, long regions where area-based scaling alone fails.
    spacing = max(cell_mm, sqrt(float(width * height) / max_cells),
                  float(width / MAX_GRID_SIDE), float(height / MAX_GRID_SIDE))
    nx, ny = max(1, ceil(width / spacing)), max(1, ceil(height / spacing))
    while nx * ny > max_cells or max(nx, ny) > MAX_GRID_SIDE:
        spacing *= max(1.01, sqrt(nx * ny / max_cells))
        nx, ny = max(1, ceil(width / spacing)), max(1, ceil(height / spacing))
    dx, dy = float(width / nx), float(height / ny)
    xs = low[0] + (np.arange(nx) + 0.5) * dx
    ys = low[1] + (np.arange(ny) + 0.5) * dy
    xx, yy = np.meshgrid(xs, ys)
    active = _inside_polygon(xx, yy, boundary)
    covered = np.zeros(active.shape, dtype=bool)
    radius = tool.radius_mm
    finish_start = int(toolpath.metadata.get("roughing", {}).get("finish_start_move_index", 0))
    for move in toolpath.moves[finish_start:]:
        if move.kind not in (MoveKind.CUT, MoveKind.LINK):
            continue
        for a, b in zip(move.points[:-1, :2], move.points[1:, :2]):
            lower = np.minimum(a, b) - radius
            upper = np.maximum(a, b) + radius
            x0, x1 = np.searchsorted(xs, [lower[0], upper[0]], side="left")
            y0, y1 = np.searchsorted(ys, [lower[1], upper[1]], side="left")
            # Include a center exactly on the capsule's upper boundary.
            x1 = min(nx, x1 + 1)
            y1 = min(ny, y1 + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            cut = np.s_[y0:y1, x0:x1]
            remaining = active[cut] & ~covered[cut]
            if not remaining.any():
                continue
            px, py = xx[cut] - a[0], yy[cut] - a[1]
            delta = b - a
            length2 = float(delta @ delta)
            t = np.clip((px * delta[0] + py * delta[1]) / max(length2, 1e-20), 0, 1)
            distance2 = (px - t * delta[0]) ** 2 + (py - t * delta[1]) ** 2
            covered[cut] |= remaining & (distance2 <= radius ** 2 + 1e-10)
    active_count = int(active.sum())
    covered_count = int((active & covered).sum())
    # No sampled centers can occur for tiny/sliver polygons. Do not fabricate 0/100%.
    fraction = covered_count / active_count if active_count else None
    area = abs(polygon_area(boundary))
    uncovered = active & ~covered
    return {
        "method": "xy_outer_radius_sweep",
        "scope": "complete_finishing_path",
        "estimate_only": True,
        "available": bool(active_count),
        "coverage_percent": None if fraction is None else fraction * 100,
        "region_area_mm2": area,
        "covered_area_mm2": None if fraction is None else area * fraction,
        "uncovered_area_mm2": None if fraction is None else area * (1 - fraction),
        "active_cell_count": active_count,
        "uncovered_cell_count": int(uncovered.sum()),
        "radius_mm": radius,
        "finish_start_move_index": finish_start,
        "approximate_contact": surface.id != "flat" or tool.kind.value != "flat"
            or any(move.is_oriented for move in toolpath.moves[finish_start:]),
        "note": "整条精加工刀路的 XY 外径扫掠估算；忽略高度、刀尖形状与倾角，不代表实际残料、残留高度或无碰撞。",
        "grid": {
            "nx": nx, "ny": ny,
            "bounds_mm": [[float(low[0]), float(high[0])], [float(low[1]), float(high[1])]],
            "cell_size_mm": [dx, dy],
            "max_cells": max_cells,
            "max_side": MAX_GRID_SIDE,
            # Row-major, bottom to top. Outside-region centers are transparent too.
            "uncovered_mask": uncovered.astype(np.uint8).ravel().tolist(),
        },
    }
