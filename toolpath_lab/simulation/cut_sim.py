"""毛坯切除仿真：Z-Map（高度图）材料去除。

原理与 NX 的"3D 动态仿真"一致，只是把体素换成了**Z-Map**（每个 XY 格点记录一个剩余
高度），这正是 2.5 轴铣削最自然的数据结构：

1. 把毛坯离散成规则栅格，初始高度 = 毛坯顶面；
2. 刀具沿刀路运动时，凡在刀具半径内的格点，其高度被"削"到刀底高度（平底刀端面切除）；
3. 每个采样时刻导出一份可渲染的三角网格，前端按帧播放即得到切削动画。

为什么是 Z-Map 而不是布尔体素：铣削总是从上往下切，同一个 XY 位置只关心"还剩多高"，
Z-Map 的精度与内存都比体素好一个数量级，而且网格导出几乎免费（一块 regular grid）。

精度与性能
----------
格距默认 0.5 mm（可配），毛坯 100×80 mm 时约 3 万个格点，一次全刀路仿真在普通桌面机上
是百毫秒级。导出帧数按"每帧至少切除一定体积"或固定步长控制，默认 ≤ 120 帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, pi, sqrt
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.stock import CylindricalStock, Mesh, RectangularStock, Stock

#: 默认格距（mm）：越小越精细，内存与耗时按平方增长。
DEFAULT_CELL_MM = 0.5
#: 允许的最小/最大格距。
MIN_CELL_MM = 0.1
MAX_CELL_MM = 5.0
#: 栅格总数上限（约 400 万格，对应几 MB 的高度图）。
MAX_CELLS = 4_000_000
#: 默认导出帧数上限。
DEFAULT_MAX_FRAMES = 120
#: 导出网格时是否包含侧壁与底面（关闭可显著减小载荷，只用于纯动画）。
INCLUDE_SIDES = True


@dataclass(slots=True)
class HeightField:
    """毛坯的 Z-Map。"""

    x0: float
    y0: float
    cell_mm: float
    height: NDArray[np.float64]   # (rows, cols)
    bottom_z: float
    kind: str = "rectangular"     # rectangular | cylindrical
    #: 圆柱毛坯的轴线位置与半径（kind == cylindrical 时有效）
    center: tuple[float, float] = (0.0, 0.0)
    radius: float = 0.0
    #: 格点是否属于毛坯（圆柱毛坯给出圆形掩码；矩形为全 True）
    active: NDArray[np.bool_] = field(default_factory=lambda: np.zeros((0, 0), dtype=bool))

    @property
    def shape(self) -> tuple[int, int]:
        return self.height.shape  # type: ignore[return-value]

    def copy(self) -> "HeightField":
        return HeightField(
            x0=self.x0, y0=self.y0, cell_mm=self.cell_mm,
            height=self.height.copy(), bottom_z=self.bottom_z,
            kind=self.kind, center=self.center, radius=self.radius,
            active=self.active.copy(),
        )

    def cell_centers(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        rows, cols = self.height.shape
        xs = self.x0 + (np.arange(rows) + 0.5) * self.cell_mm
        ys = self.y0 + (np.arange(cols) + 0.5) * self.cell_mm
        return np.meshgrid(xs, ys, indexing="ij")

    def bounds(self) -> tuple[float, float, float, float]:
        rows, cols = self.height.shape
        return (self.x0, self.y0,
                self.x0 + rows * self.cell_mm, self.y0 + cols * self.cell_mm)

    def volume_mm3(self) -> float:
        """剩余材料体积（栅格近似）。"""

        material = np.where(self.active, self.height - self.bottom_z, 0.0)
        return float(np.clip(material, 0.0, None).sum() * self.cell_mm * self.cell_mm)

    def surface_mesh(self, *, include_sides: bool = INCLUDE_SIDES) -> Mesh:
        """把高度图导出成可渲染网格。

        顶面按高度图三角化；``include_sides`` 时补上四周侧壁（矩形毛坯）或外侧圆壁
        （圆柱毛坯），于是得到一块闭合的实体，视觉上就是"一块被切过的料"。
        """

        rows, cols = self.height.shape
        active = self.active
        index = np.full((rows, cols), -1, dtype=np.int64)
        index[active] = np.arange(int(active.sum()), dtype=np.int64)

        grid_x, grid_y = self.cell_centers()
        positions = np.column_stack((grid_x[active], grid_y[active], self.height[active]))

        # 顶面三角化（向量化）：相邻四格都活跃就出两个三角形。
        # 这里以前是 Python 双重循环，40 万格要跑几十万次解释器循环。
        a = index[:-1, :-1]
        b = index[1:, :-1]
        c = index[1:, 1:]
        d = index[:-1, 1:]
        keep = (a >= 0) & (b >= 0) & (c >= 0) & (d >= 0)
        a, b, c, d = a[keep], b[keep], c[keep], d[keep]
        indices = np.empty((a.size * 2, 3), dtype=np.int64)
        indices[0::2, 0], indices[0::2, 1], indices[0::2, 2] = a, b, c
        indices[1::2, 0], indices[1::2, 1], indices[1::2, 2] = a, c, d

        if include_sides:
            positions, indices = _add_side_walls(self, index, positions, indices)
        return Mesh(np.asarray(positions, dtype=np.float64),
                    np.asarray(indices, dtype=np.int64))


def _add_side_walls(field: HeightField, index: NDArray[np.int64],
                    positions: NDArray[np.float64],
                    indices: NDArray[np.int64]
                    ) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """给高度图补侧壁：沿"非活跃区"的边缘向下拉到底面。

    全程向量化。原来的写法有两处致命开销：``field.cell_centers()``（对整个栅格做
    meshgrid）被写在**逐格循环里**，于是 40 万格就做了 40 万次全栅格 meshgrid ——
    200×200 的毛坯光这一步就要 140 秒；另外每面墙都单独建一个 (2,3) 数组再 vstack，
    墙一多同样是几十万次小对象操作。
    """

    rows, cols = field.height.shape
    active = field.active
    bottom = float(field.bottom_z)
    half = field.cell_mm * 0.5
    # 只算一次（原写法把它放在内层循环里，这是最要命的一处）
    grid_x, grid_y = field.cell_centers()

    # 四个方向"邻居是否活跃"：越界一律当成不活跃，于是毛坯外沿自然补墙。
    neighbours: list[NDArray[np.bool_]] = []
    for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        shifted = np.zeros_like(active)
        src_i = slice(max(di, 0), rows + min(di, 0))
        dst_i = slice(max(-di, 0), rows + min(-di, 0))
        src_j = slice(max(dj, 0), cols + min(dj, 0))
        dst_j = slice(max(-dj, 0), cols + min(-dj, 0))
        shifted[dst_i, dst_j] = active[src_i, src_j]
        neighbours.append(shifted)

    # 每个方向两个底角（单位：格宽的一半），顺序沿用原来的绕向
    corners = (
        ((0.0, 1.0), (0.0, -1.0)),
        ((0.0, -1.0), (0.0, 1.0)),
        ((-1.0, 0.0), (1.0, 0.0)),
        ((1.0, 0.0), (-1.0, 0.0)),
    )

    wall_positions: list[NDArray[np.float64]] = []
    wall_faces: list[NDArray[np.int64]] = []
    base = int(positions.shape[0])
    for shifted, (corner_a, corner_b) in zip(neighbours, corners):
        need = active & ~shifted
        if not need.any():
            continue
        ii, jj = np.nonzero(need)
        count = int(ii.size)
        x = grid_x[ii, jj]
        y = grid_y[ii, jj]
        top = index[ii, jj]
        floor = np.full(count, bottom, dtype=np.float64)
        a = np.column_stack((x + corner_a[0] * half, y + corner_a[1] * half, floor))
        b = np.column_stack((x + corner_b[0] * half, y + corner_b[1] * half, floor))
        # 注意：这里把 a、b 作为**两块**追加，vstack 出来是 [a 全部][b 全部]，
        # 不是交错；下标必须按块算，否则底点会和错误的顶面顶点连起来（面会扭）。
        bottom_a = base + np.arange(count, dtype=np.int64)
        bottom_b = bottom_a + count
        wall_positions.extend((a, b))
        wall_faces.append(np.column_stack((top, bottom_a, bottom_b)))
        wall_faces.append(np.column_stack((top, bottom_b, top)))
        base += count * 2

    if not wall_positions:
        return positions, indices
    new_positions = np.vstack([positions, *wall_positions])
    new_indices = np.vstack([indices, *wall_faces]) if indices.size else np.vstack(wall_faces)
    return new_positions, new_indices


# ------------------------------------------------------------------ 构造
def build_height_field(stock: Stock, *, cell_mm: float = DEFAULT_CELL_MM) -> HeightField:
    """按毛坯形状建立初始高度图。"""

    bounds = stock.bounds
    spacing = float(np.clip(cell_mm, MIN_CELL_MM, MAX_CELL_MM))
    size_x = bounds.x_max - bounds.x_min
    size_y = bounds.y_max - bounds.y_min
    rows = max(2, int(ceil(size_x / spacing)))
    cols = max(2, int(ceil(size_y / spacing)))
    while rows * cols > MAX_CELLS:
        spacing *= 1.25
        rows = max(2, int(ceil(size_x / spacing)))
        cols = max(2, int(ceil(size_y / spacing)))

    x0 = bounds.x_min
    y0 = bounds.y_min
    height = np.full((rows, cols), bounds.z_max, dtype=np.float64)
    if isinstance(stock, CylindricalStock):
        center_x, center_y = 0.5 * (bounds.x_min + bounds.x_max), 0.5 * (bounds.y_min + bounds.y_max)
        radius = 0.5 * (bounds.x_max - bounds.x_min)
        xs = x0 + (np.arange(rows) + 0.5) * spacing
        ys = y0 + (np.arange(cols) + 0.5) * spacing
        grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")
        active = (grid_x - center_x) ** 2 + (grid_y - center_y) ** 2 <= radius * radius
        return HeightField(
            x0=x0, y0=y0, cell_mm=spacing, height=height, bottom_z=bounds.z_min,
            kind="cylindrical", center=(center_x, center_y), radius=radius, active=active,
        )
    return HeightField(
        x0=x0, y0=y0, cell_mm=spacing, height=height, bottom_z=bounds.z_min,
        kind="rectangular", active=np.ones((rows, cols), dtype=bool),
    )


# ------------------------------------------------------------------ 切除
def cut_move(field: HeightField, move: Move, tool_radius: float, *,
             tolerance: float = 0.02) -> float:
    """用一段运动切削毛坯，返回本段切除的体积（mm³）。

    平底刀端面切除：刀底以下、且距刀轴轨迹不超过刀具半径的材料被移除。
    对每一条直线段一次算完（**刀轴扫过的胶囊体**），不做逐点采样——
    逐点采样在长刀路上会慢两个数量级，而结果完全一样。
    """

    if move.kind is MoveKind.RAPID:
        return 0.0
    points = np.asarray(move.points, dtype=np.float64)
    if points.shape[0] < 2:
        return 0.0
    removed = 0.0
    for index in range(points.shape[0] - 1):
        removed += _cut_segment(field, points[index], points[index + 1], tool_radius, tolerance)
    return removed


def _cut_segment(field: HeightField, a: NDArray[np.float64], b: NDArray[np.float64],
                 tool_radius: float, tolerance: float) -> float:
    """切削一条直线段：把落在"刀轴轨迹胶囊体"内的格点高度压到刀底。

    对格点 c，取它到线段的最远参数 t∈[0,1] 处的刀底高度作为切除高度，
    这样倾斜下刀（斜插）也能正确处理。
    """

    spacing = field.cell_mm
    rows, cols = field.height.shape
    radius = float(tool_radius)
    x_min = min(float(a[0]), float(b[0])) - radius
    x_max = max(float(a[0]), float(b[0])) + radius
    y_min = min(float(a[1]), float(b[1])) - radius
    y_max = max(float(a[1]), float(b[1])) + radius
    i0 = max(0, int(np.floor((x_min - field.x0) / spacing)))
    i1 = min(rows - 1, int(np.ceil((x_max - field.x0) / spacing)))
    j0 = max(0, int(np.floor((y_min - field.y0) / spacing)))
    j1 = min(cols - 1, int(np.ceil((y_max - field.y0) / spacing)))
    if i0 > i1 or j0 > j1:
        return 0.0

    xs = field.x0 + (np.arange(i0, i1 + 1) + 0.5) * spacing
    ys = field.y0 + (np.arange(j0, j1 + 1) + 0.5) * spacing
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")

    dx, dy = float(b[0] - a[0]), float(b[1] - a[1])
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        distance = np.hypot(grid_x - float(a[0]), grid_y - float(a[1]))
        ratio = np.zeros_like(distance)
    else:
        raw = ((grid_x - float(a[0])) * dx + (grid_y - float(a[1])) * dy) / length_squared
        ratio = np.clip(raw, 0.0, 1.0)
        distance = np.hypot(grid_x - (float(a[0]) + ratio * dx), grid_y - (float(a[1]) + ratio * dy))
    inside = distance <= radius
    if not inside.any():
        return 0.0

    floor_z = (float(a[2]) + (float(b[2]) - float(a[2])) * ratio) - tolerance
    block = field.height[i0:i1 + 1, j0:j1 + 1]
    active = field.active[i0:i1 + 1, j0:j1 + 1]
    cuttable = inside & active & (block > floor_z)
    if not cuttable.any():
        return 0.0
    before = np.clip(block[cuttable] - field.bottom_z, 0.0, None).sum()
    block[cuttable] = floor_z[cuttable]
    after = np.clip(block[cuttable] - field.bottom_z, 0.0, None).sum()
    return float(before - after) * spacing * spacing


# ------------------------------------------------------------------ 仿真
@dataclass(slots=True)
class SimulationFrame:
    """一帧仿真状态。"""

    time_s: float
    move_index: int
    position: tuple[float, float, float]
    removed_mm3: float
    #: 高度图（只在需要时附上，界面按需取用）
    height: NDArray[np.float64] | None = None

    def to_payload(self, *, with_height: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "time_s": round(self.time_s, 4),
            "move_index": self.move_index,
            "position": [round(float(value), 4) for value in self.position],
            "removed_mm3": round(self.removed_mm3, 2),
        }
        if with_height and self.height is not None:
            payload["height"] = [round(float(value), 3) for value in self.height.reshape(-1)]
        return payload


@dataclass(slots=True)
class SimulationResult:
    """一次毛坯切除仿真的完整结果。"""

    stock: dict[str, Any]
    grid: dict[str, Any]
    frames: list[SimulationFrame]
    final: HeightField
    toolpath_statistics: dict[str, Any]
    initial_volume_mm3: float
    remaining_volume_mm3: float
    removed_volume_mm3: float
    target_volume_mm3: float | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        deviation = None
        if self.target_volume_mm3:
            deviation = (self.remaining_volume_mm3 - self.target_volume_mm3) / self.target_volume_mm3
        return {
            "initial_volume_mm3": round(self.initial_volume_mm3, 2),
            "remaining_volume_mm3": round(self.remaining_volume_mm3, 2),
            "removed_volume_mm3": round(self.removed_volume_mm3, 2),
            "removed_ratio": round(
                self.removed_volume_mm3 / self.initial_volume_mm3, 4
            ) if self.initial_volume_mm3 > 0 else 0.0,
            "target_volume_mm3": None if self.target_volume_mm3 is None else round(self.target_volume_mm3, 2),
            "volume_deviation": None if deviation is None else round(deviation, 4),
            "frame_count": len(self.frames),
            "warnings": list(self.warnings),
        }


def simulate_toolpath(toolpath: Toolpath, stock: Stock, *, tool_radius: float,
                      cell_mm: float = DEFAULT_CELL_MM,
                      max_frames: int = DEFAULT_MAX_FRAMES,
                      sample_mm: float = 0.0,
                      target_volume_mm3: float | None = None
                      ) -> SimulationResult:
    """按刀路做毛坯切除仿真，返回逐帧状态与最终毛坯。

    ``sample_mm`` 为 0 时按"总长 / max_frames"自动决定采样步长；每帧都记录高度图，
    因此界面可以播放、暂停、拖动到任意一帧。
    """

    field = build_height_field(stock, cell_mm=cell_mm)
    initial_volume = field.volume_mm3()
    warnings: list[str] = []

    total_length = toolpath.total_length_mm
    if sample_mm <= 0.0:
        frames_target = max(2, int(max_frames))
        sample_mm = max(toolpath.cell_hint() if hasattr(toolpath, "cell_hint") else 0.0, 0.0)
        sample_mm = max(total_length / frames_target, cell_mm)
    sample_mm = max(sample_mm, cell_mm)

    frames: list[SimulationFrame] = []
    removed_total = 0.0
    time_s = 0.0
    travelled = 0.0
    next_frame_at = 0.0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    if field.height.size:
        position = (float(field.x0), float(field.y0), float(field.height.max()))

    for move_index, move in enumerate(toolpath.moves):
        points = np.asarray(move.points, dtype=np.float64)
        lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
        move_length = float(lengths.sum())
        if move_length <= 1e-12:
            continue
        feed = max(float(move.feed_mm_per_min), 1e-6)
        steps = max(1, int(ceil(move_length / max(cell_mm, 0.05))))
        segment_count = max(1, lengths.size)
        for step in range(steps):
            ratio = (step + 1) / steps
            target = ratio * move_length
            # 找到目标里程落在第几段，并在段内插值
            cumulative = np.cumsum(lengths)
            segment_index = int(np.searchsorted(cumulative, target, side="left"))
            segment_index = min(segment_index, segment_count - 1)
            before = float(cumulative[segment_index - 1]) if segment_index > 0 else 0.0
            segment_length = float(lengths[segment_index])
            local = 0.0 if segment_length <= 1e-12 else (target - before) / segment_length
            local = float(np.clip(local, 0.0, 1.0))
            start = points[segment_index]
            end = points[segment_index + 1]
            current = start + (end - start) * local
            position = (float(current[0]), float(current[1]), float(current[2]))

            if move.kind is not MoveKind.RAPID:
                previous = points[segment_index] if local > 0.0 else points[max(segment_index - 1, 0)]
                segment = Move(move.kind, np.vstack([previous, current]), move.feed_mm_per_min)
                removed_total += cut_move(field, segment, tool_radius)

            travelled += segment_length / max(steps, 1) if steps else 0.0
            time_s += (segment_length / max(steps, 1)) / feed * 60.0
            if travelled + 1e-9 >= next_frame_at:
                frames.append(SimulationFrame(
                    time_s=time_s, move_index=move_index, position=position,
                    removed_mm3=removed_total, height=field.height.copy(),
                ))
                next_frame_at = travelled + sample_mm

    if not frames:
        frames.append(SimulationFrame(
            time_s=0.0, move_index=0, position=position, removed_mm3=0.0,
            height=field.height.copy(),
        ))

    remaining = field.volume_mm3()
    if target_volume_mm3 is not None and target_volume_mm3 > 0:
        deviation = abs(remaining - target_volume_mm3) / target_volume_mm3
        if deviation > 0.15:
            warnings.append(
                f"仿真后毛坯体积 {remaining:.0f} mm³ 与零件体积 {target_volume_mm3:.0f} mm³ "
                f"相差 {deviation * 100:.0f}%：可能是余量未切净、刀具过大或区域选择不当"
            )

    return SimulationResult(
        stock=_stock_payload(stock),
        grid=_grid_payload(field),
        frames=frames,
        final=field,
        toolpath_statistics=toolpath.statistics(),
        initial_volume_mm3=initial_volume,
        remaining_volume_mm3=remaining,
        removed_volume_mm3=max(0.0, initial_volume - remaining),
        target_volume_mm3=target_volume_mm3,
        warnings=warnings,
    )


def _grid_payload(field: HeightField) -> dict[str, Any]:
    """高度图的描述（前端用它把 height 数组还原成三维网格）。"""

    return {
        "kind": field.kind,
        "x0": round(field.x0, 4),
        "y0": round(field.y0, 4),
        "cell_mm": round(field.cell_mm, 4),
        "rows": int(field.height.shape[0]),
        "cols": int(field.height.shape[1]),
        "bottom_z": round(field.bottom_z, 4),
        "center": [round(float(value), 4) for value in field.center],
        "radius": round(float(field.radius), 4),
        "active": [bool(value) for value in field.active.reshape(-1).tolist()],
        "initial_height": [round(float(value), 3) for value in field.height.reshape(-1).tolist()],
    }


def _stock_payload(stock: Stock) -> dict[str, Any]:
    return {
        "id": stock.id,
        "label": stock.label,
        "bounds": stock.bounds.to_payload(),
        "volume_mm3": round(stock.volume_mm3(), 3),
    }


__all__ = [
    "DEFAULT_CELL_MM",
    "DEFAULT_MAX_FRAMES",
    "HeightField",
    "SimulationFrame",
    "SimulationResult",
    "build_height_field",
    "cut_move",
    "simulate_toolpath",
]
