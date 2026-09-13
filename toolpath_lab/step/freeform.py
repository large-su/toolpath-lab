"""B 样条 / NURBS 求值（纯 numpy 实现）。

STEP 里的 ``B_SPLINE_CURVE_WITH_KNOTS`` 与 ``B_SPLINE_SURFACE_WITH_KNOTS`` 把节点写成
"重复度 + 节点值"两张表，而求值需要完整的节点向量，所以第一步是 :func:`expand_knots`。

求值用最经典的 de Boor 递推（写成"非零基函数"的迭代形式，便于向量化）：

- 曲线：``C(t) = Σ N_i,p(t) P_i w_i / Σ N_i,p(t) w_i``
- 曲面：两条方向各算一组基函数，再用 ``einsum`` 做张量积

一次调用可以处理一整批参数点，因此离散一张 B 样条曲面只需要两次矩阵运算。
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
from numpy.typing import NDArray

#: 认为基函数为零的下限，避免除零。
_EPS = 1e-12


def expand_knots(multiplicities: Iterable[float], values: Iterable[float],
                 control_count: int, degree: int) -> NDArray[np.float64]:
    """把 (重复度, 节点值) 展开成完整节点向量。

    重复度与节点值一一对应，展开后数量必须正好是 ``control_count + degree + 1``；
    多退少补（补在两端，保持夹紧），这样即使文件里的节点表不规范，求值也不会崩。
    """

    multiplicities = [int(round(float(item))) for item in multiplicities]
    values = [float(item) for item in values]
    expected = control_count + degree + 1
    knots: list[float] = []
    if values and len(multiplicities) == len(values):
        for multiplicity, value in zip(multiplicities, values):
            knots.extend([value] * max(1, multiplicity))
    elif values:
        knots.extend(values)
    else:
        return uniform_knots(control_count, degree)

    if len(knots) < expected:
        low, high = knots[0], knots[-1]
        while len(knots) < expected:
            knots.insert(0, low)
            if len(knots) < expected:
                knots.append(high)

    # 夹紧（clamped）节点向量：两端各 degree+1 重，否则曲线/曲面不经过端点。
    # 节点表不规范时（重复度总和与"控制点数+阶数+1"不符），在这里补齐或收敛。
    if len(knots) >= expected:
        knots = knots[:expected]
        for offset in range(degree + 1):
            knots[offset] = knots[0]
            knots[expected - 1 - offset] = knots[-1]
    return np.asarray(knots, dtype=np.float64)


def uniform_knots(control_count: int, degree: int) -> NDArray[np.float64]:
    """夹紧的均匀节点向量。"""

    interior = control_count - degree - 1
    parts = [[0.0] * (degree + 1)]
    if interior > 0:
        parts.append(list(np.linspace(0.0, 1.0, interior + 2)[1:-1]))
    parts.append([1.0] * (degree + 1))
    return np.asarray([value for chunk in parts for value in chunk], dtype=np.float64)


def knot_spans(parameters: NDArray[np.float64], knots: NDArray[np.float64],
               degree: int) -> NDArray[np.int64]:
    """每个参数落在哪个节点区间（de Boor 的区间下标）。"""

    interior = knots[degree:-degree]
    if interior.size <= 1:
        return np.zeros(parameters.shape[0], dtype=np.int64)
    spans = np.searchsorted(interior, parameters, side="right") - 1
    return np.clip(spans, 0, interior.size - 2).astype(np.int64)


def basis_functions(parameters: NDArray[np.float64], knots: NDArray[np.float64],
                    degree: int) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """返回 (非零基函数值, 区间下标)，形状 (len(parameters), degree+1) 与 (len(parameters),)。

    直接按 Cox-de Boor 公式**逐点**求值：``i`` 的范围取"含该参数的最左区间"
    （``span = searchsorted(interior, t, 'left')``），因此 t 落在节点上时仍然正确——
    这一点很关键：端点（t = 起点 / 终点）正是最容易算错的地方，早期的实现就在
    端点处把基函数整体错位，曲面的第一个控制点会被"吃掉"。
    """

    parameters = np.asarray(parameters, dtype=np.float64).reshape(-1)
    count = parameters.shape[0]
    interior = knots[degree:-degree]
    if interior.size <= 1:
        first = np.zeros(count, dtype=np.int64)
    else:
        first = np.clip(np.searchsorted(interior, parameters, side="left") - 1,
                        0, interior.size - 2).astype(np.int64)
    basis = np.zeros((count, degree + 1), dtype=np.float64)
    for row in range(count):
        for column in range(degree + 1):
            index = int(first[row]) + column
            if index < 0 or index >= knots.shape[0] - degree - 1:
                basis[row, column] = 0.0
                continue
            basis[row, column] = _basis_one(parameters[row], index, degree, knots)
    return basis, first


def _basis_one(parameter: float, index: int, degree: int,
               knots: NDArray[np.float64]) -> float:
    """单个基函数 N_{index, degree}(parameter) 的 Cox-de Boor 递归值。

    约定分母为 0 时该项为 0（重复节点处的标准处理），否则会出现 0/0。
    """

    if degree == 0:
        if knots[index] <= parameter < knots[index + 1]:
            return 1.0
        # 最后一个节点处闭区间收尾（夹紧节点向量的右端点）
        if parameter == knots[-1] and knots[index] < parameter <= knots[index + 1]:
            return 1.0
        return 0.0
    left = 0.0
    denominator = knots[index + degree] - knots[index]
    if denominator > 0.0:
        left = (parameter - knots[index]) / denominator * _basis_one(
            parameter, index, degree - 1, knots
        )
    right = 0.0
    denominator = knots[index + degree + 1] - knots[index + 1]
    if denominator > 0.0:
        right = (knots[index + degree + 1] - parameter) / denominator * _basis_one(
            parameter, index + 1, degree - 1, knots
        )
    return left + right


def curve_point_parameters(parameters: NDArray[np.float64],
                           control_points: NDArray[np.float64],
                           weights: NDArray[np.float64],
                           knots: NDArray[np.float64],
                           degree: int) -> NDArray[np.float64]:
    """一条 NURBS 曲线在一批参数处的点。"""

    parameters = np.asarray(parameters, dtype=np.float64).reshape(-1)
    basis, spans = basis_functions(parameters, knots, degree)
    indices = spans[:, None] + np.arange(degree + 1)[None, :]
    indices = np.clip(indices, 0, control_points.shape[0] - 1)
    points = control_points[indices]  # (m, p+1, 3)
    local_weights = weights[indices]  # (m, p+1)
    weighted = points * (basis * local_weights)[:, :, None]
    numerator = weighted.sum(axis=1)
    denominator = (basis * local_weights).sum(axis=1)
    return numerator / np.where(np.abs(denominator) > _EPS, denominator, 1.0)[:, None]


def curve_point_derivatives(parameter: float,
                            control_points: NDArray[np.float64],
                            weights: NDArray[np.float64],
                            knots: NDArray[np.float64],
                            degree: int) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """单点求值与一阶导（用邻近差分，够精确也够简单）。"""

    step = max(1e-5, float(knots[-1] - knots[0]) * 1e-4)
    values = curve_point_parameters(
        np.array([parameter - step, parameter, parameter + step], dtype=np.float64),
        control_points, weights, knots, degree,
    )
    derivative = (values[2] - values[0]) / (2.0 * step)
    return values[1], derivative


def surface_grid(control_points: NDArray[np.float64],
                 weights: NDArray[np.float64],
                 u_knots: NDArray[np.float64],
                 v_knots: NDArray[np.float64],
                 u_degree: int,
                 v_degree: int,
                 u_values: NDArray[np.float64],
                 v_values: NDArray[np.float64]) -> NDArray[np.float64]:
    """一张 NURBS 曲面在参数网格上的点，形状 (len(u), len(v), 3)。"""

    u_values = np.asarray(u_values, dtype=np.float64).reshape(-1)
    v_values = np.asarray(v_values, dtype=np.float64).reshape(-1)
    u_basis, u_spans = basis_functions(u_values, u_knots, u_degree)
    v_basis, v_spans = basis_functions(v_values, v_knots, v_degree)
    u_indices = np.clip(u_spans[:, None] + np.arange(u_degree + 1)[None, :],
                        0, control_points.shape[0] - 1)
    v_indices = np.clip(v_spans[:, None] + np.arange(v_degree + 1)[None, :],
                        0, control_points.shape[1] - 1)

    # 先按 v 方向收缩：把加权控制点沿 v 的局部支撑 (pv+1) 求和
    control_sub = control_points[:, v_indices]              # (nu_ctrl, nv, pv+1, 3)
    weight_sub = weights[:, v_indices]                      # (nu_ctrl, nv, pv+1)
    v_weight = v_basis[None, :, :] * weight_sub             # (nu_ctrl, nv, pv+1)
    numerator_v = np.einsum("cvk,cvkd->cvd", v_weight, control_sub)
    denominator_v = v_weight.sum(axis=2)                    # (nu_ctrl, nv)
    with np.errstate(divide="ignore", invalid="ignore"):
        reduced = numerator_v / np.where(np.abs(denominator_v) > _EPS,
                                         denominator_v, 1.0)[:, :, None]

    # 再按 u 方向收缩：基函数 (nu, pu+1) 与已收缩的分母 (nu_ctrl, nv)
    reduced_sub = reduced[u_indices]                        # (nu, pu+1, nv, 3)
    denom_sub = denominator_v[u_indices]                    # (nu, pu+1, nv)
    u_weight = u_basis[:, :, None] * denom_sub              # (nu, pu+1, nv)
    numerator = np.einsum("ukn,uknd->und", u_weight, reduced_sub)
    denominator = u_weight.sum(axis=1)                      # (nu, nv)
    with np.errstate(divide="ignore", invalid="ignore"):
        grid = numerator / np.where(np.abs(denominator) > _EPS, denominator, 1.0)[:, :, None]
    return np.nan_to_num(grid, nan=0.0)


def surface_point_derivatives(u: float, v: float,
                              control_points: NDArray[np.float64],
                              weights: NDArray[np.float64],
                              u_knots: NDArray[np.float64],
                              v_knots: NDArray[np.float64],
                              u_degree: int,
                              v_degree: int) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """单点求值与一阶偏导（du, dv 拼成一个 (2, 3) 数组）。"""

    u_step = max(1e-5, float(u_knots[-1] - u_knots[0]) * 1e-4)
    v_step = max(1e-5, float(v_knots[-1] - v_knots[0]) * 1e-4)
    u_samples = np.array([u - u_step, u, u + u_step], dtype=np.float64)
    v_samples = np.array([v - v_step, v, v + v_step], dtype=np.float64)
    grid = surface_grid(control_points, weights, u_knots, v_knots, u_degree, v_degree,
                        u_samples, v_samples)
    value = grid[1, 1]
    derivative_u = (grid[2, 1] - grid[0, 1]) / (2.0 * u_step)
    derivative_v = (grid[1, 2] - grid[1, 0]) / (2.0 * v_step)
    return value, np.vstack([derivative_u, derivative_v])


__all__ = [
    "basis_functions",
    "curve_point_derivatives",
    "curve_point_parameters",
    "expand_knots",
    "knot_spans",
    "surface_grid",
    "surface_point_derivatives",
    "uniform_knots",
]
