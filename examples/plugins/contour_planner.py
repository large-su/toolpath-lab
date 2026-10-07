"""Example plugin: contour (equidistant outline) strategy.

A complete, directly usable strategy implementation: it carries the geometry it needs (polygon
inward offset) and depends on public interfaces only, so it can serve as a reference for writing
other strategies.

**Relation to the built-in version**: the built-in toolpath_lab/planning/contour.py has since been
upgraded to "several loops per layer" (once the offset eats a concave neck the shape splits and each
piece is machined separately, with sibling loops reached by retracting and rapid moves), and its
offset geometry moved to planning/geometry2d.py. This file keeps a **single-loop** minimal
implementation to show what a strategy has to contain, and to demonstrate the path "geometry can grow
inside a strategy and move into the main program when it is needed".

**Note**: contouring is already built in as toolpath_lab/planning/contour.py (id `contour`), so the id
here is deliberately `contour_demo`; enabling it directly cannot collide with the built-in strategy.
Two ways to use this file:

1. As a template for your own strategy: copy it to toolpath_lab/planning/my_strategy.py, change the
   id / label, and add one import line to toolpath_lab/planning/__init__.py (import order = the order
   in the UI):

       from toolpath_lab.planning import my_strategy as _my_strategy  # noqa: F401

2. To see this example in the UI: copy it to toolpath_lab/planning/contour_demo.py and add
   `from toolpath_lab.planning import contour_demo as _contour_demo  # noqa: F401`; after a restart
   one more strategy entry (the example plugin) shows up.

The registry raises RegistryError on a duplicate id, so never keep two strategies with the same id.

**Current limitation**: when the offset exceeds the local inscribed radius the ring breaks; this
implementation keeps a single loop per offset layer, so a concave narrow neck ends early (the built-in
version no longer has that limitation).
"""

from __future__ import annotations

from math import atan2, ceil, cos, pi, sin
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import MOTION_PARAMETERS, Planner, PlanningContext
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area
from toolpath_lab.planning.registry import PLANNERS

_EPS = 1e-9
_MIN_RING_AREA_MM2 = 0.5


# --------------------------------------------------------------------------
# This geometry is used by contouring only, so it lives in the plugin; move it into core once a
# second strategy needs it.
# --------------------------------------------------------------------------
def _inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """Unit left normal of every edge (the inward normal for a counter-clockwise polygon)."""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def _distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)[:, None, :]
    start = polygon[None, :, :]
    end = np.roll(polygon, -1, axis=0)[None, :, :]
    edge = end - start
    squared = np.maximum(np.sum(edge * edge, axis=2), _EPS)
    t = np.clip(np.sum((pts - start) * edge, axis=2) / squared, 0.0, 1.0)
    closest = start + t[..., None] * edge
    return np.linalg.norm(pts - closest, axis=2).min(axis=1)


def offset_polygon(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    max_miter: float = 4.0,
) -> NDArray[np.float64] | None:
    """Offset a counter-clockwise polygon inwards by distance >= 0, returning None if it degenerates.

    Convex corners use miter intersections and reflex corners get a circular arc insert -- a polygon
    shrunk inwards really is an arc at a reflex corner; afterwards vertices created by
    self-intersection are filtered out with "distance to the original boundary >= offset distance".

    Inward offsets only: offsetting outwards needs arcs at convex corners and miters at reflex ones,
    and the "distance to the boundary" filter no longer holds either -- that is a different piece of
    geometry. It is not needed, so a negative distance raises.
    """

    if distance < 0.0:
        raise ValueError("offset_polygon 只支持向内偏置（distance >= 0）")
    poly = ensure_ccw(polygon)
    if distance <= _EPS:
        return poly

    normals = _inward_normals(poly)
    previous_normal = np.roll(normals, 1, axis=0)
    previous_edge = poly - np.roll(poly, 1, axis=0)
    next_edge = np.roll(poly, -1, axis=0) - poly

    result: list[tuple[float, float]] = []
    for index in range(poly.shape[0]):
        point = poly[index]
        n_prev, n_next = previous_normal[index], normals[index]
        turn = float(
            previous_edge[index, 0] * next_edge[index, 1]
            - previous_edge[index, 1] * next_edge[index, 0]
        )
        if abs(turn) <= _EPS:
            moved = point + distance * n_next
            result.append((float(moved[0]), float(moved[1])))
            continue

        dot = float(np.clip(n_prev @ n_next, -1.0, 1.0))
        if turn > 0.0 and dot > -0.999:
            moved = point + distance * (n_prev + n_next) / (1.0 + dot)
            if float(np.linalg.norm(moved - point)) <= max_miter * distance:
                result.append((float(moved[0]), float(moved[1])))
                continue

        start_angle = atan2(float(n_prev[1]), float(n_prev[0]))
        end_angle = atan2(float(n_next[1]), float(n_next[0]))
        delta = (end_angle - start_angle + pi) % (2.0 * pi) - pi
        segments = max(1, int(ceil(abs(delta) * distance / max(chord_mm, 1e-6))))
        for step in range(segments + 1):
            angle = start_angle + delta * step / segments
            result.append(
                (
                    float(point[0] + distance * cos(angle)),
                    float(point[1] + distance * sin(angle)),
                )
            )

    candidate = np.array(result, dtype=np.float64)
    tolerance = max(1e-6, 1e-6 * distance)
    filtered = candidate[_distance_to_boundary(candidate, poly) >= distance - tolerance]
    if filtered.shape[0] < 3:
        return None
    gaps = np.linalg.norm(np.diff(np.vstack([filtered, filtered[:1]]), axis=0), axis=1)
    filtered = filtered[np.concatenate(([True], gaps[:-1] > _EPS))]
    if filtered.shape[0] < 3 or signed_area(filtered) <= 0.0:
        return None
    return filtered


def resample_ring(polygon: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """Resample a closed ring at equal arc length (without repeating the first point)."""

    ring = np.vstack([polygon, polygon[:1]])
    steps = np.linalg.norm(np.diff(ring, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(steps)))
    total = float(cumulative[-1])
    if total <= _EPS:
        return np.asarray(polygon, dtype=np.float64)
    count = max(3, int(ceil(total / max(step_mm, 1e-6))))
    targets = np.linspace(0.0, total, count, endpoint=False)
    return np.column_stack(
        (
            np.interp(targets, cumulative, ring[:, 0]),
            np.interp(targets, cumulative, ring[:, 1]),
        )
    )


# --------------------------------------------------------------------------
# The strategy itself
# --------------------------------------------------------------------------
@PLANNERS.register
class ContourPlanner(Planner):
    """Contour toolpath that offsets the region outline inwards ring by ring."""

    id: ClassVar[str] = "contour_demo"
    label: ClassVar[str] = "环切(示例插件)"
    description: ClassVar[str] = "示例插件：与内置环切同源，id 特意与内置策略区分开，方便照着改"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    ) + MOTION_PARAMETERS  # retract height and rapid feed are needed by every strategy, merge them

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        boundary = context.boundary
        distance = context.cutting_radius_mm

        rings: list[NDArray[np.float64]] = []
        while True:
            ring = offset_polygon(boundary, distance)
            if ring is None or abs(signed_area(ring)) < _MIN_RING_AREA_MM2:
                break
            rings.append(ring)
            distance += stepover
        if not rings:
            raise PlanningError(
                f"环切没有生成任何刀轨：刀具足迹半径 {context.tool.footprint_radius_mm:g} mm "
                "已经超过区域的内切半径，请减小刀具直径或扩大区域"
            )

        moves: list[Move] = []
        previous: np.ndarray | None = None
        for index, ring in enumerate(rings):
            sampled = resample_ring(ring, sample_step)
            closed = np.vstack([sampled, sampled[:1]])
            if index % 2 == 1:
                closed = closed[::-1]
            positions = context.to_positions(closed)
            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            else:
                moves.append(context.link_move(previous, positions[0]))
            moves.append(
                Move(
                    MoveKind.CUT,
                    positions,
                    context.feed_mm_per_min,
                    pass_index=index,
                    label=f"第 {index + 1} 环",
                )
            )
            previous = positions[-1]
        moves.append(context.retract_move_up(previous))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"环切(示例插件)：共 {len(rings)} 环，切宽 {stepover:g} mm",
                f"边界固定内缩一个刀具足迹半径（R{context.tool.footprint_radius_mm:g} mm），"
                f"安全高度 {context.safe_height_mm:g} mm、"
                f"快移 {context.rapid_feed_mm_per_min:g} mm/min",
            ),
        )


def describe_plugin() -> dict[str, Any]:
    """Self-check information for the curious."""

    return {"id": ContourPlanner.id, "label": ContourPlanner.label,
            "parameters": [item.key for item in ContourPlanner.parameters]}
