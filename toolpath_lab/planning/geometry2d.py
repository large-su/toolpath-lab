"""刀路策略用到的平面多边形工具。

有两个"重活"：

1. scanline_intervals —— 把一条直线与多边形求交，按偶奇规则配对成若干"内部区间"。
   栅格刀路靠它在一刀之内找出从哪里切到哪里，因此方形、圆形、椭圆以及将来任何形状
   都能用同一套代码；
2. offset_polygon —— 把多边形整体向内（或向外）等距偏置。螺旋铣、环切这类"跟着轮廓
   走"的策略需要它：每退刀一圈就把轮廓再缩一个切宽。

偏置几何原先放在 examples/plugins/contour_planner.py 里作为示例，现在螺旋铣要正式使用，
因此提到本模块；示例插件保留自己的一份副本，两边互不影响。
"""

from __future__ import annotations

from math import atan2, ceil, cos, pi, sin
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

_EPS = 1e-9


class Interval(NamedTuple):
    """一条扫描线落在区域内部的区间。"""

    start: float
    end: float


def signed_area(polygon: NDArray[np.float64]) -> float:
    """多边形有向面积，逆时针为正。"""

    x = polygon[:, 0]
    y = polygon[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def ensure_ccw(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """去掉重复点并保证逆时针。"""

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
    """(x_min, x_max, y_min, y_max)。"""

    points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    return (
        float(points[:, 0].min()),
        float(points[:, 0].max()),
        float(points[:, 1].min()),
        float(points[:, 1].max()),
    )


def scanline_intervals(polygon: NDArray[np.float64], level: float) -> list[Interval]:
    """直线 y = level 落在多边形内部的区间，按 x 递增排列。

    用偶奇规则：凹多边形会得到多段，一条刀线因此可以变成几段独立刀轨。
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


# --------------------------------------------------------------------------
# 等距偏置：沿着轮廓一圈圈往里走时用得到（螺旋铣、环切）。
# --------------------------------------------------------------------------
def inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """每条边的单位左法向（逆时针多边形时即为内法向）。"""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    """每个点到多边形边界的最短距离。"""

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
    """逆时针多边形向内偏置 distance（负值向外），退化为空时返回 None。

    凸角用斜接（miter）交点，凹角插入圆弧接头——多边形内缩在凹角处本来就是圆弧；
    最后用"到原始边界的距离 >= 偏置量"过滤掉自交产生的顶点。
    """

    poly = ensure_ccw(polygon)
    if abs(distance) <= _EPS:
        return poly

    normals = inward_normals(poly)
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
        if (turn > 0.0) == (distance > 0.0) and dot > -0.999:
            moved = point + distance * (n_prev + n_next) / (1.0 + dot)
            if float(np.linalg.norm(moved - point)) <= max_miter * abs(distance):
                result.append((float(moved[0]), float(moved[1])))
                continue

        start_angle = atan2(float(n_prev[1]), float(n_prev[0]))
        end_angle = atan2(float(n_next[1]), float(n_next[0]))
        delta = (end_angle - start_angle + pi) % (2.0 * pi) - pi
        segments = max(1, int(ceil(abs(delta) * abs(distance) / max(chord_mm, 1e-6))))
        for step in range(segments + 1):
            angle = start_angle + delta * step / segments
            result.append(
                (
                    float(point[0] + distance * cos(angle)),
                    float(point[1] + distance * sin(angle)),
                )
            )

    candidate = np.array(result, dtype=np.float64)
    tolerance = max(1e-6, 1e-6 * abs(distance))
    filtered = candidate[distance_to_boundary(candidate, poly) >= abs(distance) - tolerance]
    if filtered.shape[0] < 3:
        return None
    gaps = np.linalg.norm(np.diff(np.vstack([filtered, filtered[:1]]), axis=0), axis=1)
    filtered = filtered[np.concatenate(([True], gaps[:-1] > _EPS))]
    if filtered.shape[0] < 3 or signed_area(filtered) <= 0.0:
        return None
    return filtered


def resample_ring(polygon: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """按等弧长重采样一个闭合环（不重复首点）。"""

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


#: 小于这个面积的圈就认为材料已经用尽。
MIN_RING_AREA_MM2 = 0.5


def polygon_inradius(polygon: NDArray[np.float64]) -> float:
    """多边形的内切半径估计，用来判断"中心还有没有材料"。

    做法：把重心到边界的距离乘一个收缩系数。重心到边界的距离本身就是一个合法偏置量
    ——它不会切出材料；乘以 0.9 只是留一点余量，让"刚好切到中心"归为正常收尾而不是失败。

    注意不能拿多边形自己的顶点去量：顶点就落在边界上，量出来恒为 0。
    """

    centroid = polygon.mean(axis=0)
    return 0.9 * float(distance_to_boundary(centroid[None, :], polygon)[0])


def collect_rings(
    boundary: NDArray[np.float64],
    start_distance: float,
    stepover: float,
    *,
    max_rings: int | None = None,
) -> tuple[list[NDArray[np.float64]], bool]:
    """从轮廓开始逐圈等距向内偏置，收集所有还剩材料的圈。

    返回 `(各圈, 是否因为几何自交而提前结束)`。这个区分很重要：

    - **材料用尽**：偏置量已经超过轮廓的内切半径，中心没有材料了，属于正常收尾；
    - **几何自交**：`offset_polygon` 用斜接处理凸角、圆弧处理凹角，偏置量一旦超过
      区域的圆角半径就会自交并返回 `None`。这意味着中间还有材料没切到，
      调用方应当 `warn()` 提醒用户，而不能当成"切完了"。

    `max_rings` 给定时最多收集这么多圈（用于"中心留残料圆"）。
    """

    inradius = polygon_inradius(boundary)
    distance = start_distance
    rings: list[NDArray[np.float64]] = []
    while True:
        exhausted = distance >= inradius
        ring = offset_polygon(boundary, distance)
        if ring is None or abs(signed_area(ring)) < MIN_RING_AREA_MM2:
            return rings, not exhausted
        rings.append(ring)
        if max_rings is not None and len(rings) >= max_rings:
            return rings, False
        distance += stepover
