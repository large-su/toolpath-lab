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
    notes: list[str] = field(default_factory=list)

    # -- 基本量 ------------------------------------------------------------
    @property
    def depth_mm(self) -> float:
        return max(0.0, float(self.top_z - self.floor_z))

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
                     ceiling_z: float | None = None) -> MachiningRegion:
    """由选中的平面面构造加工区域。

    ``ceiling_z`` 是这一层加工的**起始高度**（毛坯顶面或上一层的底），
    ``floor_z`` 取自面本身的平面方程。两者之差就是这一道工序要切除的深度：

    - 平面铣顶面时，ceiling 取毛坯顶面，于是深度 = 毛坯余量；
    - 型腔铣腔底时，ceiling 取毛坯顶面，深度 = 从毛坯顶到腔底的整段深度。

    不传 ``ceiling_z`` 时退回零件顶面，此时平面铣的深度为 0（只做一刀光面）。
    """

    record = part.face(int(face_id))
    if record is None:
        raise PlanningError(f"找不到序号为 {face_id} 的面")
    if not record.is_planar:
        raise PlanningError(f"面 #{face_id} 不是平面（{record.surface_kind}），暂不支持加工")
    if record.plane is None:
        raise PlanningError(f"面 #{face_id} 缺少平面方程")

    normal = np.asarray(record.normal, dtype=np.float64)
    if normal[2] <= 0.0:
        raise PlanningError(
            f"面 #{face_id} 的法向不朝上（{tuple(round(float(v), 3) for v in normal)}），"
            "请在三维视图中选择朝上的加工面"
        )
    # BUG-003 修：先前对斜面也放行，但 floor_z 取的是平面方程的常数项 d=n·atan(r00)，
    # 仅当 n=(0,0,1) 时它才是 Z；任意倾斜面（nz<1）下 d 与高度无关 → 深度计算错误。
    # 2.5D 平面铣/型腔铣仅支持朝上的水平面，曲面工序请走 surface_strategy。
    if normal[2] < 1.0 - 1e-3:
        raise PlanningError(
            f"面 #{face_id} 不是近水平面（nz={normal[2]:.4f}），"
            "平面铣/型腔铣仅支持朝上的水平面；倾斜面请改用曲面工序"
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

    floor_z = float(record.plane[3])
    start_z = float(part.bounds.z_max if ceiling_z is None else ceiling_z)
    if start_z < floor_z:
        start_z = floor_z

    return build_region(outline, islands, top_z=start_z, floor_z=floor_z, cell_mm=cell_mm)


def planar_features(part: PartModel) -> list[dict[str, Any]]:
    """可用于加工的平面面清单（供前端做特征树与快速选择）。"""

    result: list[dict[str, Any]] = []
    for feature in part.features:
        if not feature.get("planar"):
            continue
        normal = feature["normal"]
        item = dict(feature)
        item["machinable"] = bool(normal[2] > 0.0)
        item["role"] = (
            "顶面/台阶面" if normal[2] > 0.999
            else ("侧面" if abs(normal[2]) < 0.001 else "斜面")
        )
        result.append(item)
    return result


def build_region(outline: NDArray[np.float64], islands: Sequence[NDArray[np.float64]] = (),
                 *, top_z: float, floor_z: float, cell_mm: float = DEFAULT_CELL_MM
                 ) -> MachiningRegion:
    """由外轮廓与岛屿建栅格、算距离场。"""

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
        notes=notes,
    )


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
                            *, min_area_mm2: float = 1.0) -> list[NDArray[np.float64]]:
    """把等距区域的边界提取成多边形（用于精修轮廓、环切与界面显示）。

    实现方式：对等距掩码做 **Moore 邻域边界跟踪**。距离场是栅格量，等值线天然呈阶梯状，
    直接上 marching squares 会得到互不相接的碎线段；而"取等距掩码、跟踪它的边界"
    得到的是闭合环，且每环恰好一条，正好就是环切需要的形状。
    """

    mask = region.offset_mask(offset_mm)
    if not mask.any():
        return []
    result: list[NDArray[np.float64]] = []
    for pixels in _trace_mask_boundaries(mask):
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


_MOORE = ((-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1))


def _build_corner_table() -> dict[int, tuple[tuple[int, int], ...]]:
    """按"实心格在左"的规则，为一个 2×2 单元生成 16 种状态的有向边界。

    直接按几何推导，而不是手写 16 行——手写极易把角的顺序弄反，而这类错误在大网格上
    表现为"轮廓接不上"，很难定位。方向约定：沿边走时实心格在左侧，于是
    "下边 0→1、右边 2→1、上边 2→3、左边 3→0"。
    """

    # 位顺序: bit0=(0,0), bit1=(1,0), bit2=(1,1), bit3=(0,1)
    table: dict[int, tuple[tuple[int, int], ...]] = {}
    for state in range(16):
        solid = [bool(state >> bit & 1) for bit in range(4)]

        def at(i: int, j: int) -> bool:
            if i < 0 or j < 0 or i > 1 or j > 1:
                return False
            return solid[i + 2 * j]

        edges: list[tuple[int, int]] = []
        if solid[0]:
            if not at(0, -1):  # 下边暴露
                edges.append((0, 1))
            if not at(1, 0):   # 右边暴露
                edges.append((2, 1))
            if not at(1, 1):   # 上边暴露
                edges.append((2, 3))
            if not at(-1, 0):  # 左边暴露
                edges.append((3, 0))
        if solid[1]:
            if not at(1, -1):
                edges.append((1, 2))
            if not at(2, 0):
                edges.append((3, 2))
            if not at(2, 1):
                edges.append((3, 0))
            if not at(0, 0):
                edges.append((0, 1))
        if solid[2]:
            if not at(1, 0):
                edges.append((2, 3))
            if not at(2, 1):
                edges.append((0, 3))
            if not at(2, 2):
                edges.append((0, 1))
            if not at(1, 1):
                edges.append((1, 2))
        if solid[3]:
            if not at(0, 1):
                edges.append((3, 0))
            if not at(1, 1):
                edges.append((1, 0))
            if not at(1, 2):
                edges.append((1, 2))
            if not at(0, 0):
                edges.append((2, 0))
        table[state] = tuple(edges)
    return table


_CORNER_TABLE: dict[int, tuple[tuple[int, int], ...]] = _build_corner_table()


def _trace_mask_boundaries(mask: NDArray[np.bool_]) -> list[list[tuple[int, int]]]:
    """由栅格掩码构造闭合边界环（按行"行程"拼接，坐标为格点坐标）。

    关键点是**按相邻行的覆盖把每条行程切成若干段**：被上一行完全盖住的部分是内部，
    暴露的部分才产生上边界；下边界同理。整条行程只有"全盖"或"全露"两种状态的想法
    是错的——部分覆盖（行的两端同时进出）正是孔洞与窄缝出现的地方，切分之后每条边界
    线段都是整齐的格点对，接链必然闭合。
    """

    rows, cols = mask.shape
    runs_by_row: list[list[tuple[int, int]]] = []
    for i in range(rows):
        row = mask[i]
        if not row.any():
            runs_by_row.append([])
            continue
        padded = np.concatenate(([False], row, [False]))
        changes = np.flatnonzero(padded[1:] != padded[:-1])
        runs_by_row.append([(int(changes[k]), int(changes[k + 1] - 1))
                            for k in range(0, changes.size, 2)])

    successors: dict[tuple[int, int], list[tuple[int, int]]] = {}
    predecessors: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add_edge(start: tuple[int, int], end: tuple[int, int]) -> None:
        successors.setdefault(start, []).append(end)
        predecessors.setdefault(end, []).append(start)

    def covered(row_runs: list[tuple[int, int]], column: int) -> bool:
        return any(j0 <= column <= j1 for j0, j1 in row_runs)

    for i in range(rows):
        above = runs_by_row[i - 1] if i > 0 else []
        below = runs_by_row[i + 1] if i + 1 < rows else []
        for j0, j1 in runs_by_row[i]:
            # 上边界：上方未覆盖的连续列段（方向自右向左）
            start: int | None = None
            for column in range(j0, j1 + 1):
                exposed = not covered(above, column)
                if exposed and start is None:
                    start = column
                if start is not None and (not exposed or column == j1):
                    end = column if exposed else column - 1
                    add_edge((i, end + 1), (i, start))
                    start = None
            # 下边界：下方未覆盖的连续列段（方向自左向右）
            start = None
            for column in range(j0, j1 + 1):
                exposed = not covered(below, column)
                if exposed and start is None:
                    start = column
                if start is not None and (not exposed or column == j1):
                    end = column if exposed else column - 1
                    add_edge((i + 1, start), (i + 1, end + 1))
                    start = None
            # 左右竖边
            if j0 == 0 or not mask[i, j0 - 1]:
                add_edge((i, j0), (i + 1, j0))
            if j1 + 1 >= cols or not mask[i, j1 + 1]:
                add_edge((i + 1, j1 + 1), (i, j1 + 1))

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
    """

    if len(candidates) == 1:
        return candidates[0]
    incoming = predecessors.get(current, [])
    if not incoming:
        return candidates[0]
    previous = incoming[0]
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
    keep: list[NDArray[np.float64]] = []
    count = points.shape[0]
    for index in range(count):
        previous = points[(index - 1) % count]
        current = points[index]
        following = points[(index + 1) % count]
        first = current - previous
        second = following - current
        cross = abs(float(first[0] * second[1] - first[1] * second[0]))
        if cross > tolerance * max(float(np.linalg.norm(first)), 1e-9):
            keep.append(current)
    if len(keep) < 3:
        return points
    return np.asarray(keep, dtype=np.float64)


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
