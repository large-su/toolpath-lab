"""Islands (holes) in a region: how a planner keeps away from them.

A region is its outer boundary **minus** its islands, and an island is material that is meant to stay.
Two things follow, and both are *pointwise distance tests* -- no polygon booleans are needed:

- the tool **centre** has to keep `cutting_radius` away from every island, exactly the rule the outline
  already imposes on it;
- the band between the outline and an island is machined from **both sides**: rings offset inwards from
  the outline and rings offset outwards from each island. Every ring keeps only the part it is closer
  to (`distance_to_own <= distance_to_other`), so the two families meet near the midline instead of
  cutting the same strip twice, and the pass spacing stays the stepover on both sides.

`offset_loops` only offsets inwards -- outward offsets need arcs at convex corners, which its author
deliberately left out -- so the outward rings are built here by sampling the island's own edges and
corner arcs. That is an approximation, and the trimming is what makes it safe: a ring point survives
only if it really is `radius`-clear of the *other* material and on the right side of the midline, so
whatever a sampled miter got wrong is dropped instead of being fed to the machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import ceil

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.planning.geometry2d import (
    distance_to_boundary,
    ensure_ccw,
    point_in_polygon,
    resample_ring,
    signed_area,
)

_EPS = 1e-9


def outward_ring(
    polygon: NDArray[np.float64], distance: float, *, chord_mm: float = 0.5
) -> NDArray[np.float64]:
    """A closed ring `distance` outside a counter-clockwise polygon, sampled edge by edge.

    The construction is the textbook one: every edge is shifted outwards along its own normal, and the
    gap left at a **convex** corner is filled with a discretised arc of radius `distance` around that
    vertex (which is exactly what an outward offset looks like there). A **reflex** corner gets the
    miter of the two shifted edges instead, which is exact while the offset stays simple and is filtered
    out by the callers once it does not.
    """

    if distance <= _EPS:
        return ensure_ccw(polygon).copy()
    poly = ensure_ccw(polygon)
    count = poly.shape[0]
    edge = np.roll(poly, -1, axis=0) - poly
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    direction = edge / np.where(length > _EPS, length, 1.0)
    # Outward normal: the inward one (left normal of a CCW polygon) points the other way.
    outward = np.column_stack((direction[:, 1], -direction[:, 0]))
    step_angle = max(1e-3, min(chord_mm / distance, 0.5))
    points: list[NDArray[np.float64]] = []
    # A reflex corner trims *both* neighbouring offset edges back to their intersection, so the edge
    # after it must not emit its own start point again.
    skip_start = False
    for index in range(count):
        following = (index + 1) % count
        start = poly[index] + outward[index] * distance
        end = poly[following] + outward[index] * distance
        if not skip_start:
            points.append(start)
        skip_start = False
        corner = poly[following]
        turn = float(
            direction[index, 0] * direction[following, 1]
            - direction[index, 1] * direction[following, 0]
        )
        if turn < -1e-9:  # reflex: only the miter of the two shifted edges belongs in the ring
            normals = outward[index] + outward[following]
            scale = 1.0 + float(np.dot(outward[index], outward[following]))
            if scale > 1e-6:
                points.append(corner + normals * (distance / scale))
                skip_start = True
        else:
            points.append(end)
            if turn > 1e-9:  # convex: walk the arc from this edge's normal to the next one
                first = float(np.arctan2(outward[index, 1], outward[index, 0]))
                last = float(np.arctan2(outward[following, 1], outward[following, 0]))
                # The ring is walked counter-clockwise like the polygon, so the arc angle grows.
                sweep = (last - first) % (2.0 * np.pi)
                steps = max(1, int(ceil(sweep / step_angle)))
                for step in range(1, steps):
                    angle = first + sweep * step / steps
                    points.append(
                        corner
                        + distance * np.array([np.cos(angle), np.sin(angle)], dtype=np.float64)
                    )
    ring = _drop_close(np.asarray(points, dtype=np.float64))
    return ring


def _drop_close(points: NDArray[np.float64], tolerance: float = 1e-6) -> NDArray[np.float64]:
    """Drop consecutive duplicates (the joins above emit each corner point twice at worst)."""

    if points.shape[0] < 2:
        return points
    keep = [0]
    for index in range(1, points.shape[0]):
        if float(np.linalg.norm(points[index] - points[keep[-1]])) > tolerance:
            keep.append(index)
    if len(keep) > 1 and float(np.linalg.norm(points[keep[0]] - points[keep[-1]])) <= tolerance:
        keep.pop()
    return points[keep]


def clean_ring(ring: NDArray[np.float64], *, min_area_mm2: float = 0.5) -> NDArray[np.float64] | None:
    """An outward ring that survived its own self-intersections, or None if nothing usable is left."""

    if ring.shape[0] < 3:
        return None
    if abs(signed_area(ring)) < min_area_mm2 or signed_area(ring) <= 0.0:
        return None
    return ring


def distance_to_polygons(
    points: NDArray[np.float64], polygons: Sequence[NDArray[np.float64]]
) -> NDArray[np.float64]:
    """Smallest distance from every point to any of the polygons (`inf` when there are none)."""

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if not polygons:
        return np.full(pts.shape[0], np.inf)
    best = np.full(pts.shape[0], np.inf)
    for polygon in polygons:
        best = np.minimum(best, distance_to_boundary(pts, np.asarray(polygon, dtype=np.float64)))
    return best


def trim_ring(
    ring: NDArray[np.float64],
    *,
    near: Sequence[NDArray[np.float64]],
    far: Sequence[NDArray[np.float64]],
    clearance_mm: float,
    midline_tolerance_mm: float = 0.05,
    clearance_tolerance_mm: float = 1e-6,
) -> list[NDArray[np.float64]]:
    """Split a closed ring into the runs that belong to this side of the band.

    A point is kept when it is `clearance_mm` away from the *other* material and no farther from its own
    than from that other material -- the midline rule. The two tolerances are deliberately different:
    the clearance is a **safety** limit (the cutter must not touch the island, so it gets a micrometre),
    while the midline is a bookkeeping rule between two families of rings and can be loose. With no
    islands in `far` this keeps the whole ring, which is what keeps the island-free toolpaths
    bit-identical.
    """

    pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    if pts.shape[0] < 3:
        return []
    if not far:
        return [pts]
    own = distance_to_polygons(pts, near)
    other = distance_to_polygons(pts, far)
    keep = (other >= clearance_mm - clearance_tolerance_mm) & (
        own <= other + midline_tolerance_mm
    )
    return split_runs(pts, keep, closed=True)


def split_runs(
    points: NDArray[np.float64], keep: NDArray[np.bool_], *, closed: bool
) -> list[NDArray[np.float64]]:
    """Runs of consecutive points where `keep` holds; a closed input is split across its seam."""

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    flags = np.asarray(keep, dtype=bool)
    if not closed:
        return _runs(pts, flags)
    if flags.all():
        return [pts]
    if not flags.any():
        return []
    # Start the walk at a dropped point so a run crossing the seam comes out as one piece.
    start = int(np.argmax(~flags))
    rolled_points = np.roll(pts, -start, axis=0)
    rolled_flags = np.roll(flags, -start)
    return _runs(rolled_points, rolled_flags, closed_ring=True)


def _runs(
    points: NDArray[np.float64], keep: NDArray[np.bool_], *, closed_ring: bool = False
) -> list[NDArray[np.float64]]:
    runs: list[NDArray[np.float64]] = []
    current: list[NDArray[np.float64]] = []
    for point, flag in zip(points, keep):
        if flag:
            current.append(point)
        else:
            if len(current) >= 2:
                runs.append(np.asarray(current, dtype=np.float64))
            current = []
    if closed_ring and current and keep[0]:
        # The run that reaches the rolled seam continues at the front of the array.
        head: list[NDArray[np.float64]] = []
        for point, flag in zip(points, keep):
            if not flag:
                break
            head.append(point)
        current.extend(head)
    if len(current) >= 2:
        runs.append(np.asarray(current, dtype=np.float64))
    return runs


def sample_line(
    first: NDArray[np.float64], second: NDArray[np.float64], step_mm: float
) -> NDArray[np.float64]:
    """Points along a straight segment, at most `step_mm` apart (both ends included)."""

    length = float(np.linalg.norm(second - first))
    count = 1 if length <= step_mm else max(2, int(ceil(length / max(step_mm, _EPS))) + 1)
    if count == 1:
        return np.vstack([first, second])
    ratios = np.linspace(0.0, 1.0, count)[:, None]
    return first + ratios * (second - first)


def connector_is_clear(
    first: NDArray[np.float64],
    second: NDArray[np.float64],
    islands: Sequence[NDArray[np.float64]],
    clearance_mm: float,
    *,
    step_mm: float,
    tolerance_mm: float = 0.05,
) -> bool:
    """Whether a straight link between two passes stays `clearance_mm` away from every island.

    A link is a cutting move, so linking across an island would machine into material that is meant to
    stay. The raster planner asks this before joining two passes and retracts instead when the answer
    is no; without islands it is always true and nothing changes.
    """

    if not islands:
        return True
    samples = sample_line(
        np.asarray(first, dtype=np.float64)[:2], np.asarray(second, dtype=np.float64)[:2], step_mm
    )
    return bool(np.all(distance_to_polygons(samples, islands) >= clearance_mm - tolerance_mm))


def resample_outward_ring(
    polygon: NDArray[np.float64],
    distance: float,
    step_mm: float,
    *,
    chord_mm: float = 0.5,
    safety_mm: float = 0.0,
) -> NDArray[np.float64] | None:
    """The outward ring of an island at `distance`, resampled and checked to be usable.

    `safety_mm` is added to the offset distance, and its reason is worth stating: the ring becomes a
    **polyline**, and the chords of a polyline cut *inside* the curve they approximate. Without the
    allowance the tool centres would dip a few micrometres (or, with a coarse sampling step, tenths of a
    millimetre) inside the clearance, which is exactly the gouge the clearance exists to prevent.
    `sagitta_mm` computes the amount to pass.
    """

    ring = outward_ring(polygon, distance + safety_mm, chord_mm=chord_mm)
    usable = clean_ring(ring)
    if usable is None:
        return None
    return resample_ring(usable, step_mm)


def sagitta_mm(scale_mm: float, step_mm: float) -> float:
    """How far the chord of a resampled ring of radius `scale_mm` dips inside it.

    `r * (1 - cos(step / 2r))`, which is `step^2 / (8r)` to within a few percent for the steps used
    here. Passing a *small* radius is the conservative choice, so callers use the tightest ring.
    """

    radius = max(abs(scale_mm), 1e-6)
    step = max(abs(step_mm), 0.0)
    if step <= 0.0:
        return 0.0
    return float(radius * (1.0 - np.cos(min(step / (2.0 * radius), np.pi / 2.0))))


def inside_any(point: NDArray[np.float64], polygons: Sequence[NDArray[np.float64]]) -> bool:
    """Whether a point falls inside any of the polygons (used to sanity check a sampled ring)."""

    if not polygons:
        return False
    probe = np.asarray(point, dtype=np.float64).reshape(1, 2)
    return any(bool(point_in_polygon(probe, np.asarray(poly, dtype=np.float64))[0]) for poly in polygons)
