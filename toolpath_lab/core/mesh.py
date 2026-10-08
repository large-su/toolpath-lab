"""导入模型：STL 解析、XY 投影轮廓与 Z-map 高度场。

"导入模型"在这里被刻意做得很小：一个模型就是一堆三角形，加上规划真正需要的两样东西：

- **轮廓**：模型在 XY 平面的投影轮廓（栅格刀路要待在里面）；
- **高度场** z = f(x, y)：也就是 **Z-map**，它把平面栅格刀路变成三轴曲面刀路。

只用标准库 + numpy，所以读 .stl 不需要任何额外依赖；二进制与 ASCII 两种 STL 都支持。
模型一旦导入，就和内置的参数化曲面（平面 / 斜面 / 波浪面）没有区别了。
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError

#: Z-map 的节点数上限：再大就把分辨率放宽，避免一次规划吃掉几百 MB 内存。
MAX_GRID_NODES = 1_000_000
#: 同时保留的模型数量上限（导入的模型只存在内存里，重启即失效）。
MAX_MODELS = 8
#: 返回给前端做三维显示时最多发送多少三角形（再多就抽样显示）。
MAX_DISPLAY_TRIANGLES = 40_000
#: 模型自带的默认 Z-map 分辨率（mm）。
DEFAULT_RESOLUTION_MM = 1.0

#: 高度场的取面方式：最高面（凸台/外表面）或最低面（凹腔/型腔底面）。
PICK_TOP = "top"
PICK_BOTTOM = "bottom"
HEIGHT_PICKS: tuple[tuple[str, str], ...] = (
    (PICK_TOP, "最高面 Top"),
    (PICK_BOTTOM, "最低面 Bottom"),
)
PICK_LABELS: dict[str, str] = dict(HEIGHT_PICKS)

_ASCII_VERTEX = re.compile(r"vertex\s+([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)")
#: 二进制 STL 的三角形记录布局：法向量 3 + 三个顶点各 3 + 属性 2 = 50 字节。
_BINARY_TRIANGLE = np.dtype(
    [
        ("normal", "<f4", (3,)),
        ("v0", "<f4", (3,)),
        ("v1", "<f4", (3,)),
        ("v2", "<f4", (3,)),
        ("attribute", "<u2"),
    ]
)
BINARY_HEADER_BYTES = 84
BINARY_TRIANGLE_BYTES = 50


# ---------------------------------------------------------------- 三角形网格
@dataclass(frozen=True, slots=True)
class Mesh:
    """一个三角网格（三角形汤，不做拓扑合并）。"""

    triangles: NDArray[np.float64]

    def __post_init__(self) -> None:
        array = np.array(self.triangles, dtype=np.float64, copy=True).reshape(-1, 3, 3)
        if array.shape[0] < 1:
            raise ParameterError("模型里一个三角形都没有")
        if not np.all(np.isfinite(array)):
            raise ParameterError("模型顶点坐标必须是有限值")
        array.setflags(write=False)
        object.__setattr__(self, "triangles", array)

    @property
    def triangle_count(self) -> int:
        return int(self.triangles.shape[0])

    @property
    def bounds(self) -> tuple[float, float, float, float, float, float]:
        """(x_min, x_max, y_min, y_max, z_min, z_max)。"""

        flat = self.triangles.reshape(-1, 3)
        lower = flat.min(axis=0)
        upper = flat.max(axis=0)
        return (
            float(lower[0]), float(upper[0]),
            float(lower[1]), float(upper[1]),
            float(lower[2]), float(upper[2]),
        )

    @property
    def size_mm(self) -> tuple[float, float, float]:
        x_min, x_max, y_min, y_max, z_min, z_max = self.bounds
        return (x_max - x_min, y_max - y_min, z_max - z_min)

    def xy_outline(self, mode: str = "hull") -> NDArray[np.float64]:
        """模型在 XY 平面的外轮廓，逆时针、不重复首点。

        hull：所有顶点投影后的凸包（贴合零件外形，凹进去的地方会补上）；
        box ：包围盒（矩形，边缘整齐、计算最简单）。
        """

        x_min, x_max, y_min, y_max, _, _ = self.bounds
        if mode == "box":
            return np.array(
                [
                    (x_min, y_min),
                    (x_max, y_min),
                    (x_max, y_max),
                    (x_min, y_max),
                ],
                dtype=np.float64,
            )
        if mode != "hull":
            raise ParameterError(f"未知的轮廓方式 {mode!r}，只能是 hull 或 box")
        return convex_hull_2d(self.triangles.reshape(-1, 3)[:, :2])

    def flatten_positions(self) -> list[float]:
        """展平的三角形坐标（每三个点一组），供三维显示直接使用。"""

        return [float(value) for value in np.round(self.triangles, 4).reshape(-1)]


def convex_hull_2d(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Andrew 单调链凸包，返回逆时针、不含重复首点的顶点。"""

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    pts = np.unique(np.round(pts, 6), axis=0)
    if pts.shape[0] < 3:
        raise ParameterError("模型的 XY 投影退化成了一个点或一条线，无法作为加工区域")
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

    def cross(origin: NDArray[np.float64], a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
        return float(
            (a[0] - origin[0]) * (b[1] - origin[1])
            - (a[1] - origin[1]) * (b[0] - origin[0])
        )

    lower: list[NDArray[np.float64]] = []
    for point in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list[NDArray[np.float64]] = []
    for point in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    hull = np.array(lower[:-1] + upper[:-1], dtype=np.float64)
    if hull.shape[0] < 3:
        raise ParameterError("模型的 XY 投影退化，无法生成加工区域")
    return hull


# ------------------------------------------------------------------ STL 解析
def load_stl(data: bytes) -> Mesh:
    """读入二进制或 ASCII 的 STL。"""

    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ParameterError("模型文件是空的")
    payload = bytes(data)
    triangles = _read_binary_stl(payload)
    if triangles is None:
        triangles = _read_ascii_stl(payload)
    if triangles is None:
        raise ParameterError("无法解析这个 STL：既不是二进制格式，也找不到 ASCII 的 facet/vertex")
    return Mesh(triangles)


def _read_binary_stl(data: bytes) -> NDArray[np.float64] | None:
    """按二进制 STL 读取；不是二进制格式时返回 None。"""

    if len(data) < BINARY_HEADER_BYTES:
        return None
    count = int(np.frombuffer(data, dtype="<u4", count=1, offset=80)[0])
    if count <= 0:
        return None
    needed = BINARY_HEADER_BYTES + count * BINARY_TRIANGLE_BYTES
    # 有些写出的文件会在末尾多带一点内容，所以这里是 <=。
    if needed > len(data):
        return None
    records = np.frombuffer(data, dtype=_BINARY_TRIANGLE, count=count, offset=BINARY_HEADER_BYTES)
    triangles = np.stack(
        [records["v0"], records["v1"], records["v2"]], axis=1
    ).astype(np.float64)
    return triangles


def _read_ascii_stl(data: bytes) -> NDArray[np.float64] | None:
    """按 ASCII STL 读取；读不出来时返回 None。"""

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - bytes.decode 已经用了 replace
        return None
    if "vertex" not in text:
        return None
    values = _ASCII_VERTEX.findall(text)
    if len(values) < 3:
        return None
    count = len(values) // 3
    flat = np.array(values[: count * 3], dtype=np.float64)
    return flat.reshape(-1, 3, 3)


# ------------------------------------------------------------------ 高度场
@dataclass(frozen=True, slots=True)
class HeightField:
    """规则网格上的 Z-map：zs[j, i] 是 (xs[i], ys[j]) 处的加工面高度。"""

    xs: NDArray[np.float64]
    ys: NDArray[np.float64]
    zs: NDArray[np.float64]
    pick: str = PICK_TOP
    coverage: float = 1.0

    @property
    def resolution_mm(self) -> float:
        step_x = (self.xs[-1] - self.xs[0]) / max(self.xs.size - 1, 1)
        step_y = (self.ys[-1] - self.ys[0]) / max(self.ys.size - 1, 1)
        return float(max(step_x, step_y))

    @property
    def node_count(self) -> int:
        return int(self.xs.size * self.ys.size)

    @property
    def z_range_mm(self) -> tuple[float, float]:
        return (float(self.zs.min()), float(self.zs.max()))

    def heights(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """双线性插值出任意 XY 处的高度。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        if planar.size == 0:
            return np.zeros(0, dtype=np.float64)
        span_x = self.xs[-1] - self.xs[0]
        span_y = self.ys[-1] - self.ys[0]
        fx = np.zeros(planar.shape[0], dtype=np.float64)
        fy = np.zeros(planar.shape[0], dtype=np.float64)
        if span_x > 1e-12:
            fx = (np.clip(planar[:, 0], self.xs[0], self.xs[-1]) - self.xs[0]) / span_x
        if span_y > 1e-12:
            fy = (np.clip(planar[:, 1], self.ys[0], self.ys[-1]) - self.ys[0]) / span_y
        fx *= self.xs.size - 1
        fy *= self.ys.size - 1
        i0 = np.clip(np.floor(fx).astype(np.int64), 0, self.xs.size - 2)
        j0 = np.clip(np.floor(fy).astype(np.int64), 0, self.ys.size - 2)
        tx = fx - i0
        ty = fy - j0
        z00 = self.zs[j0, i0]
        z10 = self.zs[j0, i0 + 1]
        z01 = self.zs[j0 + 1, i0]
        z11 = self.zs[j0 + 1, i0 + 1]
        return (
            z00 * (1.0 - tx) * (1.0 - ty)
            + z10 * tx * (1.0 - ty)
            + z01 * (1.0 - tx) * ty
            + z11 * tx * ty
        )

    def describe(self) -> dict[str, Any]:
        return {
            "resolution_mm": round(self.resolution_mm, 4),
            "node_count": self.node_count,
            "pick": self.pick,
            "pick_label": PICK_LABELS.get(self.pick, self.pick),
            "coverage": round(self.coverage, 4),
            "z_range_mm": [round(value, 4) for value in self.z_range_mm],
        }


def build_height_field(
    mesh: Mesh,
    *,
    resolution_mm: float = DEFAULT_RESOLUTION_MM,
    pick: str = PICK_TOP,
    max_nodes: int = MAX_GRID_NODES,
) -> tuple[HeightField, tuple[str, ...]]:
    """把三角网格光栅化成 Z-map。

    对每个网格节点取所有覆盖它的三角形的最大（top）或最小（bottom）高度，
    于是"凸台顶面"与"型腔底面"都能加工。没有三角形覆盖的节点（区域伸到模型投影之外）
    用最近的有效高度补上，并记一条提醒。
    """

    if pick not in PICK_LABELS:
        raise ParameterError(f"未知的取面方式 {pick!r}，只能是 top 或 bottom")
    resolution = max(float(resolution_mm), 1e-3)
    x_min, x_max, y_min, y_max, _, _ = mesh.bounds
    span_x = max(x_max - x_min, 1e-6)
    span_y = max(y_max - y_min, 1e-6)
    warnings: list[str] = []

    nodes = 0
    while True:
        nx = max(int(np.ceil(span_x / resolution)) + 1, 2)
        ny = max(int(np.ceil(span_y / resolution)) + 1, 2)
        nodes = nx * ny
        if nodes <= max_nodes or resolution >= max(span_x, span_y):
            break
        resolution *= 1.5

    if abs(resolution - float(resolution_mm)) > 1e-9:
        warnings.append(
            f"模型范围较大，Z-map 分辨率从 {resolution_mm:g} mm 放宽到 {resolution:.3g} mm"
            f"（节点上限 {max_nodes}）"
        )

    xs = np.linspace(x_min, x_max, nx)
    ys = np.linspace(y_min, y_max, ny)
    fill = -np.inf if pick == PICK_TOP else np.inf
    grid = np.full((ny, nx), fill, dtype=np.float64)
    covered = np.zeros((ny, nx), dtype=bool)

    for triangle in mesh.triangles:
        _rasterize_triangle(grid, covered, xs, ys, triangle, pick)

    if not covered.any():  # pragma: no cover - Mesh 已经保证至少一个三角形
        raise ParameterError("模型在 XY 平面上没有任何面积，无法生成 Z-map")

    coverage = float(covered.mean())
    if coverage < 1.0:
        warnings.append(
            f"模型的 XY 投影只覆盖加工范围的 {coverage * 100:.1f}%，"
            "投影之外的位置按最近处的模型高度加工"
        )

    values = np.where(covered, grid, 0.0)
    filled = _fill_nearest(values, covered)
    return (
        HeightField(xs=xs, ys=ys, zs=filled, pick=pick, coverage=coverage),
        tuple(warnings),
    )


def _rasterize_triangle(
    grid: NDArray[np.float64],
    covered: NDArray[np.bool_],
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
    triangle: NDArray[np.float64],
    pick: str,
) -> None:
    """把一个三角形写进它覆盖的那一小块网格。"""

    v0, v1, v2 = triangle[0], triangle[1], triangle[2]
    denominator = (v1[1] - v2[1]) * (v0[0] - v2[0]) + (v2[0] - v1[0]) * (v0[1] - v2[1])
    if abs(denominator) <= 1e-12:  # 竖直三角形（XY 面积为零）不参与 Z-map
        return
    x_low = min(v0[0], v1[0], v2[0])
    x_high = max(v0[0], v1[0], v2[0])
    y_low = min(v0[1], v1[1], v2[1])
    y_high = max(v0[1], v1[1], v2[1])
    i0 = max(int(np.searchsorted(xs, x_low, side="left")) - 1, 0)
    i1 = min(int(np.searchsorted(xs, x_high, side="right")), xs.size)
    j0 = max(int(np.searchsorted(ys, y_low, side="left")) - 1, 0)
    j1 = min(int(np.searchsorted(ys, y_high, side="right")), ys.size)
    if i1 <= i0 or j1 <= j0:
        return

    gx, gy = np.meshgrid(xs[i0:i1], ys[j0:j1])
    weight0 = ((v1[1] - v2[1]) * (gx - v2[0]) + (v2[0] - v1[0]) * (gy - v2[1])) / denominator
    weight1 = ((v2[1] - v0[1]) * (gx - v2[0]) + (v0[0] - v2[0]) * (gy - v2[1])) / denominator
    weight2 = 1.0 - weight0 - weight1
    inside = (weight0 >= -1e-9) & (weight1 >= -1e-9) & (weight2 >= -1e-9)
    if not inside.any():
        return
    height = weight0 * v0[2] + weight1 * v1[2] + weight2 * v2[2]
    block = grid[j0:j1, i0:i1]
    if pick == PICK_TOP:
        np.maximum(block, np.where(inside, height, -np.inf), out=block)
    else:
        np.minimum(block, np.where(inside, height, np.inf), out=block)
    covered[j0:j1, i0:i1] |= inside


def _fill_nearest(values: NDArray[np.float64], covered: NDArray[np.bool_]) -> NDArray[np.float64]:
    """用最近的有效节点值补上空洞（先沿列、再沿行，两遍可分离传播）。"""

    if covered.all():
        return values
    rows = np.arange(values.shape[0])[:, None]
    up = np.maximum.accumulate(np.where(covered, rows, -1), axis=0)
    down = np.minimum.accumulate(np.where(covered, rows, values.shape[0])[::-1], axis=0)[::-1]
    up_valid = up >= 0
    down_valid = down < values.shape[0]
    up_index = np.clip(up, 0, values.shape[0] - 1)
    down_index = np.clip(down, 0, values.shape[0] - 1)
    nearest_row = np.where(
        np.abs(rows - up_index) <= np.abs(down_index - rows), up_index, down_index
    )
    vertical = np.take_along_axis(values, nearest_row, axis=0)

    cols = np.arange(values.shape[1])[None, :]
    left = np.maximum.accumulate(np.where(covered, cols, -1), axis=1)
    right = np.minimum.accumulate(np.where(covered, cols, values.shape[1])[:, ::-1], axis=1)[:, ::-1]
    left_valid = left >= 0
    right_valid = right < values.shape[1]
    left_index = np.clip(left, 0, values.shape[1] - 1)
    right_index = np.clip(right, 0, values.shape[1] - 1)
    nearest_col = np.where(
        np.abs(cols - left_index) <= np.abs(right_index - cols), left_index, right_index
    )
    horizontal = np.take_along_axis(values, nearest_col, axis=1)

    fallback = float(values[covered].mean()) if covered.any() else 0.0
    filled = np.where(
        up_valid | down_valid, vertical, np.where(left_valid | right_valid, horizontal, fallback)
    )
    return np.where(covered, values, filled)


# ------------------------------------------------------------------ 模型库
@dataclass(slots=True)
class StoredModel:
    """一个已经导入、可以被反复引用的模型。"""

    id: str
    name: str
    mesh: Mesh
    created_at: str
    _fields: dict[tuple[float, str], HeightField] = field(default_factory=dict, repr=False)
    _warnings: dict[tuple[float, str], tuple[str, ...]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def triangle_count(self) -> int:
        return self.mesh.triangle_count

    def height_field(self, resolution_mm: float, pick: str) -> HeightField:
        """按（分辨率, 取面方式）缓存 Z-map，避免每次规划都重新光栅化。"""

        key = (round(float(resolution_mm), 6), str(pick))
        with self._lock:
            cached = self._fields.get(key)
            if cached is not None:
                return cached
        # 光栅化放在锁外：两个并发请求最多各算一次，不会互相阻塞。
        built, warnings = build_height_field(
            self.mesh, resolution_mm=key[0], pick=key[1]
        )
        with self._lock:
            self._fields.setdefault(key, built)
            self._warnings.setdefault(key, warnings)
            return self._fields[key]

    def field_warnings(self, resolution_mm: float, pick: str) -> tuple[str, ...]:
        self.height_field(resolution_mm, pick)
        return self._warnings.get((round(float(resolution_mm), 6), str(pick)), ())

    def describe(self) -> dict[str, Any]:
        x_min, x_max, y_min, y_max, z_min, z_max = self.mesh.bounds
        size = self.mesh.size_mm
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "triangle_count": self.triangle_count,
            "bounds_mm": [
                [round(x_min, 4), round(x_max, 4)],
                [round(y_min, 4), round(y_max, 4)],
                [round(z_min, 4), round(z_max, 4)],
            ],
            "size_mm": [round(value, 4) for value in size],
        }

    def mesh_payload(self, max_triangles: int = MAX_DISPLAY_TRIANGLES) -> dict[str, Any]:
        """给三维显示用的三角形坐标；三角形太多时等间隔抽样。"""

        triangles = self.mesh.triangles
        simplified = False
        if triangles.shape[0] > max_triangles:
            step = int(np.ceil(triangles.shape[0] / max_triangles))
            triangles = triangles[::step]
            simplified = True
        display = Mesh(triangles)
        return {
            "id": self.id,
            "name": self.name,
            "triangle_count": self.mesh.triangle_count,
            "display_triangle_count": display.triangle_count,
            "simplified": simplified,
            "positions": display.flatten_positions(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id}


class ModelLibrary:
    """内存里的模型库（进程级，重启即清空）。"""

    def __init__(self, *, max_models: int = MAX_MODELS) -> None:
        self.max_models = int(max_models)
        self._items: dict[str, StoredModel] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def add(self, name: str, data: bytes, *, mesh: Mesh | None = None) -> StoredModel:
        """解析并保存一个模型，返回它的登记信息。"""

        parsed = mesh if mesh is not None else load_stl(data)
        model = StoredModel(
            id=f"m-{uuid.uuid4().hex[:8]}",
            name=(name or "model.stl").strip() or "model.stl",
            mesh=parsed,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        with self._lock:
            while len(self._items) >= max(self.max_models, 1):
                self._items.pop(next(iter(self._items)))
            self._items[model.id] = model
        return model

    def get(self, model_id: str) -> StoredModel:
        with self._lock:
            model = self._items.get(str(model_id))
        if model is None:
            known = ", ".join(self._items) or "<empty>"
            raise ParameterError(
                f"没有这个模型 {model_id!r}（已导入：{known}）；请先通过 POST /api/models 上传"
            )
        return model

    def maybe(self, model_id: str | None) -> StoredModel | None:
        """空 id 表示"不使用模型"，直接返回 None，不报错。"""

        if model_id is None:
            return None
        cleaned = str(model_id).strip()
        return self.get(cleaned) if cleaned else None

    def remove(self, model_id: str) -> bool:
        with self._lock:
            return self._items.pop(str(model_id), None) is not None

    def list(self) -> list[StoredModel]:
        with self._lock:
            return list(self._items.values())

    def describe_all(self) -> list[dict[str, Any]]:
        return [model.describe() for model in self.list()]


def model_library_payload(library: ModelLibrary) -> dict[str, Any]:
    """GET /api/models 的响应体。"""

    models = library.describe_all()
    return {
        "ok": True,
        "models": models,
        "default_id": models[0]["id"] if models else "",
        "max_models": library.max_models,
        "limits": {
            "max_models": library.max_models,
            "max_display_triangles": MAX_DISPLAY_TRIANGLES,
            "max_grid_nodes": MAX_GRID_NODES,
        },
    }


def triangle_soup(points: Iterable[Iterable[float]]) -> Mesh:  # pragma: no cover - 测试辅助
    """由一组点构造网格（每三个点为一片），供脚本与测试使用。"""

    return Mesh(np.asarray(list(points), dtype=np.float64))
