"""加工面的**高度场**：任意 (x, y) 处的"面有多高"。

2.5 轴的平面铣 / 型腔铣只需要一个常数（``floor_z``），因为加工面是水平的。
一旦型腔底是**斜面**或**曲面**，同一层刀路在不同位置的高度就不同了：
刀轴 Z 必须逐点取自面本身，否则要么切不到位、要么直接扎进工件。

本模块把"选中面"变成一张规则高度场，成为整个 CAM 层唯一的"面几何"来源：

* **平面**（含斜面）用平面方程解析求值 —— ``z = (d - nx·x - ny·y) / nz``，
  与离散精度无关，是对倾斜底面的精确表达；
* **曲面**（圆柱、圆锥、B 样条……）用该面的三角网格光栅化成高度场，
  之后一律走双线性插值。曲面面片本来就是直线近似，所以这里不引入额外误差。

网格只覆盖**加工区域外扩一圈**的范围，不铺满整个零件：
一张 100×80 的板配 0.5 mm 网格，整零件是 3.2 万格，按面就只剩几千格。

约定：网格值 ``floor[i, j]`` 位于 ``(x0 + (i+0.5)·cell, y0 + (j+0.5)·cell)``，
与 :class:`~toolpath_lab.cam.boundary.MachiningRegion` 的栅格约定一致。

**法向与斜率也一起算出来**，因为"刀具会不会扎进面里"取决于面的坡度：
平底刀在斜面上必须抬高，抬多少由坡度和刀具形状共同决定（见
:mod:`toolpath_lab.cam.surface_pocket`）。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError

#: 低于这个坡度就当作水平面（nz ≥ 0.9995 ≈ 1.8°）：避免把浮点噪声当成斜面。
FLAT_SLOPE_TOLERANCE = 1e-3
#: 高度场网格的默认分辨率（mm）。跟随加工区域栅格即可，不需要更细。
DEFAULT_FLOOR_CELL_MM = 0.5
#: 高度场分辨率下限/上限（mm）：太细会爆内存，太粗会看不出斜面。
MIN_FLOOR_CELL_MM = 0.05
MAX_FLOOR_CELL_MM = 5.0
#: 判定"这个三角形属于加工底面"所需的最小法向 Z 分量（≈ 17° 以内算底面）。
#: 腔壁的三角面片即使平均法向偏上，逐三角形的真实法向也接近水平，会被这一条挡掉。
MIN_FLOOR_NORMAL_Z = 0.3


def _bilinear(values: NDArray[np.float64], bounds: tuple[float, float, float, float],
              cell: float, px: NDArray[np.float64],
              py: NDArray[np.float64]) -> NDArray[np.float64]:
    """规则网格上的双线性插值（超出范围夹到边缘）。

    :meth:`FloorField.height_at` 与 :meth:`FloorField.normal_at` 共用这一份实现：
    梯度和高度必须用**完全相同**的插值方式，否则法向会在格子边界上跳变。
    """

    fi = np.clip((px - bounds[0]) / cell - 0.5, 0.0, float(values.shape[0] - 1))
    fj = np.clip((py - bounds[1]) / cell - 0.5, 0.0, float(values.shape[1] - 1))
    i0 = np.floor(fi).astype(np.int64)
    j0 = np.floor(fj).astype(np.int64)
    i1 = np.minimum(i0 + 1, values.shape[0] - 1)
    j1 = np.minimum(j0 + 1, values.shape[1] - 1)
    ti = fi - i0
    tj = fj - j0
    return (values[i0, j0] * (1 - ti) * (1 - tj) + values[i0, j1] * (1 - ti) * tj
            + values[i1, j0] * ti * (1 - tj) + values[i1, j1] * ti * tj)


@dataclass(slots=True)
class FloorField:
    """选中面的高度场（加工面在任意 XY 处的 Z）。

    ``values`` 可以是常数（水平面）或一张规则网格；``grad`` 只在网格形式下有意义。
    调用方通过 :meth:`height_at` / :meth:`max_in_disk` 取值，不必关心底层是哪种形式 ——
    这正是"水平型腔照旧走原来的常数逻辑、斜面曲面走高度场"能够共用一条代码路径的原因。
    """

    #: (x0, y0, x1, y1)，网格节点的**外边界**（不是单元中心）
    bounds: tuple[float, float, float, float]
    cell_mm: float
    values: NDArray[np.float64]
    #: 面法向（朝上，nz > 0）
    normal: NDArray[np.float64]
    planar: bool = True
    #: dZ/dX、dZ/dY（仅网格形式；常数场为 None）
    grad: tuple[NDArray[np.float64], NDArray[np.float64]] | None = None
    #: 生成方式说明，写进工序备注（"解析平面" / "三角网格光栅化"）
    source: str = ""
    #: 网格可能漏掉的小特征提示（例如面片小于一个格子）
    notes: list[str] | None = None
    #: :attr:`is_flat` 的缓存（不参与构造/比较：几何在构造后不再变化）
    flat_cache: bool | None = field(default=None, init=False, repr=False, compare=False)

    # -- 形态 --------------------------------------------------------------
    @property
    def is_flat(self) -> bool:
        """整张面是不是水平的。**算一次就缓存**（几何构造后不变，见下方实现）。

        **不能只看面记录的法向**：圆柱/圆锥/B 样条这类曲面，面记录的法向可能是
        (0,0,1)（例如按参数取到的那个方向），而它的高度场明显有坡度——test1.STEP 里
        的圆柱腔底就是这样：法向说"水平"，实测局部坡度 67°。只看法向会把整张曲面
        当成水平底面，层高裁剪、防过切抬升全部失效（底面切不到、刀路还是错的）。

        所以网格形式一律以**实际梯度**为准，只有常量场（真正的水平面）才退回法向判断。

        结果**缓存在 :attr:`flat_cache`**：这张场构造之后几何不再变，而规划里要按层、
        按环反复问"底面平不平"（实测一次型腔规划要问几百次），每次都把整张梯度场
        过一遍（几十万格的 hypot + 掩码扫描）是纯浪费——占了刀路生成两成多的时间。
        """

        cached = self.flat_cache
        if cached is None:
            if self.grad is not None and self.values.ndim == 2:
                grad_x, grad_y = self.grad
                if grad_x.size == 0 or grad_y.size == 0:
                    cached = True
                else:
                    slope = np.hypot(np.asarray(grad_x, dtype=np.float64),
                                     np.asarray(grad_y, dtype=np.float64))
                    finite = slope[np.isfinite(slope)]
                    cached = True if finite.size == 0 else bool(
                        float(finite.max()) <= FLAT_SLOPE_TOLERANCE)
            else:
                cached = bool(abs(float(self.normal[2])) >= 1.0 - FLAT_SLOPE_TOLERANCE)
            self.flat_cache = cached
        return cached

    @property
    def z_min(self) -> float:
        return float(self.values.min()) if self.values.size else 0.0

    @property
    def z_max(self) -> float:
        return float(self.values.max()) if self.values.size else 0.0

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.values.shape[0]), int(self.values.shape[1])) if self.values.ndim == 2 \
            else (0, 0)

    def describe(self) -> dict[str, Any]:
        """给界面/接口看的摘要。"""

        payload: dict[str, Any] = {
            "planar": bool(self.planar),
            "flat": self.is_flat,
            "source": self.source,
            "cell_mm": round(float(self.cell_mm), 4),
            "grid": list(self.shape),
            "z_min": round(self.z_min, 4),
            "z_max": round(self.z_max, 4),
        }
        if not self.is_flat:
            payload["max_slope_deg"] = round(self.max_slope_deg(), 3)
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload

    # -- 取值 --------------------------------------------------------------
    def height_at(self, x: NDArray[np.float64] | float,
                  y: NDArray[np.float64] | float) -> NDArray[np.float64]:
        """双线性插值取高度；落在网格外时夹到边缘（调用方保证区域在网格内）。"""

        px = np.asarray(x, dtype=np.float64)
        py = np.asarray(y, dtype=np.float64)
        if self.values.ndim != 2 or self.cell_mm <= 1e-12:
            # 常数场（水平面）：没有栅格可插值，直接返回那个常数
            flat = float(self.values.reshape(-1)[0]) if self.values.size else 0.0
            return np.full(np.broadcast(px, py).shape, flat, dtype=np.float64)
        return _bilinear(self.values, self.bounds, self.cell_mm, px, py)

    def normal_at(self, x: NDArray[np.float64] | float,
                  y: NDArray[np.float64] | float) -> NDArray[np.float64]:
        """单位法向，形状为 ``broadcast(x, y).shape + (3,)``。

        网格形式下由梯度算出，常数场直接返回自身法向。
        """

        flat = np.asarray(self.normal, dtype=np.float64).reshape(3)
        if self.grad is None:
            return np.broadcast_to(flat, np.broadcast(x, y).shape + (3,)).copy()
        # 梯度必须**先展成一维再拼列**：直接把 (n, m) 的三个数组 column_stack，
        # 得到的是 (n, 3m)，再 reshape(-1, 3) 就把不同列的值混进同一个法向里——
        # 法向会出现 nz≈0，坡度算到 2e7，抬升跟着变成 6e7，于是那一带的格点被
        # "层高裁剪"永久排除在加工区之外（掩码里出现莫名其妙的空洞，实测踩过）。
        shape = np.broadcast(np.asarray(x, dtype=np.float64),
                             np.asarray(y, dtype=np.float64)).shape
        px = np.asarray(x, dtype=np.float64).reshape(-1)
        py = np.asarray(y, dtype=np.float64).reshape(-1)
        grad_x, grad_y = self.grad
        gx = np.asarray(_bilinear(grad_x, self.bounds, self.cell_mm, px, py),
                        dtype=np.float64).reshape(-1)
        gy = np.asarray(_bilinear(grad_y, self.bounds, self.cell_mm, px, py),
                        dtype=np.float64).reshape(-1)
        normals = np.column_stack((-gx, -gy, np.ones_like(gx)))
        norms = np.linalg.norm(normals, axis=1, keepdims=True)
        return (normals / np.maximum(norms, 1e-12)).reshape(shape + (3,))

    def max_slope_deg(self) -> float:
        """面上的最大坡度（相对水平面，0 = 水平，90 = 垂直）。"""

        if self.grad is None:
            nz = abs(float(self.normal[2]))
            nz = min(1.0, max(1e-9, nz))
            return float(np.degrees(np.arccos(nz)))
        gx, gy = self.grad
        slope = np.hypot(gx, gy)
        return float(np.degrees(np.arctan(np.nanmax(slope) if slope.size else 0.0)))

    def max_in_disk(self, x: float, y: float, radius: float) -> float:
        """以 (x, y) 为心、radius 为半径的圆内**最高**的面高度。

        刀具接触点判断的核心：面是曲面/斜面时，同一 XY 位置上"刀具最低点能贴到哪"
        取决于整个刀底足迹内的最高点，而不是中心点的高度。半径 0 时退化为取中心高度。
        """

        if radius <= 1e-9 or self.values.ndim != 2:
            return float(self.height_at(x, y))
        # 需要覆盖圆的最小窗口半宽（节点数）
        half = int(np.ceil(radius / self.cell_mm)) + 1
        px = (np.asarray([x], dtype=np.float64) - self.bounds[0]) / self.cell_mm - 0.5
        py = (np.asarray([y], dtype=np.float64) - self.bounds[1]) / self.cell_mm - 0.5
        i0 = int(np.floor(float(px[0])))
        j0 = int(np.floor(float(py[0])))
        i_lo, i_hi = i0 - half, i0 + half + 1
        j_lo, j_hi = j0 - half, j0 + half + 1
        if i_hi <= 0 or j_hi <= 0 or i_lo >= self.values.shape[0] or j_lo >= self.values.shape[1]:
            return float(self.height_at(x, y))
        i_lo = max(i_lo, 0)
        j_lo = max(j_lo, 0)
        i_hi = min(i_hi, self.values.shape[0])
        j_hi = min(j_hi, self.values.shape[1])
        block = self.values[i_lo:i_hi, j_lo:j_hi]
        nodes_i = np.arange(i_lo, i_hi, dtype=np.float64)
        nodes_j = np.arange(j_lo, j_hi, dtype=np.float64)
        # 节点 -> 世界坐标（节点 k 对应 x0 + (k + 0.5) * cell）
        world_x = self.bounds[0] + (nodes_i + 0.5) * self.cell_mm
        world_y = self.bounds[1] + (nodes_j + 0.5) * self.cell_mm
        inside = ((world_x[:, None] - x) ** 2 + (world_y[None, :] - y) ** 2) <= radius ** 2
        if not inside.any():
            return float(self.height_at(x, y))
        return float(block[inside].max())


@dataclass(slots=True)
class FloorSurface:
    """加工底面的完整描述：高度场 + 加工轮廓 + （可选）面片掩码。"""

    floor: FloorField
    #: 加工轮廓（外轮廓，CCW）与岛屿（CW）
    outline: NDArray[np.float64]
    islands: tuple[NDArray[np.float64], ...] = ()
    #: 底面被切成许多小平面片、选中那一片的边界环不足以代表整块底面时，
    #: 这里给出"底面面片光栅化"的掩码，由调用方在同一张栅格上直接用（见 build_region）
    inside_mask: NDArray[np.bool_] | None = None

    def describe(self) -> dict[str, Any]:
        payload = self.floor.describe()
        payload["outline_points"] = int(np.asarray(self.outline).shape[0])
        payload["islands"] = len(self.islands)
        payload["mask_recovered"] = self.inside_mask is not None
        return payload


def floor_surface_from_face(part: Any, face_id: int, *, cell_mm: float = DEFAULT_FLOOR_CELL_MM,
                            outline: NDArray[np.float64] | None = None,
                            islands: Sequence[NDArray[np.float64]] = (),
                            margin_cells: int = 3) -> FloorSurface:
    """由选中面构造完整底面：高度场 + 加工轮廓。

    **选中面只是"用户点中的那一块"**，底面的真实范围常常比它大：

    * 斜底型腔在 STEP 里就是一个平面面 —— 直接读平面方程，精确；
    * 曲底型腔往往被离散成几十个小平面片，每一片的边界环只是它自己那一小条；
      用它们拼轮廓既不准（相邻片只共享边、并集里会留缝），也撑不起整个型腔。
      这时改用"底面面片光栅化 → 跟踪边界"得到轮廓，并把掩码一并交出去，
      让加工区域直接用同一张栅格上的掩码（见 :func:`~toolpath_lab.cam.boundary.build_region`）。
    """

    record = part.face(int(face_id))
    if record is None:
        raise PlanningError(f"找不到序号为 {face_id} 的面")
    normal = np.asarray(record.normal, dtype=np.float64).reshape(3)
    if normal[2] <= 0.0:
        raise PlanningError(
            f"面 #{face_id} 的法向不朝上（{tuple(round(float(v), 3) for v in normal)}），"
            "只有朝上的面才能作为型腔底加工"
        )

    cell = float(np.clip(float(cell_mm), MIN_FLOOR_CELL_MM, MAX_FLOOR_CELL_MM))
    loops = _project_loops(record.loops)
    # 连通聚类的搜索范围：先按该面的边界环，再按零件尺寸放宽（曲面底被切成几十片时，
    # 每一片的环都只是自己那一小条，不放宽就只能拿到一片）。
    # 放宽只是"允许被连上"，真正的网格范围由下面的 footprint 决定。
    loose = _loosen_bounds(_bounds_of(None, (), loops), part)
    triangles = _gather_floor_triangles(part, record, normal, loose)
    if triangles is None:
        raise PlanningError(f"面 #{face_id} 没有可用的三角面片，无法建立加工底面的高度场")
    points, indices = triangles
    covered = points[np.unique(indices.reshape(-1))]
    covered_bounds = _bounds_of_arrays([covered[:, :2]])

    # 网格范围 = **加工区域**（调用方给的轮廓；没有就用面片实际范围）再外放一点：
    #
    # * 外放是为了刀心能走到轮廓外一个刀半径时足迹仍在场内；
    # * 不能放太多：离散后的曲面底与腔壁在边缘会重叠，网格铺得越宽，
    #   被壁面/圆角污染的格子越多（实测局部坡度会虚高到 79°、z 范围偏大）。
    source = outline if outline is not None else covered[:, :2]
    footprint = _bounds_of(source, islands, ())
    span = max(footprint[2] - footprint[0], footprint[3] - footprint[1])
    pad = max(0.25 * span, 4.0 * cell)
    field_bounds = (
        min(footprint[0] - pad, covered_bounds[0]), min(footprint[1] - pad, covered_bounds[1]),
        max(footprint[2] + pad, covered_bounds[2]), max(footprint[3] + pad, covered_bounds[3]),
    )

    plane = _coplanar_plane(points, indices)
    if plane is not None:
        field = _planar_field(plane, field_bounds, cell, margin_cells)
        field.notes = _planar_notes(record, plane,
                                    abs(float(plane[2])) >= 1.0 - FLAT_SLOPE_TOLERANCE)
        # 共面且选中面的轮廓已经覆盖底面（面积相当）时，用调用方给的轮廓：
        # 那是 BRep 的解析边界，比栅格跟踪出来的更准。否则用面片掩码补轮廓。
        mask = None
        if outline is not None and not _contour_covers(points, indices, outline):
            mask = _mask_from_triangles(points, indices, field_bounds, cell, margin_cells)
        return FloorSurface(floor=field, outline=np.asarray(outline, dtype=np.float64)
                            if outline is not None else covered[:, :2].copy(),
                            islands=tuple(islands), inside_mask=mask)

    field = _mesh_field(part, record, normal, field_bounds, cell, margin_cells,
                        points, indices)
    mask = _mask_from_triangles(points, indices, field_bounds, cell, margin_cells)
    traced_outline, traced_islands = _contour_from_mask(mask, field_bounds, cell, margin_cells)
    return FloorSurface(floor=field, outline=traced_outline, islands=traced_islands,
                        inside_mask=mask)


def _loosen_bounds(bounds: tuple[float, float, float, float], part: Any
                   ) -> tuple[float, float, float, float]:
    """把范围按零件尺寸放宽（用于"选中面只是整张底面的一小片"的情形）。

    放宽量取零件 X/Y 尺寸的一半：同一个型腔一定在这个范围内，
    而放宽到整张零件也不会真的收进别的东西 —— 连通聚类只认与选中面片相连的面片。
    """

    try:
        size = part.size
        span = max(float(size[0]), float(size[1]))
    except (AttributeError, TypeError, IndexError):  # pragma: no cover - 非常规 part
        span = 0.0
    pad = max(0.5 * span, 10.0)
    return (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)


def _bounds_of_arrays(arrays: Sequence[NDArray[np.float64]]
                      ) -> tuple[float, float, float, float]:
    stacked = np.vstack([np.asarray(item, dtype=np.float64).reshape(-1, 2) for item in arrays])
    return (float(stacked[:, 0].min()), float(stacked[:, 1].min()),
            float(stacked[:, 0].max()), float(stacked[:, 1].max()))


def _contour_covers(points: NDArray[np.float64], indices: NDArray[np.int64],
                    outline: NDArray[np.float64]) -> bool:
    """选中面的外轮廓是否已经覆盖了（几乎）全部底面面片？

    判据：面片 XY 包围盒的并集必须基本落在轮廓的包围盒里，且轮廓面积不小于面片面积。
    面片只是整块底面的一小条时，面积会差一个数量级，很容易判出来。
    """

    triangles = points[indices]
    face_area = 0.0
    for index in range(int(indices.shape[0])):
        a, b, c = triangles[index]
        face_area += abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0
    polygon = np.asarray(outline, dtype=np.float64)
    x = polygon[:, 0]
    y = polygon[:, 1]
    polygon_area = abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2.0
    if polygon_area <= 1e-9:
        return False
    covered = points[np.unique(indices.reshape(-1))]
    span_ok = (covered[:, 0].min() >= x.min() - 1e-6 and covered[:, 0].max() <= x.max() + 1e-6
               and covered[:, 1].min() >= y.min() - 1e-6 and covered[:, 1].max() <= y.max() + 1e-6)
    return bool(span_ok and face_area >= 0.9 * polygon_area)


def _mask_from_triangles(positions: NDArray[np.float64], triangles: NDArray[np.int64],
                         bounds: tuple[float, float, float, float], cell: float,
                         margin_cells: int) -> NDArray[np.bool_]:
    """把底面面片在 XY 上光栅化成掩码（栅格约定与 MachiningRegion 一致）。"""

    x0, y0, x1, y1, gx, gy = _grid_axes(bounds, cell, margin_cells)
    xs = x0 + (np.arange(gx, dtype=np.float64) + 0.5) * cell
    ys = y0 + (np.arange(gy, dtype=np.float64) + 0.5) * cell
    mask = np.zeros((gx, gy), dtype=bool)
    corners = positions[triangles]
    for index in range(int(triangles.shape[0])):
        triangle = corners[index]
        lo_i = int(max(0, np.floor((triangle[:, 0].min() - x0) / cell - 0.5)))
        hi_i = int(min(gx - 1, np.ceil((triangle[:, 0].max() - x0) / cell - 0.5)))
        lo_j = int(max(0, np.floor((triangle[:, 1].min() - y0) / cell - 0.5)))
        hi_j = int(min(gy - 1, np.ceil((triangle[:, 1].max() - y0) / cell - 0.5)))
        if hi_i < lo_i or hi_j < lo_j:
            continue
        block_x, block_y = np.meshgrid(xs[lo_i:hi_i + 1], ys[lo_j:hi_j + 1], indexing="ij")
        a, b, c = triangle[0], triangle[1], triangle[2]
        denominator = ((b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]))
        if abs(float(denominator)) < 1e-12:
            continue
        wa = ((b[1] - c[1]) * (block_x - c[0]) + (c[0] - b[0]) * (block_y - c[1])) / denominator
        wb = ((c[1] - a[1]) * (block_x - c[0]) + (a[0] - c[0]) * (block_y - c[1])) / denominator
        wc = 1.0 - wa - wb
        mask[lo_i:hi_i + 1, lo_j:hi_j + 1] |= ((wa >= -1e-9) & (wb >= -1e-9) & (wc >= -1e-9))
    return mask


def _contour_from_mask(mask: NDArray[np.bool_], bounds: tuple[float, float, float, float],
                       cell: float, margin_cells: int
                       ) -> tuple[NDArray[np.float64], tuple[NDArray[np.float64], ...]]:
    """由掩码跟踪出外轮廓与内孔（用边界跟踪，不依赖多边形布尔）。

    相邻面片在 2D 并集里只共享一条边，"逐片并集"会留下缝；先光栅化再跟踪边界
    就没有这个问题，代价是轮廓精度为半个栅格（与 2.5 轴区域本来就有的栅格精度一致）。
    """

    try:
        from toolpath_lab.cam.boundary import _polygon_area, _trace_mask_boundaries
    except ImportError:  # pragma: no cover - 循环导入的兜底
        return _bounds_polygon(bounds), ()
    x0, y0 = bounds[0] - margin_cells * cell, bounds[1] - margin_cells * cell
    rings: list[tuple[float, NDArray[np.float64]]] = []
    for pixels in _trace_mask_boundaries(mask):
        if len(pixels) < 4:
            continue
        points = np.asarray([(x0 + i * cell, y0 + j * cell) for i, j in pixels], dtype=np.float64)
        if points.shape[0] < 3:
            continue
        rings.append((abs(_polygon_area(points)), points))
    if not rings:  # pragma: no cover - 掩码为空
        return _bounds_polygon(bounds), ()
    rings.sort(key=lambda item: item[0], reverse=True)
    outline = _to_ccw(rings[0][1])
    islands = tuple(_to_ccw(points)[::-1] for area, points in rings[1:] if area > 1.0)
    return outline, islands


def _to_ccw(points: NDArray[np.float64]) -> NDArray[np.float64]:
    x = points[:, 0]
    y = points[:, 1]
    area = float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0
    return points if area >= 0 else points[::-1].copy()


def _bounds_polygon(bounds: tuple[float, float, float, float]) -> NDArray[np.float64]:
    x0, y0, x1, y1 = bounds
    return np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)


# ------------------------------------------------------------------ 构造
def constant_floor(floor_z: float) -> FloorField:
    """水平面的高度场（2.5 轴的老行为，用同一套接口表达）。"""

    value = float(floor_z)
    return FloorField(
        bounds=(0.0, 0.0, 1.0, 1.0),
        cell_mm=0.0,
        values=np.asarray([[value]], dtype=np.float64),
        normal=np.asarray([0.0, 0.0, 1.0], dtype=np.float64),
        planar=True,
        source="常量（水平面）",
        notes=[],
    )


def floor_field_from_face(part: Any, face_id: int, *, cell_mm: float = DEFAULT_FLOOR_CELL_MM,
                          outline: NDArray[np.float64] | None = None,
                          islands: Sequence[NDArray[np.float64]] = (),
                          margin_cells: int = 3) -> FloorField:
    """由选中面构造加工底面的高度场（只要高度场时的便捷入口）。

    完整信息（高度场 + 加工轮廓 + 面片掩码）见
    :func:`floor_surface_from_face`；这里只是把它裁成高度场，
    两条路径共用同一份实现 —— 早先这里有一份**独立实现**，两边的范围算法悄悄分叉，
    曲底型腔从一个面片出发会得到一张 9 格宽的窄场（表面积正常、几何全错）。
    """

    return floor_surface_from_face(
        part, face_id, cell_mm=cell_mm, outline=outline, islands=islands,
        margin_cells=margin_cells,
    ).floor


def _gather_floor_triangles(part: Any, record: Any, normal: NDArray[np.float64],
                            region_bounds: tuple[float, float, float, float]
                            ) -> tuple[NDArray[np.float64], NDArray[np.int64]] | None:
    """把**与选中面相连、且朝上**的面片收齐，返回 (顶点, 三角形索引)。

    为什么要"相连"：曲底型腔的底面在 STEP 里是一串小平面片，但同一块板顶面往往也在
    加工区域的 XY 范围内 —— 只按 XY 范围收会把顶面一起卷进来，底面就不是同一张面了。
    这里先按共享边把面片聚类，再看哪一类与用户点中的面相连。

    只收朝上的面片：腔壁是竖直面，不该参与底面高度场。
    """

    mesh = part.mesh
    positions = np.asarray(mesh.positions, dtype=np.float64)
    indices = np.asarray(mesh.indices, dtype=np.int64)
    if indices.size == 0 or positions.size == 0:
        return None

    start = int(record.triangle_start)
    stop = start + int(record.triangle_count)
    if stop <= start:
        return None

    upward: list[int] = []
    for face in mesh.faces:
        face_normal = np.asarray(face.normal, dtype=np.float64)
        if face_normal[2] < 0.05:
            continue
        low = int(face.triangle_start)
        high = low + int(face.triangle_count)
        upward.extend(range(low, high))
    if not upward:
        return None
    pool = np.asarray(sorted(set(upward)), dtype=np.int64)

    x_lo, y_lo, x_hi, y_hi = region_bounds
    triangles = indices[pool]
    points = positions[triangles]
    centred = points.mean(axis=1)
    # 逐三角形的**真实法向**必须朝上。面记录的法向只在面内部有效：曲底型腔的腔壁
    # 由许多小面片拼成，部分面片的平均法向偏上（nz>0.05）却其实是竖直壁，
    # 混进底面高度场会把掩码与轮廓一起撑大（踩过一次）。
    edge1 = points[:, 1] - points[:, 0]
    edge2 = points[:, 2] - points[:, 0]
    normals = np.cross(edge1, edge2)
    lengths = np.linalg.norm(normals, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        up = np.where(lengths > 1e-12, normals[:, 2] / np.maximum(lengths, 1e-30), 0.0)
    inside = ((centred[:, 0] >= x_lo - 1e-6) & (centred[:, 0] <= x_hi + 1e-6)
              & (centred[:, 1] >= y_lo - 1e-6) & (centred[:, 1] <= y_hi + 1e-6)
              & (np.abs(up) >= MIN_FLOOR_NORMAL_Z))
    pool = pool[inside]
    if pool.size == 0:
        return None

    # 共享边聚类：每条边（两个端点的坐标）最多连两个面片。
    # 注意**必须按坐标**配对，不能按顶点下标：STEP 转 BRep 之后，同一位置的顶点
    # 常常是各自独立的（TShape 不共享），按下标配会发现"一片都不相连"，
    # 于是整张曲面被当成单个小平面片（曲底型腔就这么踩过一次）。
    quantum = 1e-4

    def _key(point: NDArray[np.float64]) -> tuple[int, int, int]:
        return (int(round(float(point[0]) / quantum)),
                int(round(float(point[1]) / quantum)),
                int(round(float(point[2]) / quantum)))

    edges: dict[tuple[tuple[int, int, int], tuple[int, int, int]], list[int]] = {}
    for slot, triangle_index in enumerate(pool):
        corners = positions[indices[int(triangle_index)]]
        for offset in range(3):
            first = _key(corners[offset])
            second = _key(corners[(offset + 1) % 3])
            key = (first, second) if first < second else (second, first)
            edges.setdefault(key, []).append(slot)
    neighbours: dict[int, list[int]] = {}
    for slots in edges.values():
        for first in slots:
            for second in slots:
                if first != second:
                    neighbours.setdefault(first, []).append(second)

    seed = None
    for slot, triangle_index in enumerate(pool):
        if int(triangle_index) == start:
            seed = slot
            break
    if seed is None:
        # 选中的面在加工区域里一片面片都没有（区域由别的面算出来）：退回到取最靠近的
        seed = 0

    # 从种子面片出发，沿共享边扩散。曲底型腔的底面是几十个小平面片拼起来的，
    # 只取种子自己那一片会把整张曲面误判成一个小斜面，必须整片连起来看。
    # 扩散时不按法向过滤：底面本来就是靠法向变化表达曲率的。
    visited = {seed}
    stack = [seed]
    while stack:
        current = stack.pop()
        for neighbour in neighbours.get(current, ()):
            if neighbour not in visited:
                visited.add(neighbour)
                stack.append(neighbour)
    chosen = pool[np.asarray(sorted(visited), dtype=np.int64)]
    return positions, indices[chosen]


def _coplanar_plane(points: NDArray[np.float64], indices: NDArray[np.int64]
                    ) -> NDArray[np.float64] | None:
    """所有面片共面时返回该平面 ``(nx, ny, nz, d)``（法向朝上），否则 None。

    判据是"每个三角形顶点到拟合平面的距离都小于容差"。共面说明用户选中的是
    一整块平面（哪怕它被切成了很多片）；不共面说明底面是曲面，必须走高度场。
    """

    if indices.shape[0] == 0:
        return None
    first = points[indices[0]]
    edge1 = first[1] - first[0]
    edge2 = first[2] - first[0]
    normal = np.cross(edge1, edge2)
    length = float(np.linalg.norm(normal))
    if length < 1e-12:
        return None
    normal = normal / length
    vertices = points[np.unique(indices.reshape(-1))]
    distance = vertices @ normal
    spread = float(distance.max() - distance.min())
    # 平面度容差：离散精度通常在 1e-2 mm 量级，1e-3 足以区分"共面"与"曲面"
    if spread > 1e-3:
        return None
    if normal[2] < 0.0:
        normal = -normal
        distance = -distance
    if normal[2] <= 1e-9:
        return None
    d = float(np.mean(distance))
    return np.asarray([normal[0], normal[1], normal[2], d], dtype=np.float64)


def _project_loops(loops: Sequence[NDArray[np.float64]]) -> list[NDArray[np.float64]]:
    result: list[NDArray[np.float64]] = []
    for loop in loops or ():
        points = np.asarray(loop, dtype=np.float64)
        if points.ndim != 2 or points.shape[0] < 3 or points.shape[1] < 2:
            continue
        result.append(points[:, :2])
    return result


def _bounds_of(outline: NDArray[np.float64] | None,
               islands: Sequence[NDArray[np.float64]],
               loops: Sequence[NDArray[np.float64]]) -> tuple[float, float, float, float]:
    """优先用加工区域轮廓；退回到面的边界环。"""

    parts: list[NDArray[np.float64]] = []
    if outline is not None and np.asarray(outline).size:
        parts.append(np.asarray(outline, dtype=np.float64).reshape(-1, 2))
    for island in islands or ():
        array = np.asarray(island, dtype=np.float64)
        if array.size:
            parts.append(array.reshape(-1, 2))
    if not parts:
        parts.extend(loops)
    if not parts:
        raise PlanningError("面没有可用的边界环，无法建立加工区域")
    stacked = np.vstack(parts)
    return (float(stacked[:, 0].min()), float(stacked[:, 1].min()),
            float(stacked[:, 0].max()), float(stacked[:, 1].max()))


def _grid_axes(bounds: tuple[float, float, float, float], cell: float, margin_cells: int):
    x0 = float(bounds[0]) - margin_cells * cell
    y0 = float(bounds[1]) - margin_cells * cell
    x1 = float(bounds[2]) + margin_cells * cell
    y1 = float(bounds[3]) + margin_cells * cell
    nx = max(2, int(np.ceil((x1 - x0) / cell)))
    ny = max(2, int(np.ceil((y1 - y0) / cell)))
    return x0, y0, x0 + nx * cell, y0 + ny * cell, nx, ny


def _planar_field(plane: NDArray[np.float64],
                  bounds: tuple[float, float, float, float], cell: float,
                  margin_cells: int) -> FloorField:
    """平面（含斜面）用平面方程解析求值。"""

    nx, ny, nz = (float(value) for value in plane[:3])
    d = float(plane[3])
    if abs(nz) < 1e-9:  # pragma: no cover - 法向朝上时不会发生
        raise PlanningError("加工底面是竖直面，不能作为型腔底")
    x0, y0, x1, y1, gx, gy = _grid_axes(bounds, cell, margin_cells)
    xs = x0 + (np.arange(gx, dtype=np.float64) + 0.5) * cell
    ys = y0 + (np.arange(gy, dtype=np.float64) + 0.5) * cell
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")
    values = (d - nx * grid_x - ny * grid_y) / nz
    grad_x = float(-nx / nz)
    grad_y = float(-ny / nz)
    return FloorField(
        bounds=(x0, y0, x1, y1),
        cell_mm=cell,
        values=np.ascontiguousarray(values, dtype=np.float64),
        normal=np.asarray([nx, ny, nz], dtype=np.float64),
        planar=True,
        grad=(np.full(values.shape, grad_x, dtype=np.float64),
              np.full(values.shape, grad_y, dtype=np.float64)),
        source="解析平面方程",
        notes=[],
    )


def _planar_notes(record: Any, plane: NDArray[np.float64], flat: bool) -> list[str]:
    notes: list[str] = []
    normal = np.asarray(plane[:3], dtype=np.float64)
    if not flat:
        slope = float(np.degrees(np.arccos(min(1.0, max(1e-9, abs(float(normal[2])))))))
        notes.append(
            f"底面为斜面（坡度 {slope:.1f}°）：刀轴 Z 逐点跟随面高，"
            "平底刀在斜面与侧壁的交角处会留下一小块三角残料（几何必然，需用球头/圆鼻刀或更小步距）"
        )
    if getattr(record, "approximate", False):
        notes.append("该面的离散是近似的，底面高度按解析平面计算，不受离散精度影响")
    return notes


def _mesh_field(part: Any, record: Any, normal: NDArray[np.float64],
                region_bounds: tuple[float, float, float, float], cell: float,
                margin_cells: int, positions: NDArray[np.float64],
                triangles: NDArray[np.int64]) -> FloorField:
    """曲面：把底面的三角面片光栅化成高度场。"""

    x0, y0, x1, y1, gx, gy = _grid_axes(region_bounds, cell, margin_cells)
    grid_x = x0 + (np.arange(gx, dtype=np.float64) + 0.5) * cell
    grid_y = y0 + (np.arange(gy, dtype=np.float64) + 0.5) * cell
    # 累加而不是 "取最高"：曲面底与腔壁在**离散后会在边缘重叠**（弧面被切成台阶状，
    # 台阶的立面与弧面在同一 XY 上有两个 z）。取最高会把壁面/圆角的高度当成底面，
    # 把局部坡度算到 70° 以上、z 范围也偏大；取平均则把两者折中，误差不超过台阶高度，
    # 而且只发生在最靠边的一圈格子上。
    total = np.zeros((gx, gy), dtype=np.float64)
    weight = np.zeros((gx, gy), dtype=np.float64)
    notes: list[str] = []

    a = positions[triangles[:, 0]]
    b = positions[triangles[:, 1]]
    c = positions[triangles[:, 2]]

    # 逐三角形光栅化：只处理与三角形 XY 包围盒相交的格子。
    # 三角形数量 = 底面的面片数（曲面通常几十到几百个），每个只覆盖十几格，向量化后很快。
    i_lo = np.floor((np.minimum(np.minimum(a[:, 0], b[:, 0]), c[:, 0]) - x0) / cell - 0.5).astype(np.int64)
    i_hi = np.floor((np.maximum(np.maximum(a[:, 0], b[:, 0]), c[:, 0]) - x0) / cell - 0.5).astype(np.int64)
    j_lo = np.floor((np.minimum(np.minimum(a[:, 1], b[:, 1]), c[:, 1]) - y0) / cell - 0.5).astype(np.int64)
    j_hi = np.floor((np.maximum(np.maximum(a[:, 1], b[:, 1]), c[:, 1]) - y0) / cell - 0.5).astype(np.int64)
    for t in range(int(triangles.shape[0])):
        lo_i = max(int(i_lo[t]), 0)
        lo_j = max(int(j_lo[t]), 0)
        hi_i = min(int(i_hi[t]), gx - 1)
        hi_j = min(int(j_hi[t]), gy - 1)
        if hi_i < lo_i or hi_j < lo_j:
            continue
        px = grid_x[lo_i:hi_i + 1]
        py = grid_y[lo_j:hi_j + 1]
        bx, by = np.meshgrid(px, py, indexing="ij")
        z = _triangle_z(a[t], b[t], c[t], bx, by)
        if z is None:
            continue
        take = ~np.isnan(z)
        if not take.any():
            continue
        total[lo_i:hi_i + 1, lo_j:hi_j + 1][take] += z[take]
        weight[lo_i:hi_i + 1, lo_j:hi_j + 1][take] += 1.0

    covered = weight > 0.0
    if not covered.any():
        raise PlanningError(
            f"面 #{record.id} 的高度场是空的：底面面片太小或网格太粗，"
            f"请把加工精度（cell_mm）调细一些"
        )
    values = np.full((gx, gy), np.nan, dtype=np.float64)
    values[covered] = total[covered] / weight[covered]
    holes = int((~covered).sum())
    if holes:
        # 边角没被任何三角形覆盖：用最近的有效值填补，避免插值出 NaN。
        values = _fill_holes(values)
        notes.append(f"高度场有 {holes} 个格子落在面片之外，已按最近有效值补齐")

    grad_x, grad_y = _height_gradients(values, cell)
    slope = float(np.degrees(np.arctan(np.nanmax(np.hypot(grad_x, grad_y)))))
    face_slope = float(np.degrees(np.arccos(min(1.0, max(1e-9, abs(float(normal[2])))))))
    notes.append(
        f"底面为曲面（{record.surface_kind}，整体倾角 {face_slope:.1f}°，"
        f"最大局部坡度 {slope:.1f}°）：刀轴 Z 按高度场逐点跟随"
    )
    if getattr(record, "approximate", False):
        notes.append("该面的离散是近似的，底面高度直接取自三角网格，精度受离散精度限制")
    return FloorField(
        bounds=(x0, y0, x1, y1),
        cell_mm=cell,
        values=values,
        normal=np.asarray(normal, dtype=np.float64),
        planar=False,
        grad=(grad_x, grad_y),
        source="三角网格光栅化",
        notes=notes,
    )


def _triangle_z(a: NDArray[np.float64], b: NDArray[np.float64], c: NDArray[np.float64],
                bx: NDArray[np.float64], by: NDArray[np.float64]) -> NDArray[np.float64] | None:
    """网格点落在三角形 XY 投影内时的插值高度（外接盒内、排除退化三角形）。

    投影退化成一条线（竖直面片）时返回 None：竖直面不该出现在加工底面上。
    """

    denominator = ((b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]))
    if abs(float(denominator)) < 1e-12:
        return None
    wa = ((b[1] - c[1]) * (bx - c[0]) + (c[0] - b[0]) * (by - c[1])) / denominator
    wb = ((c[1] - a[1]) * (bx - c[0]) + (a[0] - c[0]) * (by - c[1])) / denominator
    wc = 1.0 - wa - wb
    inside = (wa >= -1e-9) & (wb >= -1e-9) & (wc >= -1e-9)
    z = np.full(bx.shape, np.nan, dtype=np.float64)
    if inside.any():
        z[inside] = (wa[inside] * a[2] + wb[inside] * b[2] + wc[inside] * c[2])
    return z


def _fill_holes(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """用最近邻有效值填补 NaN（面片没覆盖到的边角）。

    只做几轮扩散就够：缺口都在网格边缘，而边缘一定有相邻的有效格子。
    扩散不动的位置（整块都没有面片）退回该面的中位高度，宁可偏保守也不留 NaN。
    """

    filled = values.copy()
    fallback = float(np.nanmedian(filled)) if np.isfinite(filled).any() else 0.0
    for _ in range(4):
        missing = ~np.isfinite(filled)
        if not missing.any():
            break
        padded = np.pad(filled, 1, mode="edge")
        neighbours = np.stack([
            padded[:-2, 1:-1], padded[2:, 1:-1], padded[1:-1, :-2], padded[1:-1, 2:],
        ])
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            # 四邻全空时 nanmean 会报 "Mean of empty slice"，这里用 fallback 兜底，
            # 所以那个告警没有信息量，直接屏蔽。
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(neighbours, axis=0)
        filled[missing] = np.where(np.isfinite(mean), mean, fallback)[missing]
    return np.nan_to_num(filled, nan=fallback)


def _height_gradients(values: NDArray[np.float64], cell: float
                      ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """中心差分梯度（边界用单侧差分）。"""

    grad_x = np.empty_like(values)
    grad_y = np.empty_like(values)
    grad_x[1:-1, :] = (values[2:, :] - values[:-2, :]) / (2.0 * cell)
    grad_x[0, :] = (values[1, :] - values[0, :]) / cell
    grad_x[-1, :] = (values[-1, :] - values[-2, :]) / cell
    grad_y[:, 1:-1] = (values[:, 2:] - values[:, :-2]) / (2.0 * cell)
    grad_y[:, 0] = (values[:, 1] - values[:, 0]) / cell
    grad_y[:, -1] = (values[:, -1] - values[:, -2]) / cell
    return grad_x, grad_y


def checked_floor_value(value: Any, label: str) -> float:
    """把外部传进来的高度值校验成有限浮点数。"""

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PlanningError(f"{label} 必须是数字（收到 {value!r}）") from error
    if not isfinite(number):
        raise PlanningError(f"{label} 必须是有限值（收到 {value!r}）")
    return number


__all__ = [
    "DEFAULT_FLOOR_CELL_MM",
    "FLAT_SLOPE_TOLERANCE",
    "FloorField",
    "MAX_FLOOR_CELL_MM",
    "MIN_FLOOR_CELL_MM",
    "checked_floor_value",
    "constant_floor",
    "floor_field_from_face",
]
