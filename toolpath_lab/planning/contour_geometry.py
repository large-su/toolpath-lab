"""Bounded convex offset geometry shared by contour and continuous spiral paths."""

from math import ceil

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area

MAX_RINGS = 512
MAX_PATH_POINTS = 100_000
_EPS = 1e-8


def convex_boundary(boundary):
    """Reject concave boundaries rather than connecting disconnected offset loops."""
    polygon = ensure_ccw(boundary)
    if len(polygon) > 720:
        raise PlanningError("环切/螺旋选区边界过密，请简化到 720 个点以内")
    edges = np.roll(polygon, -1, axis=0) - polygon
    turns = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    if np.any(turns < -_EPS):
        raise PlanningError("环切和连续螺旋当前仅支持凸选区；凹多边形请使用栅格或交叉栅格")
    lengths = np.linalg.norm(edges, axis=1)
    normals = np.column_stack((-edges[:, 1], edges[:, 0])) / lengths[:, None]
    # Local turns alone do not exclude self-intersecting, star-shaped input.
    for point, normal in zip(polygon, normals):
        if np.any((polygon - point) @ normal < -_EPS):
            raise PlanningError("环切/螺旋需要简单凸多边形，不支持自交选区")
    return polygon, normals


def inward_offset(polygon, normals, distance):
    """Intersect all inward half-planes; clipping safely handles collapsing edges."""
    result = polygon.copy()
    for origin, normal in zip(polygon, normals):
        distances = (result - origin) @ normal - distance
        inside = distances >= -_EPS
        if inside.all():
            continue
        if not inside.any():
            return None
        ends = np.roll(result, -1, axis=0)
        end_distances = np.roll(distances, -1)
        crossing = inside != np.roll(inside, -1)
        denominator = distances - end_distances
        fractions = np.divide(distances, denominator, out=np.zeros_like(distances),
                              where=np.abs(denominator) > 1e-20)
        intersections = result + fractions[:, None] * (ends - result)
        candidates = np.stack((intersections, ends), axis=1).reshape(-1, 2)
        keep = np.column_stack((crossing, np.roll(inside, -1))).ravel()
        result = candidates[keep]
        if len(result) < 3:
            return None
    if len(result) < 3 or signed_area(result) < 1e-7:
        return None
    gaps = np.linalg.norm(result - np.roll(result, 1, axis=0), axis=1)
    result = result[gaps > _EPS]
    return result if len(result) >= 3 else None


def offset_rings(boundary, start_distance, stepover):
    """Produce nested offsets without unbounded loops or inverted polygons."""
    polygon, normals = convex_boundary(boundary)
    rings = []
    for index in range(MAX_RINGS + 1):
        ring = inward_offset(polygon, normals, start_distance + index * stepover)
        if ring is None:
            break
        if index == MAX_RINGS:
            raise PlanningError("偏置圈数超过 512，请增大切宽或缩小区域")
        if rings:
            anchor = int(np.argmin(np.linalg.norm(ring - rings[-1][0], axis=1)))
            ring = np.roll(ring, -anchor, axis=0)
        rings.append(ring)
    if not rings:
        raise PlanningError("刀具足迹相对选区过大，无法生成环切/螺旋；请减小刀径或扩大区域")
    return rings


def sample_ring(ring, step, clockwise=False):
    """Sample every edge and retain exact corners, including the closing point."""
    if clockwise:
        ring = np.vstack((ring[:1], ring[:0:-1]))
    points = []
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        count = max(1, ceil(float(np.linalg.norm(b - a)) / step))
        if len(points) + count + 1 > MAX_PATH_POINTS:
            raise PlanningError("单圈刀点超过预算，请增大采样步长")
        points.extend(a + (b - a) * (i / count) for i in range(count))
    points.append(points[0])
    return np.asarray(points)


def _ring_clock(ring):
    closed = np.vstack((ring, ring[:1]))
    lengths = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    clock = np.concatenate(([0.0], np.cumsum(lengths)))
    return closed, clock / clock[-1], float(clock[-1])


def spiral_turn(outer, inner, step, clockwise=False):
    """Blend two nested loops over one revolution, with an exact shared seam.

    Arc-length correspondence avoids pairing unrelated vertex indices when
    an offset has fewer edges. Both rings' corners are included in the clock.
    """
    if clockwise:
        outer = np.vstack((outer[:1], outer[:0:-1]))
        inner = np.vstack((inner[:1], inner[:0:-1]))
    a, ta, la = _ring_clock(outer)
    b, tb, lb = _ring_clock(inner)
    count = max(3, ceil(max(la, lb) / step))
    if count > MAX_PATH_POINTS:
        raise PlanningError("螺旋刀点超过预算，请增大采样步长")
    targets = np.unique(np.concatenate((np.linspace(0, 1, count + 1), ta, tb)))
    pa = np.column_stack([np.interp(targets, ta, a[:, axis]) for axis in (0, 1)])
    pb = np.column_stack([np.interp(targets, tb, b[:, axis]) for axis in (0, 1)])
    return pa * (1 - targets[:, None]) + pb * targets[:, None]
