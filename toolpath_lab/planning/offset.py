"""轮廓类策略共用的偏置与重采样几何。

环切与螺旋都需要两件事：

1. **把轮廓向内偏置一个刀具足迹半径**，得到"刀心可行区域"——刀心落在里面，刀就不会切出轮廓；
2. **把折线按等弧长重采样**，让相邻刀点大致等距，播放与导出的粒度才可控。

这两段几何原本只出现在 examples/plugins/contour_planner.py 里。按照
docs/extending.md 的约定——"将来有第二个策略需要它，再提到主程序"——现在螺旋
也要用，于是提升到这里；示例插件仍保持自包含，方便单独复制出去。

约定与 core 一致：逆时针多边形、坐标 (N, 2)、float64、单位毫米。
"""

from __future__ import annotations

from math import atan2, ceil, cos, pi, sin

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.planning.geometry2d import ensure_ccw, signed_area

_EPS = 1e-9


def inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """每条边的单位左法向（逆时针多边形时为内法向）。"""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    """每个点到多边形边界的最短距离 (N,)。"""

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


def resample_polyline(points: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """按等弧长重采样一条**开放**折线，两端点都保留。

    与 resample_ring 的区别只有一个：这里把首尾都当作真实节点，
    因此适合螺旋（起点在中心、终点在边界）这种不闭合的轨迹。
    """

    path = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if path.shape[0] < 2:
        return path
    steps = np.linalg.norm(np.diff(path, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(steps)))
    total = float(cumulative[-1])
    if total <= _EPS:
        return path
    count = max(2, int(ceil(total / max(step_mm, 1e-6))) + 1)
    targets = np.linspace(0.0, total, count)
    return np.column_stack(
        (
            np.interp(targets, cumulative, path[:, 0]),
            np.interp(targets, cumulative, path[:, 1]),
        )
    )
