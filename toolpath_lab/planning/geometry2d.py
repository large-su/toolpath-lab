"""刀路策略用到的平面多边形工具。

这里只有一个"重活"：scanline_intervals —— 把一条直线与多边形求交，按偶奇规则配对成
若干"内部区间"。栅格刀路就是靠它在一刀之内找出从哪里切到哪里，因此方形、圆形、
以及将来任何形状都能用同一套代码。

其他与偏置/重采样相关的几何在 examples/plugins/ 的示例策略里，需要时再抄进主程序。
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


# ---------------------------------------------------------------------------
# 偏置几何：环切与螺旋刀路都要用，所以从 examples/plugins 提到这里当公共工具。
# ---------------------------------------------------------------------------
def inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """每条边的单位左法向（逆时针多边形时为内法向）。"""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    """点到多边形边界的最短距离（逐点）。"""

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
    """逆时针多边形向内偏置 `distance`（负值向外），退化为空时返回 None。

    凸角用斜接（miter）交点；凹角插入圆弧接头——多边形内缩在凹角处本来就是圆弧；
    最后用"到原始边界的距离 >= 偏置量"过滤掉自交产生的顶点，并丢掉退化结果。
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


def points_inside_polygon(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.bool_]:
    """向量化的点在多边形内判定（射线法，奇偶规则，边界算内侧）。"""

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    poly = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    x = pts[:, 0][:, None]
    y = pts[:, 1][:, None]
    x0 = poly[:, 0][None, :]
    y0 = poly[:, 1][None, :]
    x1 = np.roll(poly[:, 0], -1)[None, :]
    y1 = np.roll(poly[:, 1], -1)[None, :]

    # 边是否跨过射线 y = y_i（用半开区间避免顶点重复计数）
    crosses = ((y0 > y) != (y1 > y))
    with np.errstate(divide="ignore", invalid="ignore"):
        x_cross = x0 + (y - y0) * (x1 - x0) / np.where(y1 - y0 == 0.0, 1e-12, y1 - y0)
    to_right = crosses & (x_cross > x)
    return (np.count_nonzero(to_right, axis=1) % 2) == 1
