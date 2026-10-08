"""几何层共用的小工具。

约定（全项目一致）：

- 单位是毫米与秒；角度在 API 边界用度，核心内部用弧度；
- 右手系，Z 轴向上，加工平面是 XY 平面；
- 数组是 float64：单个向量 (3,)，折线 (N, 3)，平面多边形 (N, 2)。
"""

from __future__ import annotations

from math import cos, radians, sin
from typing import Any

import numpy as np
from numpy.typing import NDArray

EPS = 1e-9


def unit(vector: Any) -> NDArray[np.float64]:
    """返回 vector 方向的单位向量。"""

    array = np.asarray(vector, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm <= EPS:
        raise ValueError("向量退化（长度为零）")
    return array / norm


def rotation_2d(angle_deg: float) -> NDArray[np.float64]:
    """XY 平面内逆时针旋转 angle_deg 的旋转矩阵。"""

    angle = radians(angle_deg)
    return np.array(
        [[cos(angle), -sin(angle)], [sin(angle), cos(angle)]],
        dtype=np.float64,
    )


def direction_2d(angle_deg: float) -> NDArray[np.float64]:
    """从 +X 轴起算、角度为 angle_deg 的单位方向向量。"""

    angle = radians(angle_deg)
    return np.array([cos(angle), sin(angle)], dtype=np.float64)


def cumulative_lengths(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """折线的累计弧长，首元素为 0。"""

    array = np.asarray(points, dtype=np.float64)
    if array.shape[0] < 2:
        return np.zeros(array.shape[0], dtype=np.float64)
    steps = np.linalg.norm(np.diff(array, axis=0), axis=1)
    return np.concatenate(([0.0], np.cumsum(steps)))


def axis_angle_deg(first: Any, second: Any) -> float:
    """有向刀轴之间的夹角；刀尖到刀柄的方向不能按等价正负向量翻转。"""

    a, b = unit(first), unit(second)
    return float(np.degrees(np.arctan2(np.linalg.norm(np.cross(a, b)), np.dot(a, b))))


def slerp_axis(first: Any, second: Any, ratio: float) -> NDArray[np.float64]:
    """单位刀轴的最短大圆插值，保持每段内的角速度恒定。"""

    a, b = unit(first), unit(second)
    t = float(np.clip(ratio, 0.0, 1.0))
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    dot = float(np.clip(np.dot(a, b), -1.0, 1.0))
    tangent = b - dot * a
    length = float(np.linalg.norm(tangent))
    if length < 1e-10:
        if dot > 0.0:
            return unit((1.0 - t) * a + t * b)
        # 反向刀轴的大圆不唯一，确定性地选择垂直方向，避免中点归零。
        basis = np.eye(3)[int(np.argmin(np.abs(a)))]
        tangent = unit(basis - np.dot(basis, a) * a)
    else:
        tangent /= length
    angle = float(np.arccos(dot))
    return unit(np.cos(angle * t) * a + np.sin(angle * t) * tangent)
