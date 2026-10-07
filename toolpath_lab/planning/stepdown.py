"""Step-down: repeat the toolpath at successive depths.

Machining a floor to a depth is not one pass. The tool goes down by a limited depth of cut each layer
until the total depth is reached, and every strategy in this project plans a single plane at Z = 0.
This module turns that one-plane toolpath into the stack of layers, so no strategy has to know about
depth and a third party plugin gets the behaviour for free.

The model is deliberately simple: **a layer is the same toolpath translated down**. The region's walls
are vertical and its floor flat, so the planar path is identical at every depth and only Z changes.
Points that reach the machining plane (every cutting and link point, plus the bottom of every plunge)
move down with their layer, while points above it keep their absolute height -- so `safe_height_mm`
stays exactly what its help text says ("how far above the top surface the tool retracts") and the
rapid plane never sinks towards the walls.

Two honest limitations, both consequences of that simplicity:

- only flat floors with vertical walls are modelled: there are no islands, so nothing at a lower
  layer can collide with material that was still there at a higher one;
- coverage stays a planar measurement (it only looks at XY distances), so a layered toolpath measures
  the same as a single layer does. Depth-wise coverage would need a real material removal simulation.

A total depth of 0 (the default) switches the whole thing off, so the default toolpath is still the
single plane this project's reference numbers were computed with.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.path import Move, Toolpath

_EPS = 1e-9

#: Default total depth in mm; 0 means "one layer on the top face", i.e. the feature is off.
DEFAULT_DEPTH_MM = 0.0
#: Default depth of cut per layer in mm.
DEFAULT_STEPDOWN_MM = 2.0


def layer_depths(depth_mm: float, stepdown_mm: float) -> list[float]:
    """Z of every layer, top face first, the last one exactly at -depth_mm.

    With depth 5 and stepdown 2 that is [0, -2, -4, -5]: the last layer takes the remainder rather
    than overshooting the requested depth.
    """

    if depth_mm <= _EPS or stepdown_mm <= _EPS:
        return [0.0]
    depths = [0.0]
    current = 0.0
    while depth_mm - current > _EPS:
        current = min(depth_mm, current + stepdown_mm)
        depths.append(-current)
    return depths


def _shifted_points(points: NDArray[np.float64], depth: float) -> NDArray[np.float64]:
    """One move's points on a layer: what reaches the machining plane goes down with it."""

    shifted = np.array(points, dtype=np.float64, copy=True)
    reaches_plane = shifted[:, 2] <= _EPS
    shifted[reaches_plane, 2] += depth
    return shifted


def apply_stepdown(
    toolpath: Toolpath,
    *,
    depth_mm: float,
    stepdown_mm: float,
) -> Toolpath:
    """Return the toolpath stacked into layers from the top face down to depth_mm."""

    depths = layer_depths(depth_mm, stepdown_mm)
    if len(depths) == 1 or not toolpath.moves:
        return toolpath

    layer_pass_count = max(toolpath.pass_count, 1)
    moves: list[Move] = []
    for layer_index, depth in enumerate(depths):
        for move in toolpath.moves:
            label = move.label
            if label:
                label = f"第 {layer_index + 1} 层 {label}"
            moves.append(
                replace(
                    move,
                    points=_shifted_points(move.points, depth),
                    pass_index=(
                        move.pass_index + layer_index * layer_pass_count
                        if move.pass_index >= 0
                        else move.pass_index
                    ),
                    label=label,
                )
            )

    note = (
        f"分层：共 {len(depths)} 层（每层下刀 {stepdown_mm:g} mm，总深 {depth_mm:g} mm），"
        f"每层是同一 XY 路径依次下移，抬刀仍回到上表面之上的安全高度"
    )
    return replace(toolpath, moves=tuple(moves), notes=toolpath.notes + (note,))
