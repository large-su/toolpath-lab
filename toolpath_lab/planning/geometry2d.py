"""刀路策略用到的平面多边形工具。

这里只有一个"重活"：scanline_intervals —— 把一条直线与多边形求交，按偶奇规则配对成
若干"内部区间"。栅格刀路就是靠它在一刀之内找出从哪里切到哪里，因此方形、圆形、
以及将来任何形状都能用同一套代码。

其他与偏置/重采样相关的几何在 examples/plugins/ 的示例策略里，需要时再抄进主程序。
"""

from __future__ import annotations

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
