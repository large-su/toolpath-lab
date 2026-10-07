"""Small helpers shared by the geometry layers.

Conventions used across the project:

- units are millimetres and seconds; angles are degrees at the API boundary and radians inside;
- right-handed system, Z axis up, the machining plane is XY;
- arrays are float64: a vector is (3,), a polyline (N, 3), a planar polygon (N, 2).
"""

from __future__ import annotations

from math import cos, radians, sin
from typing import Any

import numpy as np
from numpy.typing import NDArray

EPS = 1e-9


def unit(vector: Any) -> NDArray[np.float64]:
    """Unit vector pointing along `vector`."""

    array = np.asarray(vector, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm <= EPS:
        raise ValueError("向量退化（长度为零）")
    return array / norm


def rotation_2d(angle_deg: float) -> NDArray[np.float64]:
    """Counter-clockwise rotation matrix in the XY plane by `angle_deg` degrees."""

    angle = radians(angle_deg)
    return np.array(
        [[cos(angle), -sin(angle)], [sin(angle), cos(angle)]],
        dtype=np.float64,
    )


def direction_2d(angle_deg: float) -> NDArray[np.float64]:
    """Unit direction vector at `angle_deg` measured from the +X axis."""

    angle = radians(angle_deg)
    return np.array([cos(angle), sin(angle)], dtype=np.float64)


def cumulative_lengths(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Cumulative arc length along a polyline, starting at 0."""

    array = np.asarray(points, dtype=np.float64)
    if array.shape[0] < 2:
        return np.zeros(array.shape[0], dtype=np.float64)
    steps = np.linalg.norm(np.diff(array, axis=0), axis=1)
    return np.concatenate(([0.0], np.cumsum(steps)))
