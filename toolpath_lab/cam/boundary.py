"""加工区域：把选中的平面面变成"刀具中心可以走的区域"。

选中的面（平面铣的顶面、型腔铣的腔底面）自带若干条边界环——外轮廓与岛屿。
把它们投到 XY 平面，再按**刀具半径 + 侧面余量**向内偏置，就得到刀心可行区域。

为什么用栅格而不是纯多边形偏置：

- 多边形的等距偏置要处理自交、尖角、岛屿合并，是最容易出 bug 的一类几何算法；
- 而栅格上的"距离场"天然同时处理外轮廓与岛屿，多深的型腔、多复杂的岛屿都能算；
- 刀路本身就是离散的（数控机床只认直线段），栅格精度取 0.2~0.5 mm 完全够用，
  且与后续的毛坯切除仿真共用同一套坐标，不会出现"刀路与仿真对不上"。

因此这里只做两件"重活"：

1. :func:`region_from_face` —— 读面的边界环，分拣外轮廓与岛屿；
2. :class:`MachiningRegion` —— 建栅格、算距离场、按偏移量取等距区域，
   并给出扫描线区间（平面铣/型腔铣都用它出刀）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.cam.floor_field import FloorField, constant_floor, floor_field_from_face
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.part import PartModel

#: 栅格目标间距（mm）。越小越精确，也越慢；0.4 mm 在常规零件上是很好的折中。
DEFAULT_CELL_MM = 0.4
#: 栅格总单元数上限，防止小间距 + 大零件把内存吃光。
MAX_CELLS = 4_000_000
#: 栅格间距的允许范围。
MIN_CELL_MM = 0.05
MAX_CELL_MM = 5.0
#: 自适应分辨率：沿最长边大致取多少格。见 :func:`adaptive_cell_mm`。
#: 360 ≈ 0.28 mm/cell @ 100 mm 毛坯 / 0.56 mm/cell @ 200 mm 毛坯，
#: 在常规零件上保持细腻，同时不会让大零件炸掉内存。
TARGET_CELLS_PER_AXIS = 360


def adaptive_cell_mm(extent_mm: float, *, target: int = TARGET_CELLS_PER_AXIS,
                     low: float = MIN_CELL_MM, high: float = MAX_CELL_MM) -> float:
    """按工件尺寸挑一个合适的栅格精度。

    固定 0.4 / 0.5 mm 对几十毫米的零件很合适，但 200 mm 的零件会变成 500×500 = 25 万格：
    规划一次要好几秒、仿真一次上百秒、接口响应几十 MB。按"最长边约 220 格"反推，
    小零件照样细、大零件自动放粗，用户不用关心这个参数。
    """

    if not extent_mm or extent_mm <= 0:
        return DEFAULT_CELL_MM
    return float(np.clip(float(extent_mm) / max(int(target), 1), low, high))

#: 倒角距离变换的权重（3-4 近似欧氏距离的经典取值）。
_CHAMFER_ORTHO = 3.0
_CHAMFER_DIAG = 4.0


def _ensure_ccw(points: NDArray[np.float64]) -> NDArray[np.float64]:
    x = points[:, 0]
    y = points[:, 1]
    area = float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
    return points if area >= 0.0 else points[::-1]


def _dedupe(points: NDArray[np.float64], tolerance: float = 1e-6) -> NDArray[np.float64]:
    if points.shape[0] < 2:
        return points
    keep = [points[0]]
    for point in points[1:]:
        if float(np.linalg.norm(point - keep[-1])) > tolerance:
            keep.append(point)
    while len(keep) > 1 and float(np.linalg.norm(keep[0] - keep[-1])) <= tolerance:
        keep.pop()
    return np.asarray(keep, dtype=np.float64)


def _polygon_area(points: NDArray[np.float64]) -> float:
    x = points[:, 0]
    y = points[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def point_in_polygon(point: Sequence[float], polygon: NDArray[np.float64]) -> bool:
    """射线法判点是否在多边形内（用于岛屿与轮廓的判定）。"""

    x, y = float(point[0]), float(point[1])
    inside = False
    count = polygon.shape[0]
    for index in range(count):
        x0, y0 = float(polygon[index, 0]), float(polygon[index, 1])
        x1, y1 = float(polygon[(index + 1) % count, 0]), float(polygon[(index + 1) % count, 1])
        if (y0 > y) != (y1 > y):
            denominator = y1 - y0
            if abs(denominator) > 1e-15 and x0 + (y - y0) * (x1 - x0) / denominator > x:
                inside = not inside
    return inside


@dataclass(slots=True)
class MachiningRegion:
    """刀心可行区域：外轮廓、岛屿、栅格掩码与距离场。"""

    outline: NDArray[np.float64]        # (N, 2) 面的外轮廓（世界 XY）
    islands: tuple[NDArray[np.float64], ...] = ()
    top_z: float = 0.0                  # 加工起始高度（毛坯顶面或零件顶面）
    floor_z: float = 0.0                # 加工目标高度（选中面的 Z）
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    cell_mm: float = DEFAULT_CELL_MM
    inside: NDArray[np.bool_] = field(default_factory=lambda: np.zeros((0, 0), dtype=bool))
    distance: NDArray[np.float64] = field(default_factory=lambda: np.zeros((0, 0)))
    #: 加工底面的高度场。水平面时是一张常数场，斜面/曲面时随位置变化。
    #: 刀路的 Z 与"刀具会不会扎进底面"的判断都从这里取，不再假设底面是平的。
    floor: FloorField = field(default_factory=lambda: constant_floor(0.0))
    #: 底面高度的区域栅格缓存（形状与 ``inside`` 一致，由 :meth:`prepare_floor_grid` 填）。
    #: 斜面/曲面的高度场铺得比区域大一圈，区域之外的值没有意义，
    #: 所以"环切到哪一层、哪一段还能切"一律以这张栅格为准。
    floor_grid_cache: NDArray[np.float64] | None = None
    #: 防过切抬升的区域栅格缓存（与 ``floor_grid_cache`` 配套）。
    lift_grid_cache: NDArray[np.float64] | None = None
    #: 刀轴高度场的缓存（底面高度 + 防过切抬升），见 :meth:`axis_grid`。
    axis_grid_cache: NDArray[np.float64] | None = None
    notes: list[str] = field(default_factory=list)

    # -- 基本量 ------------------------------------------------------------
    @property
    def depth_mm(self) -> float:
        return max(0.0, float(self.top_z - self.floor_z))

    @property
    def is_flat_floor(self) -> bool:
        """底面是不是水平的（水平底面走原来的常数逻辑，逐点取值等价）。"""

        return bool(self.floor.is_flat)

    @property
    def floor_slope_deg(self) -> float:
        return 0.0 if self.is_flat_floor else float(self.floor.max_slope_deg())

    def floor_z_at(self, x: NDArray[np.float64] | float,
                   y: NDArray[np.float64] | float) -> NDArray[np.float64]:
        """底面在 (x, y) 处的高度（斜面/曲面逐点不同）。"""

        return self.floor.height_at(x, y)

    def floor_grid(self) -> NDArray[np.float64]:
        """整张区域栅格上的底面高度，形状与 ``inside`` 一致。"""

        if self.floor_grid_cache is not None:
            return self.floor_grid_cache
        xs = self.bounds[0] + (np.arange(self.shape[0], dtype=np.float64) + 0.5) * self.cell_mm
        ys = self.bounds[1] + (np.arange(self.shape[1], dtype=np.float64) + 0.5) * self.cell_mm
        grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")
        values = np.asarray(self.floor.height_at(grid_x, grid_y), dtype=np.float64)
        # 高度场铺得比区域大一圈，区域之外的值没有意义：一律夹到区域内的最高/最低，
        # 免得倾斜面在区域外继续往下走，把"最深点"算到区域外面去。
        if self.inside.size == self.inside.shape and self.inside.any():
            inside_values = values[self.inside]
            values = np.clip(values, float(inside_values.min()), float(inside_values.max()))
        self.floor_grid_cache = values
        return values

    def lift_grid(self, tool: Any) -> NDArray[np.float64]:
        """区域栅格上的"防过切抬升"（与 :meth:`floor_grid` 配套，按刀具缓存一次）。

        抬升 = 刀心放在该格时，刀体为了不扎进底面必须比"刀尖贴住格心底面"再高多少。
        两条互补的界取较大的那条：

        * **解析界**（``required_lift``）：把底面当局部平面看——平底刀正好是
          ``R·tanθ``、球头刀约一半。对平面底面它是精确值；
        * **足迹采样界**：直接在高度场上取足迹圆内的最高点再减去刀体轮廓高度。
          曲面底面上解析界会偏小（曲率项 ≈ ``R²·|∇k|/2``，实测 0.05 mm 量级），
          这一条把差额兜住。

        两者都只与坡度有关，所以按"坡度值 → 抬升"缓存一次标量表就够了——
        刀路动辄几千个点，逐点扫描足迹没必要。
        """

        if self.lift_grid_cache is not None:
            return self.lift_grid_cache
        from toolpath_lab.cam.tool_engagement import required_lift

        if self.is_flat_floor:
            grid = np.zeros(self.shape, dtype=np.float64)
            self.lift_grid_cache = grid
            return grid
        xs = self.bounds[0] + (np.arange(self.shape[0], dtype=np.float64) + 0.5) * self.cell_mm
        ys = self.bounds[1] + (np.arange(self.shape[1], dtype=np.float64) + 0.5) * self.cell_mm
        grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")
        normals = np.asarray(self.floor.normal_at(grid_x, grid_y), dtype=np.float64).reshape(-1, 3)
        with np.errstate(invalid="ignore", divide="ignore"):
            slope = np.hypot(normals[:, 0], normals[:, 1]) / np.maximum(
                np.abs(normals[:, 2]), 1e-9)
        unique, inverse = np.unique(np.round(slope, 6), return_inverse=True)
        table = np.asarray([required_lift(tool, (float(value), 0.0)) for value in unique],
                           dtype=np.float64)
        analytic = (table[inverse] if table.size else np.zeros_like(slope)).reshape(self.shape)
        grid = np.maximum(analytic, self._footprint_lift_grid(tool))
        self.lift_grid_cache = grid
        return grid

    def _footprint_lift_grid(self, tool: Any) -> NDArray[np.float64]:
        """足迹采样抬升：刀放在格心时，足迹圆内最高底面相对格心底面的高差。

        与"落刀测试"（drop cutter）同一个判据，只是限制在区域栅格上、并按预算限制
        采样点数：格点数 × 采样点数超过预算时自动加粗采样（此时栅格本身也很粗，
        解析界仍在兜底）。
        """

        from toolpath_lab.cam.tool_engagement import profile_height

        floor = self.floor_grid()
        rows, cols = floor.shape
        radius = float(getattr(tool, "radius_mm", 0.0) or 0.0)
        cell = float(self.cell_mm)
        lift = np.zeros_like(floor)
        if radius <= 1e-9 or cell <= 1e-9 or rows < 2 or cols < 2:
            return lift
        reach = max(1, int(np.ceil(radius / cell)))
        # 采样预算：一次规划里这一步只做一次，但大件上"格点数 × 采样数"仍要封顶
        budget = 4_000_000
        stride = max(1, int(np.ceil(reach / max(1.0, np.sqrt(budget / max(rows * cols, 1))))))
        offsets: list[tuple[int, int, float]] = []
        for di in range(-reach, reach + 1, stride):
            for dj in range(-reach, reach + 1, stride):
                offset = float(np.hypot(di, dj)) * cell
                if offset > radius + 1e-9 or offset <= 1e-12:
                    continue
                height = profile_height(tool, offset)
                if not np.isfinite(height):
                    continue
                offsets.append((di, dj, float(height)))
        if not offsets:
            return lift
        padded = np.full((rows + 2 * reach, cols + 2 * reach), -np.inf, dtype=np.float64)
        padded[reach:reach + rows, reach:reach + cols] = floor
        for di, dj, height in offsets:
            shifted = padded[reach + di:reach + di + rows, reach + dj:reach + dj + cols]
            np.maximum(lift, shifted - height - floor, out=lift)
        return lift

    def axis_grid(self, tool: Any) -> NDArray[np.float64]:
        """区域栅格上的**刀轴高度场**：刀心放在格心时，刀尖不扎进底面的最低 Z。

        ``axis = 底面高度 + 防过切抬升``。它是这一层"能不能切"和"该切到多高"的唯一
        依据：水平底面时等于底面的常数高度（与引入高度场之前完全一致）。
        """

        if self.axis_grid_cache is not None:
            return self.axis_grid_cache
        grid = self.floor_grid() + self.lift_grid(tool)
        self.axis_grid_cache = grid
        return grid

    def axis_z_at(self, x: NDArray[np.float64] | float, y: NDArray[np.float64] | float,
                  tool: Any) -> NDArray[np.float64]:
        """任意 (x, y) 处不过切的刀轴高度（栅格形式，取周边四格的**最大值**）。

        刀位点通常落在格点上，周边四格的足迹并集覆盖该点的足迹圆，取最大值因此偏保守
        （宁可多留一点台阶，也不扎进底面）。
        """

        px = np.asarray(x, dtype=np.float64)
        py = np.asarray(y, dtype=np.float64)
        if self.is_flat_floor or self.inside.size == 0:
            return np.asarray(self.floor_z_at(px, py), dtype=np.float64)
        grid = self.axis_grid(tool)
        rows, cols = grid.shape
        fi = np.clip((px - self.bounds[0]) / self.cell_mm - 0.5, 0.0, float(rows - 1))
        fj = np.clip((py - self.bounds[1]) / self.cell_mm - 0.5, 0.0, float(cols - 1))
        i0 = np.floor(fi).astype(np.int64)
        j0 = np.floor(fj).astype(np.int64)
        i1 = np.minimum(i0 + 1, rows - 1)
        j1 = np.minimum(j0 + 1, cols - 1)
        return np.maximum(np.maximum(grid[i0, j0], grid[i1, j0]),
                          np.maximum(grid[i0, j1], grid[i1, j1]))

    def deepest_axis_z(self, tool: Any, mask: NDArray[np.bool_] | None = None) -> float:
        """``mask`` 范围内刀轴能落到的**最低**高度（斜/曲面底面的真实加工底）。

        不能直接用 :attr:`floor_z`：刀尖要贴住底面还得再抬一个 ``required_lift``，
        于是 ``floor_z`` 对倾斜底面永远够不着——按它分层，最底下几层会被判成
        "区域为空"整层跳过，底面留下整整一层甚至几毫米的残料。
        """

        return self._axis_extreme(tool, mask, np.min)

    def highest_axis_z(self, tool: Any, mask: NDArray[np.bool_] | None = None) -> float:
        """``mask`` 范围内刀轴高度的**最高**值（决定第一层够不够高）。"""

        return self._axis_extreme(tool, mask, np.max)

    def _axis_extreme(self, tool: Any, mask: NDArray[np.bool_] | None,
                      reduce_fn: Any) -> float:
        if self.is_flat_floor:
            return float(self.floor_z)
        grid = self.axis_grid(tool)
        active = self.inside if mask is None else (mask & self.inside)
        if active.shape != grid.shape or not bool(active.any()):
            return float(self.floor_z)
        values = grid[active]
        values = values[np.isfinite(values)]
        if values.size == 0:
            return float(self.floor_z)
        return float(reduce_fn(values))

    def level_mask(self, level_z: float, tool: Any,
                   mask: NDArray[np.bool_] | None = None) -> NDArray[np.bool_]:
        """这一层**还能切**的区域。

        判据是"刀轴放在这里、刀尖贴住该点底面"时，刀体不会扎进底面：

            axis(x, y) = floor(x, y) + lift(∇floor) ≤ level_z

        水平底面时抬升为 0、底面高度是常数，于是这一条对本层没有约束（等价于老行为）；
        斜面/曲面时层高降到某处底面之下，掩码会自然把那一块裁掉，
        否则刀会在层高上平着切过去、把高处的底面切掉。
        """

        if self.is_flat_floor:
            return self.inside if mask is None else mask
        allowed = self.axis_grid(tool) <= float(level_z) + 1e-9
        if mask is not None:
            allowed = allowed & mask
        # 与"刀心可行区域"求交：层高落到某处底面之下时被裁掉，而刀具半径/余量
        # 本来就要留出的那圈边距不能因此丢掉。
        return self.offset_mask(0.0) & allowed

    def deepest_floor_z(self) -> float:
        """**加工区域内**的底面最低点（斜底/曲底的最低处）。

        不能直接用高度场的全局最小值：高度场为了插值完整，范围比区域大一圈，
        倾斜面在区域之外会继续往下走，取全局最小值会把加工深度算大。
        也不能用 :meth:`floor_grid` —— 它对区域外的值做了夹取，用在"区域内最低点"
        上会把结果压在区域最低值上、反而算不准。这里直接对区域内单元求值。
        """

        if self.inside.size and self.inside.any():
            rows, cols = np.nonzero(self.inside)
            xs = self.bounds[0] + (rows + 0.5) * self.cell_mm
            ys = self.bounds[1] + (cols + 0.5) * self.cell_mm
            values = np.asarray(self.floor_z_at(xs, ys), dtype=np.float64)
            if values.size and np.isfinite(values).any():
                return float(np.nanmin(values))
        return float(self.floor_z)

    @property
    def area_mm2(self) -> float:
        return float(self.inside.sum()) * self.cell_mm * self.cell_mm

    @property
    def shape(self) -> tuple[int, int]:
        return self.inside.shape  # type: ignore[return-value]

    def grid_point(self, i: int, j: int) -> tuple[float, float]:
        x0, y0 = self.bounds[0], self.bounds[1]
        return (x0 + (i + 0.5) * self.cell_mm, y0 + (j + 0.5) * self.cell_mm)

    def describe(self) -> dict[str, Any]:
        return {
            "outline": [[round(float(value), 4) for value in point] for point in self.outline],
            "islands": [
                [[round(float(value), 4) for value in point] for point in island]
                for island in self.islands
            ],
            "top_z": round(self.top_z, 4),
            "floor_z": round(self.floor_z, 4),
            "depth_mm": round(self.depth_mm, 4),
            "area_mm2": round(self.area_mm2, 3),
            "cell_mm": round(self.cell_mm, 4),
            "grid": list(self.shape),
            "floor": self.floor.describe(),
            "notes": list(self.notes),
        }

    # -- 等距区域 ----------------------------------------------------------
    def offset_mask(self, offset_mm: float) -> NDArray[np.bool_]:
        """距边界至少 offset_mm 的"内部"单元（距离场单位已经是毫米）。"""

        if self.distance.size == 0:
            return self.inside.copy()
        if offset_mm <= 0.0:
            return self.inside.copy()
        return self.inside & (self.distance >= offset_mm)

    def offset_area_mm2(self, offset_mm: float) -> float:
        return float(self.offset_mask(offset_mm).sum()) * self.cell_mm * self.cell_mm

    # -- 扫描线 ------------------------------------------------------------
    def scanline_intervals(self, level_mm: float, angle_deg: float,
                           mask: NDArray[np.bool_] | None = None
                           ) -> list[tuple[float, float, float]]:
        """沿给定方向走刀时，位于掩码内部的区间。

        返回 ``(u_start, u_end, v_level)``，都是**走刀坐标系**下的毫米值：
        u 沿走刀方向，v 垂直于它。
        """

        active = self.inside if mask is None else mask
        if not active.any():
            return []
        x0, y0, x1, y1 = self.bounds
        angle = np.radians(float(angle_deg))
        u_axis = np.array([np.cos(angle), np.sin(angle)], dtype=np.float64)
        v_axis = np.array([-np.sin(angle), np.cos(angle)], dtype=np.float64)

        rows, cols = np.nonzero(active)
        px = x0 + (rows + 0.5) * self.cell_mm
        py = y0 + (cols + 0.5) * self.cell_mm
        u = px * u_axis[0] + py * u_axis[1]
        v = px * v_axis[0] + py * v_axis[1]
        band = self.cell_mm * 0.5
        selected = np.abs(v - float(level_mm)) <= band
        if not selected.any():
            return []
        # 区间 = **本层带宽内所有单元在 u 上投影的并集**。
        #
        # 先前按 (row, col) 邻接判据（BUG-008 的修法）在"带宽覆盖两列单元"时会把每一列
        # 交接处都当成断开：一个 60×40 的型腔一层会返回几十段几毫米的碎刀轨（既慢又碎）。
        # 而最早按 u 间距 1.6*cell 合并又太松，会把 1 格宽的窄岛直接桥接过去（过切）。
        # 投影并集同时满足两条：相邻单元的半格投影正好相接（不拆），
        # 隔 1 格的窄缝留下 1 格的空档（必拆）。
        half = band * (abs(float(u_axis[0])) + abs(float(u_axis[1])))
        starts = u[selected] - half
        ends = u[selected] + half
        order = np.argsort(starts, kind="stable")
        starts = starts[order]
        ends = ends[order]
        intervals: list[tuple[float, float, float]] = []
        start_u = float(starts[0])
        end_u = float(ends[0])
        for k in range(1, starts.size):
            if float(starts[k]) <= end_u + 1e-9:
                end_u = max(end_u, float(ends[k]))
                continue
            intervals.append((start_u, end_u, float(level_mm)))
            start_u = float(starts[k])
            end_u = float(ends[k])
        intervals.append((start_u, end_u, float(level_mm)))
        return intervals

    def scanline_levels(self, angle_deg: float, stepover_mm: float,
                        mask: NDArray[np.bool_] | None = None) -> NDArray[np.float64]:
        """按步距在垂直走刀方向上布刀线。"""

        active = self.inside if mask is None else mask
        rows, cols = np.nonzero(active)
        if rows.size == 0:
            return np.zeros(0, dtype=np.float64)
        x0, y0 = self.bounds[0], self.bounds[1]
        px = x0 + (rows + 0.5) * self.cell_mm
        py = y0 + (cols + 0.5) * self.cell_mm
        angle = np.radians(float(angle_deg))
        v = -np.sin(angle) * px + np.cos(angle) * py
        low, high = float(v.min()), float(v.max())
        # 间距下限取半格即可：整格下限会在粗栅格 + 小刀具时把刀线间距
        # 抬到超过刀直径，两条刀线之间留下切不到的残料。
        step = max(float(stepover_mm), 0.5 * self.cell_mm)
        count = int(np.floor((high - low) / step + 1e-9)) + 1
        levels = low + np.arange(count, dtype=np.float64) * step
        if high - float(levels[-1]) > 0.25 * step:
            levels = np.append(levels, high)
        return levels


# ------------------------------------------------------------------ 构造
def region_from_face(part: PartModel, face_id: int, *, cell_mm: float = DEFAULT_CELL_MM,
                     ceiling_z: float | None = None,
                     require_horizontal: bool = False) -> MachiningRegion:
    """由选中的面构造加工区域。

    ``ceiling_z`` 是这一层加工的**起始高度**（毛坯顶面或上一层的底）。两者之差就是
    这一道工序要切除的深度：

    - 平面铣顶面时，ceiling 取毛坯顶面，于是深度 = 毛坯余量；
    - 型腔铣腔底时，ceiling 取毛坯顶面，深度 = 从毛坯顶到腔底的整段深度。

    不传 ``ceiling_z`` 时退回零件顶面，此时平面铣的深度为 0（只做一刀光面）。

    **底面可以是斜面或曲面**：加工区域始终是 XY 平面上的栅格（外轮廓 + 岛屿 + 距离场），
    但底面高度不再是常数，而是随位置变化的
    :class:`~toolpath_lab.cam.floor_field.FloorField`。这样"区域运算"仍是一套成熟的
    2.5D 逻辑，而刀轴 Z 可以逐点跟随实际面形。
    ``require_horizontal=True`` 时恢复老行为（只接受水平面），平面铣用它。

    底面类型与范围都交给 :func:`~toolpath_lab.cam.floor_field.floor_field_from_face` 判定：
    曲面底在 STEP 里常被离散成几十个小平面片，只看选中那一片的平面方程会把整张底面
    当成一个小斜面。
    """

    record = part.face(int(face_id))
    if record is None:
        raise PlanningError(f"找不到序号为 {face_id} 的面")

    normal = np.asarray(record.normal, dtype=np.float64)
    if normal[2] <= 0.0:
        raise PlanningError(
            f"面 #{face_id} 的法向不朝上（{tuple(round(float(v), 3) for v in normal)}），"
            "请在三维视图中选择朝上的加工面"
        )
    if require_horizontal:
        if not record.is_planar or record.plane is None:
            raise PlanningError(f"面 #{face_id} 不是平面（{record.surface_kind}），暂不支持加工")
        if normal[2] < 1.0 - 1e-3:
            raise PlanningError(
                f"面 #{face_id} 不是近水平面（nz={normal[2]:.4f}），"
                "平面铣只支持朝上的水平面；倾斜面请改用型腔铣"
            )

    loops_2d: list[NDArray[np.float64]] = []
    for loop in record.loops:
        points = np.asarray(loop, dtype=np.float64)
        if points.shape[0] < 3:
            continue
        planar = _dedupe(points[:, :2])
        if planar.shape[0] < 3:
            continue
        loops_2d.append(planar)
    if not loops_2d:
        raise PlanningError(f"面 #{face_id} 没有可用的边界环")

    areas = [abs(_polygon_area(loop)) for loop in loops_2d]
    outer_index = int(np.argmax(areas))
    outline = _ensure_ccw(loops_2d[outer_index])
    islands = tuple(_ensure_ccw(loop)[::-1] for index, loop in enumerate(loops_2d)
                    if index != outer_index and areas[index] > 1e-6)

    floor, outline, islands, inside_mask = resolve_floor_surface(
        part, face_id, outline, islands, cell_mm=cell_mm)
    start_z = float(part.bounds.z_max if ceiling_z is None else ceiling_z)
    # floor_z 只是"最深处的参考值"：斜面/曲面时底面的最低点就是加工目标高度，
    # 所以先建区域、再用区域内实际取到的底面高度修正它（build_region 里会再做一次）。
    floor_z = float(floor.z_min)
    if start_z < floor_z:
        start_z = floor_z

    region = build_region(outline, islands, top_z=start_z, floor_z=floor_z, cell_mm=cell_mm,
                          floor=floor, inside_mask=inside_mask)
    region.floor_z = region.deepest_floor_z()
    return region


def planar_features(part: PartModel) -> list[dict[str, Any]]:
    """可用于加工的面清单（供前端做特征树与快速选择）。

    除了平面面，**朝上的曲面**（圆柱/圆锥/B 样条……）也列进来并标记为可加工：
    型腔铣现在支持斜面与曲面底，前端得能把它们选出来。
    每项都带 ``machinable_kinds``，界面据此禁用不支持的加工类型。
    """

    result: list[dict[str, Any]] = []
    for feature in part.features:
        normal = np.asarray(feature.get("normal") or (0.0, 0.0, 1.0), dtype=np.float64)
        planar = bool(feature.get("planar"))
        if not planar:
            item = dict(feature)
            # 朝上的曲面可以作为型腔底面；朝下的（零件底部）排除
            item["machinable"] = bool(normal[2] > 0.05)
            item["role"] = "曲面（可作型腔底）" if item["machinable"] else "曲面（朝下）"
            item["machinable_kinds"] = ["pocket_mill", "contour_mill"] \
                if item["machinable"] else []
            item["floor_capable"] = bool(item["machinable"])
            result.append(item)
            continue
        item = dict(feature)
        upward = bool(normal[2] > 0.0)
        level = bool(normal[2] > 0.999)
        item["machinable"] = upward
        item["role"] = (
            "顶面/台阶面" if level
            else ("侧面" if abs(normal[2]) < 0.001 else "斜面")
        )
        kinds: list[str] = []
        if upward:
            # 平面铣要求水平面；型腔铣与轮廓铣斜面/曲面都行
            kinds.append("pocket_mill")
            kinds.append("contour_mill")
            if level:
                kinds.append("face_mill")
        item["machinable_kinds"] = kinds
        item["floor_capable"] = bool(upward)
        result.append(item)
    return result


def build_region(outline: NDArray[np.float64], islands: Sequence[NDArray[np.float64]] = (),
                 *, top_z: float, floor_z: float, cell_mm: float = DEFAULT_CELL_MM,
                 floor: FloorField | None = None,
                 inside_mask: NDArray[np.bool_] | None = None) -> MachiningRegion:
    """由外轮廓与岛屿建栅格、算距离场。

    ``floor`` 是加工底面的高度场：不传时按常数 ``floor_z`` 建一张水平底面，
    行为与引入高度场之前完全一致。

    ``inside_mask`` 是"已经在同一张栅格上算好的实心掩码"：曲面型腔的底轮廓由
    底面三角面片光栅化得到（退化面片拼出来的轮廓用多边形拼不准），走这条路进来时
    就直接用它当内部区域，并把它的边界当作距离场的轮廓。
    """

    x_min = float(outline[:, 0].min())
    x_max = float(outline[:, 0].max())
    y_min = float(outline[:, 1].min())
    y_max = float(outline[:, 1].max())
    margin = max(cell_mm * 2.0, 0.5)
    x_min -= margin
    y_min -= margin
    x_max += margin
    y_max += margin

    spacing = float(np.clip(cell_mm, MIN_CELL_MM, MAX_CELL_MM))
    cols = int(np.ceil((x_max - x_min) / spacing)) + 1
    rows = int(np.ceil((y_max - y_min) / spacing)) + 1
    while rows * cols > MAX_CELLS:
        spacing *= 1.25
        cols = int(np.ceil((x_max - x_min) / spacing)) + 1
        rows = int(np.ceil((y_max - y_min) / spacing)) + 1

    notes: list[str] = []
    if spacing > cell_mm * 1.05:
        notes.append(f"栅格间距按内存上限放大到 {spacing:.3f} mm（精度略降）")

    # 单元中心坐标
    xs = x_min + (np.arange(cols) + 0.5) * spacing
    ys = y_min + (np.arange(rows) + 0.5) * spacing
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")

    inside = _rasterize(outline, grid_x, grid_y)
    for island in islands:
        if island.shape[0] >= 3:
            inside &= ~_rasterize(island, grid_x, grid_y)
    if inside_mask is not None and inside_mask.shape == inside.shape:
        inside = inside_mask
        outline = _outline_from_mask(mask=inside, bounds=(x_min, y_min, x_max, y_max),
                                     spacing=spacing, fallback=outline)
        islands = ()

    # 到轮廓的距离：外轮廓与所有岛屿取最小，直接对原始多边形求，没有阶梯误差
    distance = _distance_to_contour(inside, np.asarray(outline, dtype=np.float64),
                                    x_min, y_min, spacing)
    for island in islands:
        if island.shape[0] >= 3:
            distance = np.minimum(
                distance,
                _distance_to_contour(inside, np.asarray(island, dtype=np.float64),
                                     x_min, y_min, spacing),
            )
    distance = np.where(inside, distance, 0.0)

    # 面积核对：栅格化后的面积应当与多边形面积接近
    polygon_area = abs(_polygon_area(outline)) - sum(abs(_polygon_area(item)) for item in islands)
    raster_area = float(inside.sum()) * spacing * spacing
    if polygon_area > 0 and abs(raster_area - polygon_area) / polygon_area > 0.08:
        notes.append(
            f"栅格面积 {raster_area:.1f} mm² 与轮廓面积 {polygon_area:.1f} mm² 相差"
            f" {abs(raster_area - polygon_area) / polygon_area * 100:.1f}%，建议减小栅格间距"
        )

    return MachiningRegion(
        outline=np.asarray(outline, dtype=np.float64),
        islands=tuple(np.asarray(item, dtype=np.float64) for item in islands),
        top_z=float(top_z),
        floor_z=float(floor_z),
        bounds=(x_min, y_min, x_max, y_max),
        cell_mm=spacing,
        inside=inside,
        distance=distance,
        floor=constant_floor(floor_z) if floor is None else floor,
        notes=notes,
    )


def _mask_from_triangles(positions: NDArray[np.float64], triangles: NDArray[np.int64],
                         x_min: float, y_min: float, spacing: float,
                         shape: tuple[int, int]) -> NDArray[np.bool_]:
    """把一组三角面片在 XY 上光栅化成掩码（底面轮廓的"证据"）。"""

    rows, cols = shape
    mask = np.zeros((rows, cols), dtype=bool)
    if triangles.size == 0:
        return mask
    xs = x_min + (np.arange(rows) + 0.5) * spacing
    ys = y_min + (np.arange(cols) + 0.5) * spacing
    corners = positions[triangles]
    for index in range(int(triangles.shape[0])):
        triangle = corners[index]
        x_lo = int(max(0, np.floor((triangle[:, 0].min() - x_min) / spacing - 0.5)))
        x_hi = int(min(rows - 1, np.ceil((triangle[:, 0].max() - x_min) / spacing - 0.5)))
        y_lo = int(max(0, np.floor((triangle[:, 1].min() - y_min) / spacing - 0.5)))
        y_hi = int(min(cols - 1, np.ceil((triangle[:, 1].max() - y_min) / spacing - 0.5)))
        if x_hi < x_lo or y_hi < y_lo:
            continue
        block_x, block_y = np.meshgrid(xs[x_lo:x_hi + 1], ys[y_lo:y_hi + 1], indexing="ij")
        a, b, c = triangle[0], triangle[1], triangle[2]
        denominator = ((b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]))
        if abs(float(denominator)) < 1e-12:
            continue
        wa = ((b[1] - c[1]) * (block_x - c[0]) + (c[0] - b[0]) * (block_y - c[1])) / denominator
        wb = ((c[1] - a[1]) * (block_x - c[0]) + (a[0] - c[0]) * (block_y - c[1])) / denominator
        wc = 1.0 - wa - wb
        mask[x_lo:x_hi + 1, y_lo:y_hi + 1] |= ((wa >= -1e-9) & (wb >= -1e-9) & (wc >= -1e-9))
    return mask


def _outline_from_mask(mask: NDArray[np.bool_], bounds: tuple[float, float, float, float],
                       spacing: float, fallback: NDArray[np.float64]) -> NDArray[np.float64]:
    """从掩码里取**面积最大**的那条边界环当外轮廓（孔洞不进外轮廓）。"""

    x0, y0 = bounds[0], bounds[1]
    best: NDArray[np.float64] | None = None
    best_area = 0.0
    for pixels in _trace_mask_boundaries(mask):
        if len(pixels) < 4:
            continue
        points = np.asarray([(x0 + i * spacing, y0 + j * spacing) for i, j in pixels],
                            dtype=np.float64)
        polygon = _simplify(points, spacing * 0.35)
        if polygon.shape[0] < 3:
            continue
        area = abs(_polygon_area(polygon))
        if area > best_area:
            best_area = area
            best = polygon
    if best is None:  # pragma: no cover - 掩码为空时退回调用方的轮廓
        return np.asarray(fallback, dtype=np.float64)
    return _ensure_ccw(best)


def resolve_floor_surface(part: PartModel, face_id: int,
                          outline: NDArray[np.float64],
                          islands: Sequence[NDArray[np.float64]] = (),
                          *, cell_mm: float = DEFAULT_CELL_MM):
    """底面模型：高度场 + 由底面三角面片推出来的加工轮廓。

    返回 ``(floor, outline, islands, inside_mask)``。``inside_mask`` 只有"底面被切成
    很多小平面片、选中那一片的边界环不足以代表整块底面"时才不是 None ——
    这时轮廓由面片光栅化后跟踪边界得到（曲面型腔的底面就是这种情形）。
    """

    from toolpath_lab.cam.floor_field import floor_surface_from_face

    surface = floor_surface_from_face(part, face_id, cell_mm=cell_mm,
                                      outline=outline, islands=islands)
    return surface.floor, surface.outline, surface.islands, surface.inside_mask


def _rasterize(polygon: NDArray[np.float64], grid_x: NDArray[np.float64],
               grid_y: NDArray[np.float64]) -> NDArray[np.bool_]:
    """向量化的射线法栅格化（外轮廓与岛屿共用）。"""

    inside = np.zeros(grid_x.shape, dtype=bool)
    count = polygon.shape[0]
    for index in range(count):
        x0, y0 = float(polygon[index, 0]), float(polygon[index, 1])
        x1, y1 = float(polygon[(index + 1) % count, 0]), float(polygon[(index + 1) % count, 1])
        if y0 == y1:
            continue
        crosses = (y0 > grid_y) != (y1 > grid_y)
        denominator = y1 - y0
        with np.errstate(divide="ignore", invalid="ignore"):
            intersect = x0 + (grid_y - y0) * (x1 - x0) / denominator
        inside ^= crosses & (grid_x < intersect)
    return inside


def _distance_to_contour(inside: NDArray[np.bool_], polygon: NDArray[np.float64],
                         x_min: float, y_min: float, spacing: float) -> NDArray[np.float64]:
    """内部单元到某条轮廓线的最短距离（毫米，向量化）。

    在栅格上直接对**原始多边形**求距离，比"栅格化 → 距离变换"精确得多：
    不会出现阶梯状的距离场，等距区域（进而环切刀轨）因此是平滑的。
    """

    rows, cols = inside.shape
    xs = x_min + (np.arange(rows) + 0.5) * spacing
    ys = y_min + (np.arange(cols) + 0.5) * spacing
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")

    best = np.full(inside.shape, np.inf, dtype=np.float64)
    ends = np.roll(polygon, -1, axis=0)
    for index in range(polygon.shape[0]):
        ax, ay = float(polygon[index, 0]), float(polygon[index, 1])
        bx, by = float(ends[index, 0]), float(ends[index, 1])
        dx, dy = bx - ax, by - ay
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-18:
            distance = np.hypot(grid_x - ax, grid_y - ay)
        else:
            t = ((grid_x - ax) * dx + (grid_y - ay) * dy) / length_squared
            np.clip(t, 0.0, 1.0, out=t)
            distance = np.hypot(grid_x - (ax + t * dx), grid_y - (ay + t * dy))
        np.minimum(best, distance, out=best)
    return best


def offset_outline_polygons(region: MachiningRegion, offset_mm: float,
                            *, min_area_mm2: float = 1.0,
                            mask: NDArray[np.bool_] | None = None
                            ) -> list[NDArray[np.float64]]:
    """把等距区域的边界提取成多边形（用于精修轮廓、环切与界面显示）。

    实现方式：对等距掩码做 **Moore 邻域边界跟踪**。距离场是栅格量，等值线天然呈阶梯状，
    直接上 marching squares 会得到互不相接的碎线段；而"取等距掩码、跟踪它的边界"
    得到的是闭合环，且每环恰好一条，正好就是环切需要的形状。

    ``mask`` 用来限定"这一层还能切的区域"（斜面/曲面时层高以上的部分会被裁掉）：
    给了它就把等距掩码与它求交，环只出现在两者都允许的地方。
    """

    active = region.offset_mask(offset_mm)
    if mask is not None:
        if mask.shape != active.shape:  # pragma: no cover - 调用方保证同栅格
            mask = None
        else:
            active = active & mask
    if not active.any():
        return []
    result: list[NDArray[np.float64]] = []
    for pixels in _trace_mask_boundaries(active):
        if len(pixels) < 4:
            continue
        # BUG-002 修：_trace_mask_boundaries 返回的是角点格 (i, j) 而非单元中心，
        # 之前按 (i+0.5)*cell 写世界坐标导致所有 2.5D 轮廓类刀路整体偏移半格。
        points = np.asarray(
            [(region.bounds[0] + i * region.cell_mm,
              region.bounds[1] + j * region.cell_mm) for i, j in pixels],
            dtype=np.float64,
        )
        polygon = _simplify(points, region.cell_mm * 0.35)
        if polygon.shape[0] < 3:
            continue
        if abs(_polygon_area(polygon)) < min_area_mm2:
            continue
        result.append(polygon)
    return result


#: 实心格的四条边对应的有向偏移：沿边走时**实心格在左侧**，于是
#: 外轮廓逆时针、孔洞顺时针，接链时每个顶点只有一个自然后继。
_BOUNDARY_STEPS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _boundary_edge_selections(mask: NDArray[np.bool_]) -> tuple[NDArray[np.bool_], ...]:
    """四条有向边界边的选择掩码（向量化，一次性筛出全部边界）。

    掩码单元 ``(i, j)`` 覆盖角点 ``(i, j)…(i+1, j+1)``。对每个角点 ``(I, J)``，
    它的四条邻边分别隔开两对单元；"实心格在左"的约定把每条边归属唯一确定：

    ==================  =====================  ==========================
    边（方向）          左侧实心格              触发条件
    ==================  =====================  ==========================
    ``(I,J)→(I+1,J)``   ``cell(I, J)``         ``cell(I,J)`` 实心、``cell(I,J-1)`` 空
    ``(I,J)→(I-1,J)``   ``cell(I-1, J-1)``     ``cell(I-1,J-1)`` 实心、``cell(I-1,J)`` 空
    ``(I,J)→(I,J+1)``   ``cell(I-1, J)``       ``cell(I-1,J)`` 实心、``cell(I,J)`` 空
    ``(I,J)→(I,J-1)``   ``cell(I, J-1)``       ``cell(I,J-1)`` 实心、``cell(I-1,J-1)`` 空
    ==================  =====================  ==========================

    这四条与逐行程拼边是**同一个边集**，只是这里一次性用 numpy 算出来：
    复杂度是 O(格点数) 的向量运算 + O(周长) 的 Python 接链，而逐行程实现是
    O(实心格数) 的纯 Python 循环（大掩码上慢两个数量级）。
    """

    rows, cols = mask.shape
    padded = np.zeros((rows + 2, cols + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask
    here = padded[1:, 1:]          # cell(I, J)
    left = padded[1:, :-1]         # cell(I, J-1)
    above = padded[:-1, 1:]        # cell(I-1, J)
    corner = padded[:-1, :-1]      # cell(I-1, J-1)
    return (
        here & ~left,
        corner & ~above,
        above & ~here,
        left & ~corner,
    )


def _trace_mask_boundaries(mask: NDArray[np.bool_]) -> list[list[tuple[int, int]]]:
    """由栅格掩码构造闭合边界环（坐标为**格点**坐标，不是单元中心）。

    先把四种朝向的边界边一次性向量化筛出来，再按端点接链成环：方向统一为
    "实心格在左"，因此外轮廓逆时针、孔洞顺时针，互不串环，接链本身就是一次哈希查找。
    """

    rows, cols = mask.shape
    if rows == 0 or cols == 0 or not bool(np.asarray(mask, dtype=bool).any()):
        return []
    starts: list[NDArray[np.int64]] = []
    ends: list[NDArray[np.int64]] = []
    for selection, (di, dj) in zip(_boundary_edge_selections(mask), _BOUNDARY_STEPS):
        index = np.argwhere(selection)
        if index.size == 0:
            continue
        starts.append(index)
        ends.append(index + np.array([di, dj], dtype=np.int64))
    if not starts:
        return []
    first = np.vstack(starts)
    second = np.vstack(ends)

    successors: dict[tuple[int, int], list[tuple[int, int]]] = {}
    predecessors: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for k in range(int(first.shape[0])):
        start = (int(first[k, 0]), int(first[k, 1]))
        end = (int(second[k, 0]), int(second[k, 1]))
        successors.setdefault(start, []).append(end)
        predecessors.setdefault(end, []).append(start)
    return _walk_edge_cycles(successors, predecessors)


def _walk_edge_cycles(successors: dict[tuple[int, int], list[tuple[int, int]]],
                      predecessors: dict[tuple[int, int], list[tuple[int, int]]]
                      ) -> list[list[tuple[int, int]]]:
    """把有向边集合分解成闭合环。

    每个顶点处选择"最靠右"的后继（顺时针优先），因此外轮廓逆时针、孔洞顺时针，
    互不串环。走不到起点时把本轮用掉的边还回去，避免误删其它环的边。
    """

    loops: list[list[tuple[int, int]]] = []
    used: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    total_edges = sum(len(item) for item in successors.values())
    for start in list(successors.keys()):
        while True:
            candidates = [end for end in successors.get(start, []) if (start, end) not in used]
            if not candidates:
                break
            current = start
            loop: list[tuple[int, int]] = [current]
            closed = False
            for _ in range(total_edges + 8):
                options = [end for end in successors.get(current, []) if (current, end) not in used]
                if not options:
                    break
                nxt = _prefer_turn(current, options, predecessors)
                used.add((current, nxt))
                current = nxt
                if current == start:
                    closed = True
                    break
                loop.append(current)
            if closed and len(loop) >= 4:
                loops.append(loop)
                continue
            for index in range(len(loop)):
                start_point = loop[index]
                end_point = loop[index + 1] if index + 1 < len(loop) else current
                used.discard((start_point, end_point))
            break
    return loops


def _prefer_turn(current: tuple[int, int], candidates: list[tuple[int, int]],
                 predecessors: dict[tuple[int, int], list[tuple[int, int]]]) -> tuple[int, int]:
    """在多个后继里挑一个：选"最靠右"的那条，保证沿边界走而不抄近道。

    用带符号转角（-180°~180°，顺时针为负）比较，而不是靠方向枚举猜顺序。
    在"外轮廓与孔洞共用一个格点"的地方，这个规则会让当前环继续贴着走，
    从而把孔洞留到后面单独成环。

    对角的两个实心格共用一个格点时，该点有两条入边与两条出边，入边必须**确定**地取
    （取坐标最小的那条）——否则环的拆分会随字典插入顺序变化，同一份掩码在不同调用
    顺序下给出不同的环，刀路就不再可复现。
    """

    if len(candidates) == 1:
        return candidates[0]
    incoming = predecessors.get(current, [])
    if not incoming:
        return min(candidates)
    previous = min(incoming)
    in_angle = np.arctan2(current[1] - previous[1], current[0] - previous[0])

    def turn(candidate: tuple[int, int]) -> float:
        out_angle = np.arctan2(candidate[1] - current[1], candidate[0] - current[0])
        angle = float(np.degrees(out_angle - in_angle)) % 360.0 - 180.0
        return angle

    # 顺时针（负角度）优先：外环因此逆时针、孔洞顺时针，互不串环
    return min(candidates, key=lambda candidate: (turn(candidate), candidate))


def _simplify(points: NDArray[np.float64], tolerance: float) -> NDArray[np.float64]:
    """去掉共线点，压缩折线规模（阶梯边缘因此变成干净的直线段）。"""

    if points.shape[0] < 3:
        return points
    previous = np.roll(points, 1, axis=0)
    following = np.roll(points, -1, axis=0)
    first = points - previous
    second = following - points
    cross = np.abs(first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0])
    length = np.maximum(np.hypot(first[:, 0], first[:, 1]), 1e-9)
    keep = cross > tolerance * length
    if int(keep.sum()) < 3:
        return points
    return np.asarray(points[keep], dtype=np.float64)


def _marching_squares(field: NDArray[np.float64], level: float,
                      region: MachiningRegion) -> list[NDArray[np.float64]]:
    """在标量场上提取等值线（向量化的 marching squares）。

    为了拿到"闭合环"，这里先把等值线拆成有向线段，再按端点接链：方向统一取
    "高值在左"，因此接链时每条线段只有一个后继，拼接很快也很稳。
    """

    rows, cols = field.shape
    if rows < 2 or cols < 2:
        return []
    cell = region.cell_mm
    x0, y0 = region.bounds[0], region.bounds[1]

    f00 = field[:-1, :-1]
    f10 = field[1:, :-1]
    f11 = field[1:, 1:]
    f01 = field[:-1, 1:]

    def frac(a: NDArray[np.float64], b: NDArray[np.float64]) -> NDArray[np.float64]:
        denominator = b - a
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(np.abs(denominator) > 1e-12, (level - a) / denominator, 0.5)
        return np.clip(ratio, 0.0, 1.0)

    index = np.arange(rows - 1)[:, None] * np.ones((1, cols - 1), dtype=np.int64)
    jndex = np.ones((rows - 1, 1), dtype=np.int64) * np.arange(cols - 1)[None, :]
    base_x = x0 + index * cell
    base_y = y0 + jndex * cell

    # 四条边上的交点（边编号：0 下、1 右、2 上、3 左）
    bottom = (base_x + frac(f00, f10) * cell, base_y + np.zeros_like(f00))
    right = (base_x + np.full_like(f00, cell), base_y + frac(f10, f11) * cell)
    top = (base_x + frac(f01, f11) * cell, base_y + np.full_like(f00, cell))
    left = (base_x + np.zeros_like(f00), base_y + frac(f00, f01) * cell)

    mask = np.stack([f00 >= level, f10 >= level, f11 >= level, f01 >= level], axis=0)
    # 每种四角状态对应的有向线段（起点边 -> 终点边），方向统一为"高值在左"，
    # 因此接链时每条线段只有一个后继。边编号：0 下、1 右、2 上、3 左。
    table: dict[tuple[bool, bool, bool, bool], tuple[tuple[int, int], ...]] = {
        (False, False, False, False): (),
        (True, True, True, True): (),
        (True, True, False, False): ((1, 2),),
        (False, True, True, False): ((2, 3),),
        (False, False, True, True): ((3, 0),),
        (True, False, False, True): ((0, 1),),
        (True, True, True, False): ((1, 3),),
        (True, True, False, True): ((0, 2),),
        (True, False, True, True): ((3, 1),),
        (False, True, True, True): ((2, 0),),
        (True, False, False, False): ((1, 0),),
        (False, True, False, False): ((2, 1),),
        (False, False, True, False): ((3, 2),),
        (False, False, False, True): ((0, 3),),
        (True, False, True, False): ((0, 1), (2, 3)),
        (False, True, False, True): ((1, 2), (3, 0)),
    }
    edges = [bottom, right, top, left]
    starts: list[tuple[float, float]] = []
    ends: list[tuple[float, float]] = []
    for i in range(rows - 1):
        for j in range(cols - 1):
            state = (bool(mask[0, i, j]), bool(mask[1, i, j]),
                     bool(mask[2, i, j]), bool(mask[3, i, j]))
            segments = table.get(state, ())
            if not segments:
                continue
            if _ambiguous(state):
                # 鞍点：用中心值决定接法
                center = float(field[i, j] + field[i + 1, j] + field[i + 1, j + 1] + field[i, j + 1]) / 4.0
                segments = ((0, 1), (2, 3)) if center >= level else ((0, 3), (1, 2))
            for start_edge, end_edge in segments:
                starts.append((float(edges[start_edge][0][i, j]), float(edges[start_edge][1][i, j])))
                ends.append((float(edges[end_edge][0][i, j]), float(edges[end_edge][1][i, j])))
    if not starts:
        return []
    return _chain_segments(list(zip(starts, ends)))


def _ambiguous(state: tuple[bool, bool, bool, bool]) -> bool:
    return state in {(True, False, True, False), (False, True, False, True)}


def _chain_segments(segments: Sequence[tuple[tuple[float, float], tuple[float, float]]],
                    tolerance: float = 1e-6) -> list[NDArray[np.float64]]:
    """把有向线段按端点接成闭合环。

    有向线段的方向统一为"高值在左"，因此每个端点最多只有一个后继，接链就是一次
    哈希查找。端点会先按容差量化——marching squares 的线性插值在两个相邻单元里
    算出的交点在浮点上并不严格相等。
    """

    if not segments:
        return []
    quantum = max(float(tolerance), 1e-9)

    def key(point: Sequence[float]) -> tuple[int, int]:
        return (int(round(float(point[0]) / quantum)), int(round(float(point[1]) / quantum)))

    # 端点量化后再匹配：以 1e-6 为容差在毫米尺度上偏严，放宽到 1e-4 更稳
    quantum = 1e-4
    successors: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[int, int]]]] = {}
    for start, end in segments:
        successors.setdefault(key(start), []).append((start, key(end)))

    loops: list[NDArray[np.float64]] = []
    used: set[tuple[int, int, int, int]] = set()
    for index, (start, end) in enumerate(segments):
        start_key, end_key = key(start), key(end)
        if (start_key[0], start_key[1], end_key[0], end_key[1]) in used:
            continue
        chain: list[Sequence[float]] = [start]
        current_key = start_key
        current_point = start
        guard = 0
        closed = False
        while guard <= len(segments) + 2:
            guard += 1
            options = successors.get(current_key, [])
            chosen = None
            for point, next_key in options:
                marker = (current_key[0], current_key[1], next_key[0], next_key[1])
                if marker in used:
                    continue
                chosen = (point, next_key, marker)
                break
            if chosen is None:
                break
            point, next_key, marker = chosen
            used.add(marker)
            if _close(point, chain[-1], 1e-4):
                pass
            else:
                chain.append(point)
            current_point = point
            current_key = next_key
            if current_key == start_key:
                closed = True
                break
        if closed and len(chain) >= 3:
            loops.append(np.asarray(chain, dtype=np.float64))
        _ = current_point
    return loops


def _close(a: Sequence[float], b: Sequence[float], tolerance: float) -> bool:
    return abs(float(a[0]) - float(b[0])) <= tolerance and abs(float(a[1]) - float(b[1])) <= tolerance


__all__ = [
    "DEFAULT_CELL_MM",
    "MachiningRegion",
    "adaptive_cell_mm",
    "build_region",
    "offset_outline_polygons",
    "point_in_polygon",
    "region_from_face",
]
