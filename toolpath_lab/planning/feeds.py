"""Corner feed reduction: slow the cutting feed down where the path turns sharply.

A machine cannot hold the programmed feed through a sharp corner. The direction change forces the
axes to decelerate, and pushing through it either overshoots the corner or shakes the machine, so
real CAM output lowers the feed around corners. This module does the same for the toolpaths this base
produces.

The model is deliberately simple and explainable: at every interior vertex of a cutting polyline the
turn angle is measured (0° = straight, 180° = a full reversal). Turns up to `corner_angle_deg` keep
the programmed feed; sharper turns ramp linearly down to `corner_feed_ratio` of it at 180°. A segment
takes the smaller factor of its two end vertices, so the slowdown covers exactly the segments that
meet at the corner.

A move carries a single feed rate, so the polyline is split into runs of equal factor: a ring with
four corners becomes a handful of moves (the slow bits at the corners and the fast stretches between
them) instead of one move with a compromise feed. The pass index is preserved, so pass counts and
everything derived from them are untouched, and the geometry stays identical -- only the feeds, and
with them the estimated time and the playback speed, change.

A turn angle of 0 (the default) switches the whole thing off, so the default toolpath is still the
plain geometric one this project's reference numbers were computed with.
"""

from __future__ import annotations

from dataclasses import replace
from math import degrees

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.path import Move, MoveKind, Toolpath

_EPS = 1e-9

#: Default turn angle (degrees) above which the feed starts ramping down; 0 disables the feature.
DEFAULT_CORNER_ANGLE_DEG = 0.0
#: Default feed factor at a full 180° reversal.
DEFAULT_CORNER_FEED_RATIO = 0.35


def turn_angles(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Turn angle at every interior vertex of a polyline, in degrees.

    Straight ahead is 0, a right angle is 90, a full reversal is 180. Degenerate (zero length)
    segments have no direction, so a vertex touching one is treated as straight.
    """

    planar = np.asarray(points, dtype=np.float64).reshape(-1, 3)[:, :2]
    steps = np.diff(planar, axis=0)
    lengths = np.linalg.norm(steps, axis=1)
    if steps.shape[0] < 2:
        return np.zeros(0, dtype=np.float64)
    usable = lengths > _EPS
    unit = np.zeros_like(steps)
    unit[usable] = steps[usable] / lengths[usable, None]
    cosine = np.sum(unit[:-1] * unit[1:], axis=1)
    angle = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
    paired = usable[:-1] & usable[1:]
    return np.where(paired, angle, 0.0)


def feed_factors(
    points: NDArray[np.float64],
    *,
    corner_angle_deg: float,
    corner_feed_ratio: float,
) -> NDArray[np.float64]:
    """Feed factor of every segment, from the turn angles at its two ends."""

    planar = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    factors = np.ones(planar.shape[0] - 1, dtype=np.float64)
    if planar.shape[0] < 3 or corner_angle_deg <= 0.0 or corner_feed_ratio >= 1.0:
        return factors
    span = max(180.0 - corner_angle_deg, _EPS)
    excess = np.clip((turn_angles(planar) - corner_angle_deg) / span, 0.0, 1.0)
    vertex = np.ones(planar.shape[0], dtype=np.float64)
    vertex[1:-1] = 1.0 - (1.0 - corner_feed_ratio) * excess
    return np.minimum(vertex[:-1], vertex[1:])


def apply_corner_slowdown(
    toolpath: Toolpath,
    *,
    corner_angle_deg: float,
    corner_feed_ratio: float,
) -> Toolpath:
    """Return the toolpath with cutting feeds lowered around sharp corners.

    Cutting moves are split into runs of equal feed factor; links and rapids are straight single
    segments with nothing to slow down, so they are kept as they are.
    """

    if corner_angle_deg <= 0.0 or corner_feed_ratio >= 1.0:
        return toolpath

    moves: list[Move] = []
    slowed = 0
    for move in toolpath.moves:
        if move.kind is not MoveKind.CUT or move.points.shape[0] < 3:
            moves.append(move)
            continue
        factors = feed_factors(
            move.points,
            corner_angle_deg=corner_angle_deg,
            corner_feed_ratio=corner_feed_ratio,
        )
        if bool(np.allclose(factors, 1.0)):
            moves.append(move)
            continue
        # Split where the factor changes: one move per run, sharing the boundary point with its
        # neighbour so the path stays continuous.
        breaks = np.flatnonzero(np.abs(np.diff(factors)) > 1e-9) + 1
        starts = np.concatenate(([0], breaks))
        ends = np.concatenate((breaks, [factors.shape[0]]))
        for start, end in zip(starts, ends):
            factor = float(factors[start])
            moves.append(
                replace(
                    move,
                    points=move.points[start:end + 1],
                    feed_mm_per_min=move.feed_mm_per_min * factor,
                )
            )
            slowed += 1
    if slowed == 0:
        return toolpath

    note = (
        f"拐角减速：转角超过 {corner_angle_deg:g}° 的切削段按线性降到 "
        f"{corner_feed_ratio * 100:g}% 进给（180° 时），共 {slowed} 段受影响的移动"
    )
    return replace(toolpath, moves=tuple(moves), notes=toolpath.notes + (note,))
