"""Planar polygon helpers used by the toolpath strategies.

Two heavy lifting jobs live here:

- `scanline_intervals`: intersect a straight line with a polygon and pair the crossings by the
  even-odd rule into "inside" intervals. The raster strategy uses it to find where a pass starts
  and ends, so squares, circles, U shapes and any future shape share one code path.
- `offset_loops`: offset a polygon inwards and keep **every loop a layer may produce**. The
  contour strategy walks inwards ring by ring; once a narrow neck is eaten away by the offset the
  shape splits into several pieces, and each piece has to come out as its own loop.

Inward offsetting (erosion) happens in four steps:

1. **Primitives**: shift every edge inwards by the offset distance to get a segment, leaving a
   small extension at both ends (see `_MARGIN_FACTOR` -- a miter point almost always falls inside
   the segment, so the extension is only numerical slack); then add a circular arc of radius equal
   to the offset distance at every **reflex** corner and discretise it into chords. Convex corners
   need no arc: in an erosion the convex corner stays sharp and the miter point falls out of the
   intersection of the two shifted lines. "Edges plus reflex arcs" is enough because the nearest
   boundary point of any interior point either lies on an edge or on a reflex vertex -- a convex
   vertex is never the nearest point (it is no farther from the two adjacent edges than from
   itself).
2. **Split at intersections**: cut every segment at all of its crossings, sharing the intersection
   coordinates between both sides, so "miter point" and "point where an arc joins" become shared
   nodes. This is what makes chaining into loops possible at all.
3. **Keep the equidistant pieces**: only keep sub-segments that are "inside the polygon and at a
   distance ~ offset distance from the boundary". Anything further in is interior, not offset
   boundary; the extended parts are invalid and get filtered out here.
4. **Chain into loops**: merge the endpoints into nodes and chain by "smallest turn" -- the offset
   curve goes straight through such a node.

Three "cheap before expensive" prunings keep this fast. All of them are provably result
preserving, and `PrefilterTests` in `tests/test_geometry2d.py` pins them bit for bit:

- only compare segment pairs whose **bounding boxes share a grid cell** (intersecting segments
  always overlap in their boxes, so nothing is missed);
- when testing "distance to the boundary ~ offset distance", first drop edges via their
  **supporting lines** (the distance to a segment is never smaller than the distance to its line,
  so the nearest edge always survives);
- drop **invalid crossings** before splitting (validity only changes at valid crossings, so the two
  pieces cut at an invalid crossing have the same status).

Large offsets are the worst case: the shifted segments are longer than the offset perimeter and
cross each other heavily (an 80 mm circle discretised into 180 edges offset by 33 produces 1800
crossings and 3780 sub-segments, only 180 of which are valid). The three prunings bring one contour
plan of that circle from 0.76 s down to 0.22 s with bit-identical output.

Offsetting and resampling used to live in the contour strategy (examples/plugins/contour_planner.py
keeps a single-loop copy of that old version); following the convention "copy it into the main
program when you need it", they now live here.
"""

from __future__ import annotations

from collections.abc import Callable
from math import atan2, ceil, cos, floor, pi, sin
from typing import Any, NamedTuple

import numpy as np
from numpy.typing import NDArray

_EPS = 1e-9
#: Segments shorter than this (mm) are dropped, so quantisation noise cannot create zero-length
#: primitives.
_MIN_PIECE_MM = 1e-9
#: Largest central angle (radians) allowed when discretising a reflex arc: chords never get
#: absurdly short even for small radii.
_MAX_ARC_STEP_RAD = 0.35
#: Below this edge count the crossings are not filtered: simple shapes have few crossings anyway,
#: and one filtering pass costs more than it saves.
_CROSSING_FILTER_MIN_EDGES = 32
#: How far the shifted segments are extended at both ends, as a multiple of the offset distance.
#: The extension is **numerical slack**, not a correctness switch: at a convex corner whose miter
#: point falls outside the shifted segment, the two adjacent edges are necessarily shorter than the
#: required reach d*tan(turn/2), so the material there is at most 2L*sin(interior/2) < 2d wide --
#: narrower than the tool, fully eroded away, and that edge should not appear on the offset
#: boundary at all. A small margin therefore suffices.
_MARGIN_FACTOR = 4.0
#: Quantisation grid (mm) for nodes: endpoints are snapped to intersections so their coordinates
#: already match exactly; this only absorbs floating point noise.
_NODE_GRID_MM = 1e-6
#: Tolerance for the equidistant line of a straight segment, relative to the offset distance. A
#: straight segment has no discretisation error, so the tolerance only absorbs floating point
#: noise; too loose a tolerance also accepts the bit of extension outside a miter point and makes
#: the chaining take a wrong turn.
_LINE_TOLERANCE_RATIO = 1e-7


class Interval(NamedTuple):
    """One scanline interval lying inside the region."""

    start: float
    end: float


def signed_area(polygon: NDArray[np.float64]) -> float:
    """Signed polygon area, positive for counter-clockwise winding."""

    x = polygon[:, 0]
    y = polygon[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def ensure_ccw(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """Drop repeated points and make the winding counter-clockwise."""

    points = np.asarray(polygon, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 3:
        raise ValueError("多边形必须是 (N, 2) 且 N >= 3")
    if np.linalg.norm(points[-1] - points[0]) <= _EPS:
        points = points[:-1]
    keep = np.ones(points.shape[0], dtype=bool)
    gaps = np.linalg.norm(np.diff(np.vstack([points, points[:1]]), axis=0), axis=1)
    keep[1:] = gaps[:-1] > _EPS
    points = points[keep]
    if points.shape[0] < 3:
        raise ValueError("去掉重复点后多边形退化了")
    if signed_area(points) < 0.0:
        points = points[::-1]
    return np.ascontiguousarray(points)


def bounding_box(polygon: NDArray[np.float64]) -> tuple[float, float, float, float]:
    """Return (x_min, x_max, y_min, y_max)."""

    points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    return (
        float(points[:, 0].min()),
        float(points[:, 0].max()),
        float(points[:, 1].min()),
        float(points[:, 1].max()),
    )


def region_inside_mask(
    region: Any, xs: NDArray[np.float64], ys: NDArray[np.float64]
) -> NDArray[np.bool_]:
    """Cell centres that belong to the region: inside `boundary()`, outside every island.

    This is the single answer to "is this cell part of the region" -- coverage and the material removal
    sweep both ask it, so both agree the moment a region has islands (holes). An island's material is
    *meant* to stay, so counting it as region would report a permanently uncut hole; a solid region
    (every shape without islands) gets exactly the mask those two modules always computed themselves.
    """

    mask = np.zeros((ys.shape[0], xs.shape[0]), dtype=bool)
    for row, y in enumerate(ys):
        for interval in scanline_intervals(ensure_ccw(region.boundary()), float(y)):
            mask[row] |= (xs >= interval.start) & (xs <= interval.end)
    for island in region.islands():
        polygon = ensure_ccw(island)
        for row, y in enumerate(ys):
            for interval in scanline_intervals(polygon, float(y)):
                mask[row] &= ~((xs >= interval.start) & (xs <= interval.end))
    return mask


def scanline_intervals(polygon: NDArray[np.float64], level: float) -> list[Interval]:
    """Intervals of the line y = level that lie inside the polygon, ordered by x.

    Uses the even-odd rule, so a concave polygon yields several intervals and one pass can become
    several independent toolpaths.
    """

    x0 = polygon[:, 0]
    y0 = polygon[:, 1]
    x1 = np.roll(x0, -1)
    y1 = np.roll(y0, -1)
    crosses = (y0 - level) * (y1 - level) <= 0.0
    valid = crosses & (np.abs(y1 - y0) > _EPS)
    if not np.any(valid):
        return []
    fraction = (level - y0[valid]) / (y1[valid] - y0[valid])
    xs = np.sort(x0[valid] + fraction * (x1[valid] - x0[valid]))
    if xs.size < 2:
        return []
    pairs = xs[: xs.size - (xs.size % 2)].reshape(-1, 2)
    return [Interval(float(a), float(b)) for a, b in pairs if b - a > _EPS]


# ---------------------------------------------------------------- polygon helpers
def inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """Unit left normal of every edge (points inwards for a counter-clockwise polygon)."""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def boundary_edges(
    polygon: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Pre-compute each edge's start, direction vector and squared length for batch distance work."""

    start = np.asarray(polygon, dtype=np.float64)
    edge = np.roll(start, -1, axis=0) - start
    squared = np.maximum(np.sum(edge * edge, axis=1), _EPS)
    return start, edge, squared


def distance_to_edges(
    points: NDArray[np.float64],
    edges: tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]],
) -> NDArray[np.float64]:
    """Distance from every point to the nearest polygon edge (edges pre-computed)."""

    start, edge, squared = edges
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)[:, None, :]
    t = np.clip(np.sum((pts - start[None]) * edge[None], axis=2) / squared[None], 0.0, 1.0)
    closest = start[None] + t[..., None] * edge[None]
    return np.linalg.norm(pts - closest, axis=2).min(axis=1)


def distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Distance from every point to the nearest polygon edge."""

    return distance_to_edges(points, boundary_edges(polygon))


def point_in_polygon(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.bool_]:
    """Ray casting test per point.

    Points exactly on the boundary have an undefined result (both answers occur); callers must not
    rely on it.
    """

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    x = pts[:, 0][:, None]
    y = pts[:, 1][:, None]
    x0 = polygon[:, 0][None, :]
    y0 = polygon[:, 1][None, :]
    x1 = np.roll(polygon[:, 0], -1)[None, :]
    y1 = np.roll(polygon[:, 1], -1)[None, :]
    straddles = (y0 > y) != (y1 > y)
    height = np.where(np.abs(y1 - y0) > _EPS, y1 - y0, 1.0)
    crossing_x = x0 + (y - y0) * (x1 - x0) / height
    return (np.sum(straddles & (crossing_x > x), axis=1) % 2) == 1


def on_offset_boundary(
    points: NDArray[np.float64],
    polygon: NDArray[np.float64],
    distance: float,
    *,
    tolerance: float,
) -> NDArray[np.bool_]:
    """Whether points lie on the offset boundary: inside the polygon and about `distance` away."""

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    inside = point_in_polygon(pts, polygon)
    gaps = np.abs(distance_to_boundary(pts, polygon) - distance)
    return inside & (gaps <= tolerance)


# ---------------------------------------------------------------- inward offset
def offset_primitives(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    margin_factor: float = _MARGIN_FACTOR,
) -> list[tuple[NDArray[np.float64], NDArray[np.float64], bool]]:
    """Offset primitives: `(start, end, is_arc_chord)`.

    Every edge is shifted inwards by the offset distance; at a convex corner both ends are extended
    by `distance * tan(turn / 2)` (capped at `_MARGIN_FACTOR` times the distance). That extension is
    numerical slack only: a miter point is the intersection of two adjacent shifted lines, it needs
    the extension only when it falls outside the segment, and such a corner is necessarily narrower
    than the tool and eroded away completely (see `_MARGIN_FACTOR`). The reach is computed per
    vertex rather than extending everything by a large fixed amount, because a uniform extension
    makes curved shapes sprout a pile of useless crossings. Reflex ends need no extension: the arc
    join covers them, and the arc endpoints land exactly on the shifted segment endpoints.
    """

    count = polygon.shape[0]
    normals = inward_normals(polygon)
    previous_normal = np.roll(normals, 1, axis=0)
    previous_edge = polygon - np.roll(polygon, 1, axis=0)
    next_edge = np.roll(polygon, -1, axis=0) - polygon
    cross = previous_edge[:, 0] * next_edge[:, 1] - previous_edge[:, 1] * next_edge[:, 0]
    dot = np.sum(previous_edge * next_edge, axis=1)
    turn = np.arctan2(cross, dot)  # signed turn: positive = left turn = convex corner
    limit = margin_factor * distance
    reach = np.where(
        turn > 0.0,
        np.minimum(distance * np.tan(turn / 2.0), limit),
        0.0,
    )

    pieces: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]] = []
    for index in range(count):
        start = polygon[index] + distance * normals[index]
        end = polygon[(index + 1) % count] + distance * normals[index]
        span = end - start
        length = float(np.linalg.norm(span))
        if length <= _MIN_PIECE_MM:
            continue
        direction = span / length
        pieces.append(
            (
                start - float(reach[index]) * direction,
                end + float(reach[(index + 1) % count]) * direction,
                False,
            )
        )

    for index in range(count):
        if turn[index] >= 0.0:
            # Convex corner: the miter point comes out of the two shifted lines, no arc needed.
            continue
        center = polygon[index]
        n_prev = previous_normal[index]
        n_next = normals[index]
        start_angle = atan2(float(n_prev[1]), float(n_prev[0]))
        end_angle = atan2(float(n_next[1]), float(n_next[0]))
        delta = (end_angle - start_angle + pi) % (2.0 * pi) - pi
        segments = max(
            1,
            int(ceil(abs(delta) * distance / max(chord_mm, 1e-6))),
            int(ceil(abs(delta) / _MAX_ARC_STEP_RAD)),
        )
        previous_point = center + distance * n_prev
        for step in range(1, segments + 1):
            angle = start_angle + delta * step / segments
            point = center + distance * np.array([cos(angle), sin(angle)])
            pieces.append((previous_point, point, True))
            previous_point = point
    return pieces


def snap_to_nodes(points: list[NDArray[np.float64]], snap: float):
    """Return a function that snaps a point to the nearest given node within `snap`, else returns it.

    An arc is discretised into chords, so its endpoints may differ from "the intersection of the arc
    with the neighbouring segment" by a few 1e-4 mm. Without snapping, those chains never join
    (the difference is tiny numerically, but chaining relies on exactly equal coordinates).

    The lookup is per point, so buckets hold plain tuples and comparisons use plain floats: a curved
    shape snaps thousands of points at a time, and element-wise numpy calls would cost more than the
    arithmetic itself.
    """

    cell = max(snap, _EPS)
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for point in points:
        x, y = float(point[0]), float(point[1])
        buckets.setdefault((int(floor(x / cell)), int(floor(y / cell))), []).append((x, y))

    def snap_point(point: NDArray[np.float64]) -> NDArray[np.float64]:
        x, y = float(point[0]), float(point[1])
        key_x, key_y = int(floor(x / cell)), int(floor(y / cell))
        best: tuple[float, float] | None = None
        best_squared = snap * snap
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for candidate in buckets.get((key_x + dx, key_y + dy), ()):
                    squared = (candidate[0] - x) ** 2 + (candidate[1] - y) ** 2
                    if squared <= best_squared:
                        best, best_squared = candidate, squared
        return point if best is None else np.array(best, dtype=np.float64)

    return snap_point


def split_at_intersections(
    pieces: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]],
    *,
    snap: float = 0.0,
    keep_crossing: Callable[[NDArray[np.float64]], NDArray[np.bool_]] | None = None,
) -> list[tuple[NDArray[np.float64], NDArray[np.float64], bool]]:
    """Cut every segment at its intersections; both sides share the coordinates they meet at.

    Passing `snap` snaps endpoints to the nearest intersection within that distance: the leftover
    between an arc discretisation point and an intersection only disappears this way, otherwise the
    chain breaks there.

    `keep_crossing` filters crossings before splitting, see `_crossing_filter`: the transition
    between valid and invalid can only happen at a valid crossing, and the two pieces cut at an
    invalid crossing have the same status, so cutting there changes nothing. Curved shapes have a
    huge number of crossings at large offsets (an 80 mm circle offset by 33 has 1800 of them), and
    filtering first removes an order of magnitude of sub-segments.

    Only segment pairs whose **bounding boxes share a grid cell** are compared: intersecting segments
    always overlap in their boxes, so bucketing can only produce extra candidates, never miss one.
    """

    count = len(pieces)
    if count < 2:
        return list(pieces)
    starts = np.array([piece[0] for piece in pieces], dtype=np.float64)
    ends = np.array([piece[1] for piece in pieces], dtype=np.float64)
    chords = [piece[2] for piece in pieces]
    spans = ends - starts

    i_flat, j_flat = _candidate_pairs(starts, ends)
    cuts: list[list[tuple[float, NDArray[np.float64]]]] = [[] for _ in range(count)]
    nodes: list[NDArray[np.float64]] = []
    if i_flat.size:
        p = starts[i_flat]
        r = spans[i_flat]
        q = starts[j_flat]
        s = spans[j_flat]
        denominator = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
        usable = np.abs(denominator) > _EPS
        safe = np.where(usable, denominator, 1.0)
        delta = q - p
        t = (delta[:, 0] * s[:, 1] - delta[:, 1] * s[:, 0]) / safe
        u = (delta[:, 0] * r[:, 1] - delta[:, 1] * r[:, 0]) / safe
        crossing = usable & (t > _EPS) & (t < 1.0 - _EPS) & (u > _EPS) & (u < 1.0 - _EPS)
        if keep_crossing is not None and crossing.any():
            points = 0.5 * ((p + t[:, None] * r) + (q + u[:, None] * s))
            crossing &= keep_crossing(points)
        for i, j, ti, uj in zip(
            i_flat[crossing], j_flat[crossing], t[crossing], u[crossing]
        ):
            point = 0.5 * (
                (starts[i] + ti * spans[i]) + (starts[j] + uj * spans[j])
            )
            cuts[int(i)].append((float(ti), point))
            cuts[int(j)].append((float(uj), point))
            nodes.append(point)

    snap_point = snap_to_nodes(nodes, snap) if snap > 0.0 and nodes else (lambda point: point)

    subsegments: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]] = []
    for index in range(count):
        ordered = sorted(cuts[index], key=lambda item: item[0])
        points: list[NDArray[np.float64]] = [snap_point(starts[index])]
        for parameter, point in ordered:
            if parameter <= _EPS or parameter >= 1.0 - _EPS:
                continue
            if _squared_distance(point, points[-1]) <= _MIN_PIECE_MM**2:
                continue
            points.append(point)
        points.append(snap_point(ends[index]))
        for begin, finish in zip(points, points[1:]):
            if _squared_distance(finish, begin) > _MIN_PIECE_MM**2:
                subsegments.append((begin, finish, chords[index]))
    return subsegments


def _squared_distance(
    first: NDArray[np.float64], second: NDArray[np.float64]
) -> float:
    """Squared distance between two points: comparing lengths piece by piece only needs the square,
    which saves thousands of numpy calls."""

    delta_x = float(first[0]) - float(second[0])
    delta_y = float(first[1]) - float(second[1])
    return delta_x * delta_x + delta_y * delta_y


def _candidate_pairs(
    starts: NDArray[np.float64], ends: NDArray[np.float64]
) -> tuple[NDArray[np.intp], NDArray[np.intp]]:
    """Bucket by bounding box and return the possibly intersecting pairs (i < j, sorted by (i, j)).

    Bucketing can only produce **extra** candidates, never miss a real crossing: two intersecting
    segments necessarily overlap in their boxes and therefore share at least one cell. Sorting by
    (i, j) keeps the crossing order -- and with it the snapping order -- identical to the
    "compare everything pairwise" version.
    """

    count = starts.shape[0]
    lower = np.minimum(starts, ends)
    upper = np.maximum(starts, ends)
    span = np.maximum(upper.max(axis=0) - lower.min(axis=0), _EPS)
    cells = max(1, int(np.sqrt(count)))
    cell_size = span / cells

    buckets: dict[tuple[int, int], list[int]] = {}
    for index in range(count):
        first_cell = np.floor((lower[index] - lower.min(axis=0)) / cell_size).astype(int)
        last_cell = np.floor((upper[index] - lower.min(axis=0)) / cell_size).astype(int)
        for cx in range(first_cell[0], last_cell[0] + 1):
            for cy in range(first_cell[1], last_cell[1] + 1):
                buckets.setdefault((cx, cy), []).append(index)

    found: list[NDArray[np.int64]] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        rows = np.array(members, dtype=np.int64)
        left, right = np.triu_indices(rows.size, 1)
        # Encode as i * count + j and deduplicate: one cell can hold a hundred segments (at large
        # offsets all shifted segments shrink towards the centre), where adding them to a Python set
        # one by one becomes the hot spot while numpy deduplication is a single sort.
        found.append(np.minimum(rows[left], rows[right]) * count
                     + np.maximum(rows[left], rows[right]))
    if not found:
        return np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp)
    encoded = np.unique(np.concatenate(found))
    return (encoded // count).astype(np.intp), (encoded % count).astype(np.intp)


def _crossing_filter(
    polygon: NDArray[np.float64],
    edges: tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]],
    distance: float,
    slack: float,
):
    """Return a predicate telling whether a crossing lies on the offset boundary, to pre-filter.

    Why filtering first is sound: the validity of a sub-segment is uniform along it, and the status
    can only change at a crossing; if both pieces at a crossing are valid then the crossing itself is
    valid too (distance is continuous). So an **invalid** crossing has the same status on both sides
    and cutting there or not makes no difference -- only valid crossings are real transitions.

    The looser of the two tolerances is used here: keeping a few extra crossings only costs a few
    extra tests, while dropping a real transition would change the result.
    """

    def keep(points: NDArray[np.float64]) -> NDArray[np.bool_]:
        gaps = np.abs(_nearest_edge_distance(points, edges, distance, slack) - distance)
        near = gaps <= slack
        if not near.any():
            return near
        kept = np.zeros(points.shape[0], dtype=bool)
        survivors = np.flatnonzero(near)
        kept[survivors] = point_in_polygon(points[survivors], polygon)
        return kept

    return keep


def _nearest_edge_distance(
    points: NDArray[np.float64],
    edges: tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]],
    target: float,
    slack: float,
    *,
    chunk: int = 64,
) -> NDArray[np.float64]:
    """Distance from every point to the nearest edge, after dropping edges via their supporting lines.

    The distance to a segment is never smaller than the distance to the line it lies on, so any edge
    whose segment distance is ~ target also has a line distance <= target + slack. Computing the
    cheap distance (line) before the expensive one (segment) gives the same result as computing
    everything: the true nearest edge of every point is among the candidates, and extra candidates
    only move the minimum closer to its true value.

    Work happens in **small blocks**: the candidate columns are the union over the whole block, and a
    large block makes that union degenerate into all edges, wasting the whole saving.
    """

    start, edge, squared = edges
    norm = np.sqrt(squared)[None, :]
    distances = np.full(points.shape[0], np.inf)
    for begin in range(0, points.shape[0], chunk):
        finish = min(begin + chunk, points.shape[0])
        block = points[begin:finish]
        line_gap = np.abs(
            (block[:, 0][:, None] - start[None, :, 0]) * edge[None, :, 1]
            - (block[:, 1][:, None] - start[None, :, 1]) * edge[None, :, 0]
        ) / norm
        columns = np.flatnonzero((line_gap <= target + slack).any(axis=0))
        if columns.size == 0:
            continue
        distances[begin:finish] = distance_to_edges(
            block, (start[columns], edge[columns], squared[columns])
        )
    return distances


def _valid_subsegments(
    subsegments: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]],
    polygon: NDArray[np.float64],
    distance: float,
    *,
    line_tolerance: float,
    chord_tolerance: float,
) -> list[tuple[NDArray[np.float64], NDArray[np.float64]]]:
    """Keep only the sub-segments that really lie on the offset boundary.

    Straight segments and arc chords use different tolerances: the midpoint of a chord is naturally
    one sagitta closer than the radius and the tolerance has to cover that, while a straight segment
    has no such error and needs a tight tolerance -- otherwise the bit of extension outside a miter
    point is accepted as boundary too.

    Testing the midpoint of every sub-segment is enough: sub-segments are cut at crossings, so a
    whole piece is either on the equidistant line or not; for an arc chord the midpoint deviates the
    most (the sagitta), so if it passes the whole chord passes.

    A curved shape cuts out thousands of sub-segments, and most of them actually lie closer to some
    other edge (at large offsets the shifted segments are longer than the offset perimeter and cross
    each other). So the test is batched, the edge vectors are computed once, and the edges are
    pre-filtered by their supporting lines (see `_nearest_edge_distance`).
    """

    if not subsegments:
        return []
    edges = boundary_edges(polygon)
    starts = np.array([item[0] for item in subsegments], dtype=np.float64)
    ends = np.array([item[1] for item in subsegments], dtype=np.float64)
    tolerances = np.where(
        np.array([item[2] for item in subsegments]),
        chord_tolerance,
        line_tolerance,
    )
    midpoints = 0.5 * (starts + ends)
    slack = max(line_tolerance, chord_tolerance)

    keep = np.zeros(len(subsegments), dtype=bool)
    chunk = 512
    for begin in range(0, len(subsegments), chunk):
        finish = min(begin + chunk, len(subsegments))
        block = midpoints[begin:finish]
        gaps = np.abs(_nearest_edge_distance(block, edges, distance, slack) - distance)
        near = gaps <= tolerances[begin:finish]
        if not near.any():
            continue
        # Distance first (cheap, drops more than nine out of ten), then the inside test for the
        # survivors: a point outside the region can be exactly d away from the boundary too, so the
        # test cannot be skipped, only skipped for most points.
        inside = np.zeros(finish - begin, dtype=bool)
        survivors = np.flatnonzero(near)
        inside[survivors] = point_in_polygon(block[survivors], polygon)
        keep[begin:finish] = inside

    return [
        (start, end)
        for (start, end, _chord), selected in zip(subsegments, keep.tolist())
        if selected
    ]


def _node_labels(points: NDArray[np.float64], grid: float = _NODE_GRID_MM) -> NDArray[np.int64]:
    """Group points into nodes on a grid and return each point's node id.

    Both sides of an intersection share their coordinates exactly, so this only lumps floating point
    noise together; using a dict instead of comparing all pairs keeps curved shapes (hundreds of
    segments) from degenerating into O(n^2).
    """

    keys = np.round(points / grid).astype(np.int64)
    mapping: dict[tuple[int, int], int] = {}
    labels = np.empty(points.shape[0], dtype=np.int64)
    for index, key in enumerate(map(tuple, keys.tolist())):
        labels[index] = mapping.setdefault(key, len(mapping))
    return labels


def _chain_loops(
    pieces: list[tuple[NDArray[np.float64], NDArray[np.float64]]],
) -> list[NDArray[np.float64]]:
    """Chain sub-segments into closed loops.

    When a node offers several ways out, take the one with the **smallest turn** -- the offset curve
    goes straight through such a node. Chains that do not close are dropped: they mean degenerate
    geometry (a sub-segment outside the offset boundary, or a region narrower than the tool that got
    eroded away completely), and such a chain should not appear in a toolpath.
    """

    if not pieces:
        return []
    starts = np.array([piece[0] for piece in pieces], dtype=np.float64)
    ends = np.array([piece[1] for piece in pieces], dtype=np.float64)
    endpoints = np.vstack([starts, ends])
    labels = _node_labels(endpoints)
    start_labels = labels[: len(pieces)]
    end_labels = labels[len(pieces):]

    # Representative point of a node (mean coordinate) plus unit direction and length are computed
    # once: chaining is a per-piece loop and "smallest turn" compares directions, so recomputing them
    # inside the loop becomes the single hottest spot.
    _, inverse = np.unique(labels, return_inverse=True)
    groups = int(inverse.max()) + 1
    counts = np.bincount(inverse, minlength=groups)
    representatives = np.column_stack(
        (
            np.bincount(inverse, weights=endpoints[:, 0], minlength=groups) / counts,
            np.bincount(inverse, weights=endpoints[:, 1], minlength=groups) / counts,
        )
    )
    spans = ends - starts
    lengths = np.linalg.norm(spans, axis=1)
    directions = spans / np.maximum(lengths, _EPS)[:, None]

    outgoing: dict[int, list[int]] = {}
    for index, label in enumerate(start_labels):
        outgoing.setdefault(int(label), []).append(index)

    used = [False] * len(pieces)
    loops: list[NDArray[np.float64]] = []
    for seed in range(len(pieces)):
        if used[seed]:
            continue
        seed_label = int(start_labels[seed])
        visited: list[int] = []
        current: int | None = seed
        while current is not None:
            used[current] = True
            visited.append(int(start_labels[current]))
            tail = int(end_labels[current])
            if tail == seed_label and len(visited) >= 3:
                loops.append(representatives[np.array(visited, dtype=np.intp)])
                break
            current_piece = current
            current = None
            if lengths[current_piece] <= _MIN_PIECE_MM:
                continue
            direction = directions[current_piece]
            best_score = -2.0
            for candidate in outgoing.get(tail, ()):
                if used[candidate] or lengths[candidate] <= _MIN_PIECE_MM:
                    continue
                following = directions[candidate]
                score = float(
                    direction[0] * following[0] + direction[1] * following[1]
                )
                if score > best_score:
                    best_score, current = score, candidate
    return loops


def _drop_repeats(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Drop consecutive repeated points from a closed loop."""

    if points.shape[0] == 0:
        return points
    gaps = np.linalg.norm(np.diff(np.vstack([points, points[:1]]), axis=0), axis=1)
    keep = np.concatenate(([True], gaps[:-1] > _MIN_PIECE_MM))
    return points[keep]


def offset_loops(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    min_area_mm2: float = 0.5,
) -> list[NDArray[np.float64]]:
    """Offset a counter-clockwise polygon inwards by distance >= 0, returning **all** loops by area.

    When a narrow neck is eaten away the shape splits and several loops come out here; an empty list
    means nothing is left. Only inward offsetting is supported: offsetting outwards needs arcs at
    convex corners, which is a different piece of geometry, so a negative distance raises.
    """

    if distance < 0.0:
        raise ValueError("offset_loops 只支持向内偏置（distance >= 0）")
    poly = ensure_ccw(polygon)
    if distance <= _EPS:
        return [poly]

    # Reflex arcs are discretised into chords and a chord midpoint is one sagitta closer than the
    # radius, d*(1-cos(half step angle)); the tolerance has to cover that or the whole arc is
    # rejected as "not on the equidistant line".
    step_angle = min(chord_mm / distance, _MAX_ARC_STEP_RAD)
    sagitta = distance * (1.0 - cos(step_angle / 2.0))
    chord_tolerance = max(1e-6, 1e-6 * distance, 1.5 * sagitta)
    line_tolerance = max(1e-9, _LINE_TOLERANCE_RATIO * distance)

    primitives = offset_primitives(poly, distance, chord_mm=chord_mm)
    edges = boundary_edges(poly)
    slack = max(line_tolerance, chord_tolerance)
    pieces = _valid_subsegments(
        split_at_intersections(
            primitives,
            snap=chord_tolerance,
            keep_crossing=(
                _crossing_filter(poly, edges, distance, slack)
                if poly.shape[0] >= _CROSSING_FILTER_MIN_EDGES
                else None
            ),
        ),
        poly,
        distance,
        line_tolerance=line_tolerance,
        chord_tolerance=chord_tolerance,
    )

    loops: list[NDArray[np.float64]] = []
    for loop in _chain_loops(pieces):
        loop = _drop_repeats(loop)
        if loop.shape[0] < 3:
            continue
        area = signed_area(loop)
        if area <= 0.0 or area < min_area_mm2:
            continue
        loops.append(loop)
    loops.sort(key=lambda item: -abs(signed_area(item)))
    return loops


def offset_polygon(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    min_area_mm2: float = 0.5,
) -> NDArray[np.float64] | None:
    """Convenience entry point for a single loop: the largest one, or None if there is none."""

    loops = offset_loops(polygon, distance, chord_mm=chord_mm, min_area_mm2=min_area_mm2)
    return loops[0] if loops else None


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
