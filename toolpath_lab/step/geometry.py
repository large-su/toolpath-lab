"""STEP 几何层：把实体图翻译成"可求值的"点、曲线与曲面。

STEP 的 B-rep 用两层描述一张面：``ADVANCED_FACE`` 给出**曲面**（PLANE、CYLINDRICAL_SURFACE、
B_SPLINE_SURFACE_WITH_KNOTS……）和若干**边界环**（由 EDGE_CURVE 组成的 EDGE_LOOP）。
本模块负责把这两层都变成能在任意参数处求值的 Python 对象：

- :class:`Surface` / :class:`Curve` 基类：``point(u, v)``、``normal(u, v)``、``uv_of(point)``；
- :class:`Resolver`：带缓存的实体解释器，也是"这个实体是什么意思"的唯一入口。

覆盖范围（按出现频率排序）：
平面、圆柱面、圆锥面、球面、环面、B 样条曲面；直线、圆、椭圆、B 样条曲线。
其余实体（例如 OFFSET_SURFACE、SWEPT_SURFACE）不会让解析失败——调用方会拿到 ``None``，
并在结果里记一条警告，界面上会提示"该面未离散"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import acos, atan2, ceil, cos, degrees, hypot, pi, sin, sqrt
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.step import freeform
from toolpath_lab.step.errors import StepFormatError
from toolpath_lab.step.parser import (
    Ref,
    StepEntity,
    StepFile,
    as_bool,
    as_float,
    as_int,
    as_numbers,
    as_ref,
    as_sequence,
)

#: ``closest_uv`` 迭代次数与步数：解析曲面上用网格 + 二分，够用且不慢。
_UV_COARSE_STEPS = 24
_UV_REFINE_STEPS = 6

Vec3 = NDArray[np.float64]
Vec2 = NDArray[np.float64]


# ---------------------------------------------------------------- 基础变换
def placement_matrix(origin: Sequence[float], axis: Sequence[float],
                     ref_direction: Sequence[float]) -> NDArray[np.float64]:
    """由 STEP 的 ``AXIS2_PLACEMENT_3D`` 三个分量构造 4x4 齐次矩阵。"""

    point = np.asarray(origin, dtype=np.float64).reshape(3)
    z_axis = np.asarray(axis, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(z_axis))
    if norm <= 1e-12:
        z_axis = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    else:
        z_axis = z_axis / norm
    x_axis = np.asarray(ref_direction, dtype=np.float64).reshape(3)
    # 参考方向去掉沿 Z 的分量再归一化；退化时随便取一个与 Z 正交的方向。
    x_axis = x_axis - float(np.dot(x_axis, z_axis)) * z_axis
    norm = float(np.linalg.norm(x_axis))
    if norm <= 1e-12:
        helper = np.array([1.0, 0.0, 0.0]) if abs(z_axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        x_axis = helper - float(np.dot(helper, z_axis)) * z_axis
        norm = float(np.linalg.norm(x_axis))
        if norm <= 1e-12:  # pragma: no cover - 只有零向量轴才会走到
            x_axis = np.array([1.0, 0.0, 0.0])
            norm = 1.0
    x_axis = x_axis / norm
    y_axis = np.cross(z_axis, x_axis)
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, 0] = x_axis
    matrix[:3, 1] = y_axis
    matrix[:3, 2] = z_axis
    matrix[:3, 3] = point
    return matrix


def transform_points(matrix: NDArray[np.float64], points: NDArray[np.float64]) -> NDArray[np.float64]:
    """用 4x4 矩阵变换 (N, 3) 点集。"""

    array = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    return array @ matrix[:3, :3].T + matrix[:3, 3]


# ------------------------------------------------------------------- 曲面
class Surface:
    """曲面的共同接口。参数域与 STEP 约定一致（弧度，单位 mm）。"""

    kind: str = "unknown"
    #: 参数域；``None`` 表示该方向无界。
    u_range: tuple[float, float] | None = None
    v_range: tuple[float, float] | None = None

    def point(self, u: float, v: float) -> Vec3:  # pragma: no cover - 抽象
        raise NotImplementedError

    def points_grid(self, u_values: NDArray[np.float64], v_values: NDArray[np.float64]) -> NDArray[np.float64]:
        """在参数网格上批量求值，返回 (len(u), len(v), 3)。"""

        grid = np.empty((u_values.size, v_values.size, 3), dtype=np.float64)
        for i, u in enumerate(u_values):
            for j, v in enumerate(v_values):
                grid[i, j] = self.point(float(u), float(v))
        return grid

    def normal(self, u: float, v: float) -> Vec3:
        """数值法向（单位向量）。"""

        du = max(1e-5, self._parameter_scale(u, v) * 1e-4)
        p = self.point(u, v)
        tu = self.point(u + du, v) - self.point(u - du, v)
        tv = self.point(u, v + du) - self.point(u, v - du)
        cross = np.cross(tu, tv)
        norm = float(np.linalg.norm(cross))
        if norm <= 1e-12:
            return np.array([0.0, 0.0, 1.0], dtype=np.float64)
        return cross / norm

    def _parameter_scale(self, u: float, v: float) -> float:
        return 1.0

    def uv_of(self, point: Sequence[float]) -> Vec2:
        """把空间点反算成参数坐标（用于把边界曲线投到参数域）。

        粗搜 + 逐步收缩的局部细化。粗搜必须在**整个参数域**上稠密取样：
        距离函数在周期方向上不是单峰的（例如圆柱面上正对某点的角度有两个候选），
        稀疏取样会落到错误的局部极小值上。
        """

        target = np.asarray(point, dtype=np.float64).reshape(3)
        u_lo, u_hi = self._search_range(self.u_range, 0.0, 2.0 * pi)
        v_lo, v_hi = self._search_range(self.v_range, -1.0, 1.0)

        best_uv: Vec2 | None = None
        best_distance = float("inf")
        for u in np.linspace(u_lo, u_hi, _UV_COARSE_STEPS):
            for v in np.linspace(v_lo, v_hi, _UV_COARSE_STEPS):
                distance = float(np.linalg.norm(self.point(float(u), float(v)) - target))
                if distance < best_distance:
                    best_distance = distance
                    best_uv = np.array([u, v], dtype=np.float64)
        if best_uv is None:  # pragma: no cover - 搜索范围永远非空
            return np.zeros(2, dtype=np.float64)

        step_u = (u_hi - u_lo) / _UV_COARSE_STEPS
        step_v = (v_hi - v_lo) / _UV_COARSE_STEPS
        for _ in range(_UV_REFINE_STEPS * 2):
            improved = False
            for delta_u in (-step_u, 0.0, step_u):
                for delta_v in (-step_v, 0.0, step_v):
                    if delta_u == 0.0 and delta_v == 0.0:
                        continue
                    candidate_u, candidate_v = best_uv[0] + delta_u, best_uv[1] + delta_v
                    if self.u_range is not None and not (self.u_range[0] - 1e-9 <= candidate_u <= self.u_range[1] + 1e-9):
                        continue
                    if self.v_range is not None and not (self.v_range[0] - 1e-9 <= candidate_v <= self.v_range[1] + 1e-9):
                        continue
                    distance = float(np.linalg.norm(self.point(float(candidate_u), float(candidate_v)) - target))
                    if distance < best_distance:
                        best_distance = distance
                        best_uv = np.array([candidate_u, candidate_v], dtype=np.float64)
                        improved = True
            if not improved:
                step_u *= 0.5
                step_v *= 0.5
        return best_uv

    @property
    def periodic_u(self) -> bool:
        """参数域在 u 方向是否首尾相接（圆柱、圆锥、球、环面都是）。"""

        if self.u_range is None:
            return False
        return abs((self.u_range[1] - self.u_range[0]) - 2.0 * pi) < 1e-6

    @staticmethod
    def _search_range(bounds: tuple[float, float] | None, low: float, high: float) -> tuple[float, float]:
        if bounds is None:
            return low, high
        return float(bounds[0]), float(bounds[1])


@dataclass(slots=True)
class Plane(Surface):
    """平面：``AXIS2_PLACEMENT_3D`` 给出的局部坐标系，参数就是局部 x/y。"""

    matrix: NDArray[np.float64]
    kind: str = "plane"

    def point(self, u: float, v: float) -> Vec3:
        return self.matrix[:3, 3] + u * self.matrix[:3, 0] + v * self.matrix[:3, 1]

    def normal(self, u: float = 0.0, v: float = 0.0) -> Vec3:
        return self.matrix[:3, 2].copy()

    def uv_of(self, point: Sequence[float]) -> Vec2:
        delta = np.asarray(point, dtype=np.float64).reshape(3) - self.matrix[:3, 3]
        return np.array([float(np.dot(delta, self.matrix[:3, 0])),
                         float(np.dot(delta, self.matrix[:3, 1]))], dtype=np.float64)

    def _parameter_scale(self, u: float, v: float) -> float:
        return 1.0


@dataclass(slots=True)
class Cylinder(Surface):
    """圆柱面：u 是绕轴角度（弧度），v 是沿轴高度。"""

    matrix: NDArray[np.float64]
    radius: float
    kind: str = "cylinder"
    u_range: tuple[float, float] | None = (0.0, 2.0 * pi)

    def point(self, u: float, v: float) -> Vec3:
        return (self.matrix[:3, 3] + self.radius * (cos(u) * self.matrix[:3, 0] + sin(u) * self.matrix[:3, 1])
                + v * self.matrix[:3, 2])

    def normal(self, u: float, v: float = 0.0) -> Vec3:
        return cos(u) * self.matrix[:3, 0] + sin(u) * self.matrix[:3, 1]

    def uv_of(self, point: Sequence[float]) -> Vec2:
        delta = np.asarray(point, dtype=np.float64).reshape(3) - self.matrix[:3, 3]
        x = float(np.dot(delta, self.matrix[:3, 0]))
        y = float(np.dot(delta, self.matrix[:3, 1]))
        return np.array([atan2(y, x), float(np.dot(delta, self.matrix[:3, 2]))], dtype=np.float64)

    def _parameter_scale(self, u: float, v: float) -> float:
        return max(self.radius, 1.0)


@dataclass(slots=True)
class Cone(Surface):
    """圆锥面：u 是绕轴角度，v 是沿素线到顶点的距离。"""

    matrix: NDArray[np.float64]
    radius: float
    semi_angle: float
    kind: str = "cone"
    u_range: tuple[float, float] | None = (0.0, 2.0 * pi)

    def point(self, u: float, v: float) -> Vec3:
        radial = self.radius + v * sin(self.semi_angle)
        height = -v * cos(self.semi_angle)
        return (self.matrix[:3, 3] + radial * (cos(u) * self.matrix[:3, 0] + sin(u) * self.matrix[:3, 1])
                + height * self.matrix[:3, 2])

    def uv_of(self, point: Sequence[float]) -> Vec2:
        delta = np.asarray(point, dtype=np.float64).reshape(3) - self.matrix[:3, 3]
        x = float(np.dot(delta, self.matrix[:3, 0]))
        y = float(np.dot(delta, self.matrix[:3, 1]))
        height = float(np.dot(delta, self.matrix[:3, 2]))
        radial = hypot(x, y)
        tangent = sin(self.semi_angle)
        along = radial - self.radius if abs(tangent) <= 1e-9 else (radial - self.radius) / tangent
        if abs(cos(self.semi_angle)) > 1e-9:
            along = -height / cos(self.semi_angle)
        return np.array([atan2(y, x), float(along)], dtype=np.float64)

    def _parameter_scale(self, u: float, v: float) -> float:
        return max(abs(self.radius), 1.0)


@dataclass(slots=True)
class Sphere(Surface):
    """球面：u 是绕轴角度，v 是与 +Z 的夹角。"""

    matrix: NDArray[np.float64]
    radius: float
    kind: str = "sphere"
    u_range: tuple[float, float] | None = (0.0, 2.0 * pi)
    v_range: tuple[float, float] | None = (-pi / 2.0, pi / 2.0)

    def point(self, u: float, v: float) -> Vec3:
        local = np.array([cos(v) * cos(u), cos(v) * sin(u), sin(v)], dtype=np.float64)
        return self.matrix[:3, 3] + self.radius * (self.matrix[:3, :3] @ local)

    def normal(self, u: float, v: float) -> Vec3:
        local = np.array([cos(v) * cos(u), cos(v) * sin(u), sin(v)], dtype=np.float64)
        return self.matrix[:3, :3] @ local

    def uv_of(self, point: Sequence[float]) -> Vec2:
        delta = np.asarray(point, dtype=np.float64).reshape(3) - self.matrix[:3, 3]
        local = self.matrix[:3, :3].T @ delta
        norm = float(np.linalg.norm(local))
        if norm <= 1e-12:
            return np.zeros(2, dtype=np.float64)
        local = local / norm
        return np.array([atan2(float(local[1]), float(local[0])), float(np.arcsin(np.clip(local[2], -1.0, 1.0)))],
                        dtype=np.float64)

    def _parameter_scale(self, u: float, v: float) -> float:
        return max(self.radius, 1.0)


@dataclass(slots=True)
class Torus(Surface):
    """环面：u 绕主轴，v 绕管截面。"""

    matrix: NDArray[np.float64]
    major_radius: float
    minor_radius: float
    kind: str = "torus"
    u_range: tuple[float, float] | None = (0.0, 2.0 * pi)
    v_range: tuple[float, float] | None = (0.0, 2.0 * pi)

    def point(self, u: float, v: float) -> Vec3:
        radial = self.major_radius + self.minor_radius * cos(v)
        local = np.array([radial * cos(u), radial * sin(u), self.minor_radius * sin(v)], dtype=np.float64)
        return self.matrix[:3, 3] + (self.matrix[:3, :3] @ local)

    def uv_of(self, point: Sequence[float]) -> Vec2:
        delta = np.asarray(point, dtype=np.float64).reshape(3) - self.matrix[:3, 3]
        local = self.matrix[:3, :3].T @ delta
        u = atan2(float(local[1]), float(local[0]))
        radial = hypot(float(local[0]), float(local[1])) - self.major_radius
        if abs(self.minor_radius) <= 1e-12:
            return np.array([u, 0.0], dtype=np.float64)
        v = atan2(float(local[2]), radial)
        return np.array([u, v], dtype=np.float64)

    def _parameter_scale(self, u: float, v: float) -> float:
        return max(self.major_radius, self.minor_radius, 1.0)


@dataclass(slots=True)
class BSplineSurface(Surface):
    """B 样条 / NURBS 曲面。"""

    control_points: NDArray[np.float64]  # (n_u, n_v, 3)
    weights: NDArray[np.float64]  # (n_u, n_v)
    u_knots: NDArray[np.float64]
    v_knots: NDArray[np.float64]
    u_degree: int
    v_degree: int
    kind: str = "bspline_surface"
    u_range: tuple[float, float] | None = None
    v_range: tuple[float, float] | None = None

    def point(self, u: float, v: float) -> Vec3:
        value, _ = freeform.surface_point_derivatives(
            u, v, self.control_points, self.weights, self.u_knots, self.v_knots,
            self.u_degree, self.v_degree,
        )
        return value

    def points_grid(self, u_values: NDArray[np.float64], v_values: NDArray[np.float64]) -> NDArray[np.float64]:
        return freeform.surface_grid(
            self.control_points, self.weights, self.u_knots, self.v_knots,
            self.u_degree, self.v_degree, u_values, v_values,
        )


# ------------------------------------------------------------------- 曲线
class Curve:
    """3D 曲线的共同接口。"""

    kind: str = "unknown"

    def point(self, t: float) -> Vec3:  # pragma: no cover - 抽象
        raise NotImplementedError

    def sample(self, count: int) -> NDArray[np.float64]:
        """按参数等分采样 count 个点，返回 (count, 3)。"""

        parameters = self.parameters(count)
        return np.array([self.point(float(t)) for t in parameters], dtype=np.float64)

    def parameters(self, count: int) -> NDArray[np.float64]:
        low, high = self.parameter_range()
        return np.linspace(low, high, max(2, count))

    def parameter_range(self) -> tuple[float, float]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def projected_uv(self, surface: Surface, count: int) -> NDArray[np.float64]:
        points = self.sample(count)
        return np.array([surface.uv_of(point) for point in points], dtype=np.float64)


@dataclass(slots=True)
class Line(Curve):
    """直线段（用参数区间截断）。"""

    origin: Vec3
    direction: Vec3
    low: float = 0.0
    high: float = 1.0
    kind: str = "line"

    def point(self, t: float) -> Vec3:
        return self.origin + t * self.direction

    def parameter_range(self) -> tuple[float, float]:
        return self.low, self.high


@dataclass(slots=True)
class Circle(Curve):
    """圆；``trim_*`` 给出参数区间。"""

    center: Vec3
    axis_x: Vec3
    axis_y: Vec3
    radius: float
    low: float = 0.0
    high: float = 2.0 * pi
    kind: str = "circle"

    def point(self, t: float) -> Vec3:
        return self.center + self.radius * (cos(t) * self.axis_x + sin(t) * self.axis_y)

    def parameter_range(self) -> tuple[float, float]:
        return self.low, self.high

    def angle_of(self, point: Sequence[float]) -> float:
        """点相对本圆的角度（用于把圆弧的两个端点变成参数区间）。"""

        delta = np.asarray(point, dtype=np.float64).reshape(3) - self.center
        x = float(np.dot(delta, self.axis_x))
        y = float(np.dot(delta, self.axis_y))
        return atan2(y, x)

    def arc_candidates(self, start: Sequence[float], end: Sequence[float]
                       ) -> tuple[tuple[float, float], tuple[float, float]]:
        """两个候选参数区间：正扫（逆参数方向）与反扫（顺参数方向）。

        仅凭两个端点的角度无法判断一条边沿哪个方向走——正扫与反扫的端点角度完全相同
        （例如另一侧环上的圆弧从 11.25° 反向扫到 0°，与正扫 0°→11.25° 端点一样）。
        调用方需要用几何采样（与边界折线的距离）来决出胜者，见
        :func:`~toolpath_lab.step.tessellate.edge_uv_loops`。
        """

        start_angle = self.angle_of(start)
        end_angle = self.angle_of(end)
        forward_sweep = (end_angle - start_angle) % (2.0 * pi)
        backward_sweep = forward_sweep - 2.0 * pi
        return ((start_angle, start_angle + forward_sweep),
                (start_angle, start_angle + backward_sweep))

    def arc_parameters(self, start: Sequence[float], end: Sequence[float],
                       same_sense: bool) -> tuple[float, float]:
        """按 ``same_sense`` 给出参数区间（不做"劣弧"压缩）。

        跨度取该方向上的第一段（0 到 2π 之间）。把它当成劣弧会让参数域里的边界环
        方向反转，裁剪多边形随之失效。
        """

        start_angle = self.angle_of(start)
        end_angle = self.angle_of(end)
        # 起止点**重合**说明这是一条闭合边（圆柱面 / 圆孔的接缝边），它绕了整整一圈。
        # 此时两个角度相同，按下面的公式算出来的跨度是 0，采样会退化成一堆重合点：
        # 整张圆面（例如直径 25 的孔底）于是变成零面积并被丢弃，模型上凭空多一个破洞。
        if float(np.linalg.norm(np.asarray(end, dtype=np.float64).reshape(3)
                                - np.asarray(start, dtype=np.float64).reshape(3))) <= 1e-9:
            sweep = 2.0 * pi if same_sense else -2.0 * pi
            return start_angle, start_angle + sweep
        if same_sense:
            sweep = (end_angle - start_angle) % (2.0 * pi)
        else:
            sweep = -((start_angle - end_angle) % (2.0 * pi))
        return start_angle, start_angle + sweep

    def arc_parameters_from(self, candidates: tuple[tuple[float, float], tuple[float, float]],
                            same_sense: bool) -> tuple[float, float]:
        """没有几何参考可依据时，按 ``same_sense`` 在候选里选一个。"""

        first, second = candidates
        forward_span = first[1] - first[0]
        backward_span = second[1] - second[0]
        if same_sense:
            return first if abs(forward_span) <= abs(backward_span) else second
        return second if abs(backward_span) <= abs(forward_span) else first

    def sample_arc(self, start: Sequence[float], end: Sequence[float], same_sense: bool,
                   count: int = 8) -> NDArray[np.float64]:
        low, high = self.arc_parameters(start, end, same_sense)
        return np.array([self.point(float(t)) for t in np.linspace(low, high, max(2, count))],
                        dtype=np.float64)


@dataclass(slots=True)
class Ellipse(Curve):
    """椭圆。"""

    center: Vec3
    axis_x: Vec3
    axis_y: Vec3
    semi_x: float
    semi_y: float
    low: float = 0.0
    high: float = 2.0 * pi
    kind: str = "ellipse"

    def point(self, t: float) -> Vec3:
        return self.center + self.semi_x * cos(t) * self.axis_x + self.semi_y * sin(t) * self.axis_y

    def parameter_range(self) -> tuple[float, float]:
        return self.low, self.high


@dataclass(slots=True)
class BSplineCurve(Curve):
    """B 样条 / NURBS 曲线。"""

    control_points: NDArray[np.float64]  # (n, 3)
    weights: NDArray[np.float64]  # (n,)
    knots: NDArray[np.float64]
    degree: int
    kind: str = "bspline_curve"

    def point(self, t: float) -> Vec3:
        value, _ = freeform.curve_point_derivatives(
            t, self.control_points, self.weights, self.knots, self.degree
        )
        return value

    def parameter_range(self) -> tuple[float, float]:
        return float(self.knots[self.degree]), float(self.knots[-1 - self.degree])


@dataclass(slots=True)
class CompositeCurve(Curve):
    """复合曲线：把若干段首尾相接的曲线当成一条。"""

    segments: tuple[Curve, ...]
    reversed_flags: tuple[bool, ...] = ()
    kind: str = "composite_curve"

    def point(self, t: float) -> Vec3:
        low, high = self.parameter_range()
        if high <= low:
            return self.segments[0].point(low) if self.segments else np.zeros(3)
        position = (t - low) / (high - low) * len(self.segments)
        index = int(min(max(position, 0.0), len(self.segments) - 1e-9))
        local = min(max(position - index, 0.0), 1.0)
        segment = self.segments[index]
        if self.reversed_flags and self.reversed_flags[index]:
            local = 1.0 - local
        seg_low, seg_high = segment.parameter_range()
        return segment.point(seg_low + local * (seg_high - seg_low))

    def parameter_range(self) -> tuple[float, float]:
        return 0.0, float(max(1, len(self.segments)))


# ---------------------------------------------------------------- 解释器
class Resolver:
    """实体解释器：把 ``#n`` 变成对象，并缓存结果。"""

    def __init__(self, step: StepFile) -> None:
        self.step = step
        self._cache: dict[tuple[str, int], Any] = {}
        self._unsupported_surfaces: dict[str, int] = {}
        self._unsupported_curves: dict[str, int] = {}

    # -- 基础实体 ----------------------------------------------------------
    def entity(self, value: Any) -> StepEntity | None:
        entity_id = as_ref(value)
        if entity_id is None:
            return None
        return self.step.get(entity_id)

    def point3(self, value: Any) -> Vec3 | None:
        """``CARTESIAN_POINT`` -> (3,) 坐标。"""

        entity = self.entity(value)
        if entity is None:
            return None
        coordinates = as_numbers(entity.arg(1))
        if len(coordinates) < 3:
            coordinates = coordinates + [0.0] * (3 - len(coordinates))
        return np.array(coordinates[:3], dtype=np.float64)

    def direction3(self, value: Any) -> Vec3 | None:
        """``DIRECTION`` -> 单位向量。"""

        entity = self.entity(value)
        if entity is None:
            return None
        coordinates = as_numbers(entity.arg(1))
        vector = np.array((coordinates + [0.0, 0.0, 1.0])[:3], dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            return np.array([0.0, 0.0, 1.0], dtype=np.float64)
        return vector / norm

    def placement(self, value: Any) -> NDArray[np.float64] | None:
        """``AXIS2_PLACEMENT_3D``（及其子类）-> 4x4 矩阵。"""

        entity = self.entity(value)
        if entity is None:
            return None
        cache_key = ("placement", entity.id)
        if cache_key in self._cache:
            return self._cache[cache_key]
        origin = self.point3(entity.arg(1))
        axis = self.direction3(entity.arg(2))
        reference = self.direction3(entity.arg(3))
        if origin is None:
            origin = np.zeros(3, dtype=np.float64)
        if axis is None:
            axis = np.array([0.0, 0.0, 1.0], dtype=np.float64)
        if reference is None:
            reference = np.array([1.0, 0.0, 0.0], dtype=np.float64)
        matrix = placement_matrix(origin, axis, reference)
        self._cache[cache_key] = matrix
        return matrix

    # -- 曲线 --------------------------------------------------------------
    def curve(self, value: Any) -> Curve | None:
        entity = self.entity(value)
        if entity is None:
            return None
        cache_key = ("curve", entity.id)
        if cache_key in self._cache:
            return self._cache[cache_key]
        curve = self._build_curve(entity)
        self._cache[cache_key] = curve
        return curve

    def _build_curve(self, entity: StepEntity) -> Curve | None:
        keyword = entity.keyword
        if keyword == "LINE":
            origin = self.point3(entity.arg(1))
            vector_entity = self.entity(entity.arg(2))
            direction = None
            magnitude = 0.0
            if vector_entity is not None:
                # DIRECTION 只有方向；VECTOR 还带长度。
                if vector_entity.keyword == "VECTOR":
                    direction = self.direction3(vector_entity.arg(1))
                    magnitude = as_float(vector_entity.arg(2), 1.0)
                else:
                    direction = self.direction3(entity.arg(2))
                    magnitude = 1.0
            if origin is None or direction is None:
                return None
            # 注意：**不能**把单位方向乘上 VECTOR 的 magnitude。
            # 直线在 STEP 里的参数化是 P(t) = pnt + t·dir，dir 是**单位**方向，t 就是距离。
            # 真实导出器（SolidWorks / SwSTEP）常用 VECTOR 代替 direction，而它的 magnitude
            # 常写成 1000；早先按 `direction * magnitude` 构造、参数域又取 [0, magnitude]，
            # 于是 point(t) 一路跑到 origin + magnitude²·dir（1e6 量级），
            # 边界折线被拉出 ±975634 mm 的尖刺，面积、体积、渲染全跟着错。
            # 直线段的采样统一由 Edge.sample / _loop_polyline 按两个端点线性插值，
            # 完全不依赖这里的参数域。
            low, high = self._trim_range(entity.arg(3), 0.0, 1.0)
            return Line(origin, direction, low, high)
        if keyword == "CIRCLE":
            matrix = self.placement(entity.arg(1))
            radius = as_float(entity.arg(2))
            if matrix is None or radius <= 0:
                return None
            low, high = self._trim_range(entity.arg(3), 0.0, 2.0 * pi)
            return Circle(matrix[:3, 3], matrix[:3, 0], matrix[:3, 1], radius, low, high)
        if keyword == "ELLIPSE":
            matrix = self.placement(entity.arg(1))
            semi_x = as_float(entity.arg(2))
            semi_y = as_float(entity.arg(3))
            if matrix is None or semi_x <= 0 or semi_y <= 0:
                return None
            low, high = self._trim_range(entity.arg(4), 0.0, 2.0 * pi)
            return Ellipse(matrix[:3, 3], matrix[:3, 0], matrix[:3, 1], semi_x, semi_y, low, high)
        if keyword in {"B_SPLINE_CURVE", "B_SPLINE_CURVE_WITH_KNOTS", "RATIONAL_B_SPLINE_CURVE",
                       "BEZIER_CURVE", "QUASI_UNIFORM_CURVE", "UNIFORM_CURVE"}:
            return self._build_bspline_curve(entity)
        if keyword == "COMPOSITE_CURVE":
            return self._build_composite_curve(entity)
        if keyword in {"TRIMMED_CURVE"}:
            return self.curve(entity.arg(1))
        self._unsupported_curves[keyword] = self._unsupported_curves.get(keyword, 0) + 1
        return None

    def _build_bspline_curve(self, entity: StepEntity, offset: int = 0) -> BSplineCurve | None:
        degree = as_int(entity.arg(offset + 1))
        raw_points = as_sequence(entity.arg(offset + 2))
        control: list[Vec3] = []
        for item in raw_points:
            point = self.point3(item)
            if point is None:
                return None
            control.append(point)
        if len(control) <= degree:
            return None
        multiplicity = as_numbers(entity.arg(offset + 6))
        knot_values = as_numbers(entity.arg(offset + 7))
        points = np.array(control, dtype=np.float64)
        if entity.keyword.startswith("RATIONAL") or "RATIONAL" in entity.components:
            weights = self._weights(entity, offset + 8, len(control))
        else:
            weights = np.ones(len(control), dtype=np.float64)
        if not knot_values:
            knots = freeform.uniform_knots(len(control), degree)
        else:
            knots = freeform.expand_knots(multiplicity, knot_values, len(control), degree)
        return BSplineCurve(points, weights, knots, degree)

    def _weights(self, entity: StepEntity, index: int, count: int) -> NDArray[np.float64]:
        values = as_numbers(entity.arg(index))
        if len(values) == count:
            return np.array(values, dtype=np.float64)
        return np.ones(count, dtype=np.float64)

    def _build_composite_curve(self, entity: StepEntity) -> CompositeCurve | None:
        segments: list[Curve] = []
        reversed_flags: list[bool] = []
        for item in as_sequence(entity.arg(1)):
            segment_entity = self.entity(item)
            if segment_entity is None:
                return None
            if segment_entity.keyword == "COMPOSITE_CURVE_SEGMENT":
                same_sense = as_bool(segment_entity.arg(0), True)
                curve = self.curve(segment_entity.arg(1))
            else:
                same_sense = True
                curve = self.curve(item)
            if curve is None:
                return None
            segments.append(curve)
            reversed_flags.append(not same_sense)
        if not segments:
            return None
        return CompositeCurve(tuple(segments), tuple(reversed_flags))

    def _trim_range(self, value: Any, default_low: float, default_high: float) -> tuple[float, float]:
        """读取 ``TRIMMED_CURVE`` 的参数区间；没有就用默认全域。"""

        entity = self.entity(value)
        if entity is None or entity.keyword != "TRIMMED_CURVE":
            return default_low, default_high
        low = self._trim_value(entity.arg(1), default_low)
        high = self._trim_value(entity.arg(2), default_high)
        sense = as_bool(entity.arg(4), True)
        if not sense:
            low, high = high, low
        if high <= low:
            low, high = default_low, default_high
        return low, high

    def _trim_value(self, value: Any, default: float) -> float:
        if isinstance(value, Ref):
            entity = self.entity(value)
            if entity is None:
                return default
            if entity.keyword == "CARTESIAN_POINT":
                point = self.point3(value)
                return default if point is None else float(np.linalg.norm(point))
            if entity.keyword == "PARAMETER_VALUE":
                return as_float(entity.arg(1), default)
            return default
        return as_float(value, default)

    # -- 曲面 --------------------------------------------------------------
    def surface(self, value: Any) -> Surface | None:
        entity = self.entity(value)
        if entity is None:
            return None
        cache_key = ("surface", entity.id)
        if cache_key in self._cache:
            return self._cache[cache_key]
        surface = self._build_surface(entity)
        self._cache[cache_key] = surface
        return surface

    def _build_surface(self, entity: StepEntity) -> Surface | None:
        keyword = entity.keyword
        if keyword == "PLANE":
            matrix = self.placement(entity.arg(1))
            return None if matrix is None else Plane(matrix)
        if keyword == "CYLINDRICAL_SURFACE":
            matrix = self.placement(entity.arg(1))
            radius = as_float(entity.arg(2))
            if matrix is None or radius <= 0:
                return None
            return Cylinder(matrix, radius)
        if keyword == "CONICAL_SURFACE":
            matrix = self.placement(entity.arg(1))
            radius = as_float(entity.arg(2))
            semi_angle = as_float(entity.arg(3))
            if matrix is None:
                return None
            return Cone(matrix, radius, semi_angle)
        if keyword == "SPHERICAL_SURFACE":
            matrix = self.placement(entity.arg(1))
            radius = as_float(entity.arg(2))
            if matrix is None or radius <= 0:
                return None
            return Sphere(matrix, radius)
        if keyword == "TOROIDAL_SURFACE":
            matrix = self.placement(entity.arg(1))
            major = as_float(entity.arg(2))
            minor = as_float(entity.arg(3))
            if matrix is None or major <= 0 or minor <= 0:
                return None
            return Torus(matrix, major, minor)
        if keyword in {"B_SPLINE_SURFACE", "B_SPLINE_SURFACE_WITH_KNOTS", "RATIONAL_B_SPLINE_SURFACE",
                       "BEZIER_SURFACE", "UNIFORM_SURFACE", "QUASI_UNIFORM_SURFACE"}:
            return self._build_bspline_surface(entity)
        if keyword in {"SURFACE_OF_REVOLUTION", "SURFACE_OF_LINEAR_EXTRUSION", "SWEPT_SURFACE",
                       "OFFSET_SURFACE", "CURVE_BOUNDED_SURFACE", "RECTANGULAR_TRIMMED_SURFACE"}:
            self._unsupported_surfaces[keyword] = self._unsupported_surfaces.get(keyword, 0) + 1
            return None
        self._unsupported_surfaces[keyword] = self._unsupported_surfaces.get(keyword, 0) + 1
        return None

    def _build_bspline_surface(self, entity: StepEntity, offset: int = 0) -> BSplineSurface | None:
        u_degree = as_int(entity.arg(offset + 1))
        v_degree = as_int(entity.arg(offset + 2))
        raw_grid = as_sequence(entity.arg(offset + 3))
        rows: list[list[Vec3]] = []
        for row in raw_grid:
            points = [self.point3(item) for item in as_sequence(row)]
            if any(point is None for point in points):
                return None
            rows.append([point for point in points if point is not None])
        if not rows or len(rows) <= u_degree or len(rows[0]) <= v_degree:
            return None
        if any(len(row) != len(rows[0]) for row in rows):
            return None
        control = np.array(rows, dtype=np.float64)
        u_multiplicity = as_numbers(entity.arg(offset + 8))
        u_values = as_numbers(entity.arg(offset + 9))
        v_multiplicity = as_numbers(entity.arg(offset + 10))
        v_values = as_numbers(entity.arg(offset + 11))
        if len(control.shape) != 3:
            return None
        if "RATIONAL" in entity.components or entity.keyword.startswith("RATIONAL"):
            raw = self._weights(entity, offset + 12, control.shape[0] * control.shape[1])
            weights = raw.reshape(control.shape[0], control.shape[1]) if raw.size == control.shape[0] * control.shape[1] \
                else np.ones(control.shape[:2], dtype=np.float64)
        else:
            weights = np.ones(control.shape[:2], dtype=np.float64)
        u_knots = freeform.expand_knots(u_multiplicity, u_values, control.shape[0], u_degree) \
            if u_values else freeform.uniform_knots(control.shape[0], u_degree)
        v_knots = freeform.expand_knots(v_multiplicity, v_values, control.shape[1], v_degree) \
            if v_values else freeform.uniform_knots(control.shape[1], v_degree)
        return BSplineSurface(
            control_points=control, weights=weights, u_knots=u_knots, v_knots=v_knots,
            u_degree=u_degree, v_degree=v_degree,
            u_range=(float(u_knots[u_degree]), float(u_knots[-1 - u_degree])),
            v_range=(float(v_knots[v_degree]), float(v_knots[-1 - v_degree])),
        )

    # -- 报告 --------------------------------------------------------------
    def unsupported_report(self) -> dict[str, int]:
        report = {f"surface:{key}": value for key, value in self._unsupported_surfaces.items()}
        report.update({f"curve:{key}": value for key, value in self._unsupported_curves.items()})
        return report


# ------------------------------------------------------------------ 边/环
@dataclass(slots=True)
class EdgeRecord:
    """一条边的几何与端点（用于边界离散与拓扑统计）。"""

    id: int
    curve: Curve | None
    start: Vec3
    end: Vec3
    same_sense: bool = True
    length_mm: float = 0.0

    def sample(self, count: int = 12) -> NDArray[np.float64]:
        count = max(2, count)
        if self.curve is None or isinstance(self.curve, Line):
            # 直线段直接按两个端点线性插值：既精确，也不会被曲线自身的参数域带到别处。
            start = np.asarray(self.start, dtype=np.float64).reshape(3)
            end = np.asarray(self.end, dtype=np.float64).reshape(3)
            steps = np.linspace(0.0, 1.0, count).reshape(-1, 1)
            return start + steps * (end - start)
        points = self.curve.sample(count)
        if not self.same_sense:
            points = points[::-1]
        # 用顶点把曲线的两端"钉住"，避免曲线参数区间与顶点不一致时出现缝隙。
        points = np.array(points, dtype=np.float64)
        points[0] = self.start
        points[-1] = self.end
        return points

    def approximate_length(self) -> float:
        points = self.sample(24)
        return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())


def edge_length_hint(curve: Curve | None, start: Vec3, end: Vec3) -> float:
    """由曲线类型估算长度，用于决定采样密度。"""

    if curve is None:
        return float(np.linalg.norm(end - start))
    if isinstance(curve, Line):
        # 直线段的长度就是两端点距离。不要去用参数区间：直线实体本身是无界的，
        # 它的 low/high 与这条边的实际长度没有必然关系。
        return float(np.linalg.norm(np.asarray(end, dtype=np.float64)
                                    - np.asarray(start, dtype=np.float64)))
    if isinstance(curve, Circle):
        return abs(curve.high - curve.low) * curve.radius
    if isinstance(curve, Ellipse):
        mean_radius = 0.5 * (curve.semi_x + curve.semi_y)
        return abs(curve.high - curve.low) * mean_radius
    if isinstance(curve, BSplineCurve):
        return float(np.linalg.norm(np.diff(curve.control_points, axis=0), axis=1).sum())
    return float(np.linalg.norm(end - start))


def chord_count(length_mm: float, *, tolerance_mm: float = 0.12, minimum: int = 2,
                maximum: int = 96) -> int:
    """把一段曲线按弦高误差离散成多少段（经验公式，够用且可控）。"""

    if length_mm <= 1e-9:
        return minimum
    count = int(ceil(sqrt(max(length_mm, 1e-6) / max(tolerance_mm, 1e-6)) * 2.0))
    return int(min(maximum, max(minimum, count)))


__all__ = [
    "BSplineCurve",
    "BSplineSurface",
    "Circle",
    "CompositeCurve",
    "Cone",
    "Curve",
    "Cylinder",
    "EdgeRecord",
    "Ellipse",
    "Line",
    "Plane",
    "Resolver",
    "Sphere",
    "Surface",
    "Torus",
    "chord_count",
    "edge_length_hint",
    "placement_matrix",
    "transform_points",
]
