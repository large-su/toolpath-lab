"""刀路策略用到的平面多边形工具。

这里有两个"重活"：

- `scanline_intervals`：把一条直线与多边形求交，按偶奇规则配对成若干"内部区间"。栅格刀路
  就是靠它在一刀之内找出从哪里切到哪里，方形、圆形、U 形、以及将来任何形状都用同一套代码；
- `offset_loops`：多边形向内偏置，并且保留**每层可能出现的多条环**。环切靠它逐圈向内；
  凹形状的细颈被偏置吃掉之后会把形状切成几块，这时必须输出多条互不相连的环。

向内偏置（侵蚀）的做法，四步：

1. **图元**：每条边向内平移一个偏置量得到线段，两端各留一点延长量（见 `_MARGIN_FACTOR`——
   斜接点几乎总落在线段之内，延长只是数值余量）；每个**凹角**补半径等于偏置量的圆弧并离散成弦。
   凸角不需要圆弧——侵蚀里凸角就是尖的，斜接点由两条平移线的交点自然给出；凹角才必须是圆弧。
   为什么用"边 + 凹角圆弧"就够了：区域内部任一点到边界的最近点，要么落在某条边上，
   要么落在某个凹角顶点上，凸角永远不会是最近点（它到两边的距离都不大于到它的距离）。
2. **求交切分**：把每条线段在所有交点处切开，交点坐标两边共用，于是"斜接点""圆弧切进来的
   那一点"都成了共享节点——这一步是能不能接成环的关键。
3. **取等距线**：只留下"在多边形内部、且到边界距离 ≈ 偏置量"的子段。更靠里的是区域内部，
   不是偏置区域的边界；延长出来的部分是无效的，在这里被滤掉。
4. **接环**：把端点并成节点后按"转角最小"接成闭环——偏置曲线穿过这种节点时就是直着过去的。

偏置与重采样本来写在环切策略里（examples/plugins/contour_planner.py 保留了一份单环的旧版），
按当时的约定"需要时再抄进主程序"，现在抄进来了。
"""

from __future__ import annotations

from math import atan2, ceil, cos, pi, sin
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

_EPS = 1e-9
#: 长度小于这个值的线段直接丢掉（mm），避免量化噪声变出一堆零长度图元。
_MIN_PIECE_MM = 1e-9
#: 凹角圆弧离散时允许的最大圆心角（弧度）：弦长再小也不至于把圆弧切得太碎。
_MAX_ARC_STEP_RAD = 0.35
#: 平移线段两端延长的倍数（×偏置量）。延长只是**数值余量**，不是正确性的开关：
#: 斜接点落在平移线段之外的那个凸角，它两侧的边长 L 必然小于需要的延长 d·tan(转角/2)，
#: 于是这一点附近的材料宽度最大只有 2L·sin(内角/2) < 2d——也就是比刀具还窄，早就被整个
#: 侵蚀掉了，那条边根本不该出现在偏置边界上。所以这里给一个小余量就够了。
_MARGIN_FACTOR = 4.0
#: 节点量化的网格（mm）：端点吸附到交点之后坐标已经严格一致，这里只吸收浮点噪声。
_NODE_GRID_MM = 1e-6
#: 直线段的等距线容差（相对偏置量）：直线段本来没有离散误差，容差只吸收浮点噪声；
#: 容差留大了会把斜接点外侧那一小截延长线也当成合法段，接环时就走错路。
_LINE_TOLERANCE_RATIO = 1e-7


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


# ---------------------------------------------------------------- 多边形工具
def inward_normals(polygon: NDArray[np.float64]) -> NDArray[np.float64]:
    """每条边的单位左法向（逆时针多边形时指向内部）。"""

    edge = np.roll(polygon, -1, axis=0) - polygon
    length = np.linalg.norm(edge, axis=1, keepdims=True)
    edge = edge / np.where(length > _EPS, length, 1.0)
    return np.column_stack((-edge[:, 1], edge[:, 0]))


def boundary_edges(
    polygon: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """预先算好各边的起点、方向向量与长度平方：批量测距时反复用得到。"""

    start = np.asarray(polygon, dtype=np.float64)
    edge = np.roll(start, -1, axis=0) - start
    squared = np.maximum(np.sum(edge * edge, axis=1), _EPS)
    return start, edge, squared


def distance_to_edges(
    points: NDArray[np.float64],
    edges: tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]],
) -> NDArray[np.float64]:
    """每个点到多边形各边的最近距离（边已经预先算好）。"""

    start, edge, squared = edges
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)[:, None, :]
    t = np.clip(np.sum((pts - start[None]) * edge[None], axis=2) / squared[None], 0.0, 1.0)
    closest = start[None] + t[..., None] * edge[None]
    return np.linalg.norm(pts - closest, axis=2).min(axis=1)


def distance_to_boundary(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.float64]:
    """每个点到多边形各边的最近距离。"""

    return distance_to_edges(points, boundary_edges(polygon))


def point_in_polygon(
    points: NDArray[np.float64], polygon: NDArray[np.float64]
) -> NDArray[np.bool_]:
    """射线法判断每个点是否在多边形内部。

    正好落在边界上的点结果未定义（两种都可能），调用方不要依赖它。
    """

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    x = pts[:, 0][:, None]
    y = pts[:, 1][:, None]
    x0 = polygon[:, 0][None, :]
    y0 = polygon[:, 1][None, :]
    x1 = np.roll(polygon[:, 0], -1)[None, :]
    y1 = np.roll(polygon[:, 1], -1)[None, :]
    straddles = (y0 > y) != (y1 > y)
    height = np.where(np.abs(y1 - y0) > _EPS, y1 - y0, 1.0)
    crossing_x = x0 + (y - y0) * (x1 - x0) / height
    return (np.sum(straddles & (crossing_x > x), axis=1) % 2) == 1


def on_offset_boundary(
    points: NDArray[np.float64],
    polygon: NDArray[np.float64],
    distance: float,
    *,
    tolerance: float,
) -> NDArray[np.bool_]:
    """点是否落在偏置区域的边界上：在多边形内部，且到边界的距离约等于偏置量。"""

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    inside = point_in_polygon(pts, polygon)
    gaps = np.abs(distance_to_boundary(pts, polygon) - distance)
    return inside & (gaps <= tolerance)


# ---------------------------------------------------------------- 向内偏置
def offset_primitives(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    margin_factor: float = _MARGIN_FACTOR,
) -> list[tuple[NDArray[np.float64], NDArray[np.float64], bool]]:
    """偏置图元：`(起点, 终点, 是不是圆弧的弦)`。

    每条边向内平移一个偏置量得到线段，凸角两端各按 `偏置量 × tan(转角/2)` 延长一点（再截到
    `_MARGIN_FACTOR` 倍）。这个延长只是数值余量：斜接点是相邻两条平移线的交点，只有它落在
    线段之外时才需要延长，而那种角的局部材料必然比刀具还窄、整块会被侵蚀掉（见 `_MARGIN_FACTOR`
    的说明）。按顶点逐个算而不是统一延长一大截，是因为统一延长会让曲线形状凭空多出一堆交点。
    凹角端不需要延长：那里由圆弧接头补上，圆弧的端点正好落在平移线段的端点上。
    """

    count = polygon.shape[0]
    normals = inward_normals(polygon)
    previous_normal = np.roll(normals, 1, axis=0)
    previous_edge = polygon - np.roll(polygon, 1, axis=0)
    next_edge = np.roll(polygon, -1, axis=0) - polygon
    cross = previous_edge[:, 0] * next_edge[:, 1] - previous_edge[:, 1] * next_edge[:, 0]
    dot = np.sum(previous_edge * next_edge, axis=1)
    turn = np.arctan2(cross, dot)  # 带符号的转角：正 = 左转 = 凸角
    limit = margin_factor * distance
    reach = np.where(
        turn > 0.0,
        np.minimum(distance * np.tan(turn / 2.0), limit),
        0.0,
    )

    pieces: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]] = []
    for index in range(count):
        start = polygon[index] + distance * normals[index]
        end = polygon[(index + 1) % count] + distance * normals[index]
        span = end - start
        length = float(np.linalg.norm(span))
        if length <= _MIN_PIECE_MM:
            continue
        direction = span / length
        pieces.append(
            (
                start - float(reach[index]) * direction,
                end + float(reach[(index + 1) % count]) * direction,
                False,
            )
        )

    for index in range(count):
        if turn[index] >= 0.0:
            # 凸角：斜接点由两条平移线段的交点自然给出，不需要圆弧。
            continue
        center = polygon[index]
        n_prev = previous_normal[index]
        n_next = normals[index]
        start_angle = atan2(float(n_prev[1]), float(n_prev[0]))
        end_angle = atan2(float(n_next[1]), float(n_next[0]))
        delta = (end_angle - start_angle + pi) % (2.0 * pi) - pi
        segments = max(
            1,
            int(ceil(abs(delta) * distance / max(chord_mm, 1e-6))),
            int(ceil(abs(delta) / _MAX_ARC_STEP_RAD)),
        )
        previous_point = center + distance * n_prev
        for step in range(1, segments + 1):
            angle = start_angle + delta * step / segments
            point = center + distance * np.array([cos(angle), sin(angle)])
            pieces.append((previous_point, point, True))
            previous_point = point
    return pieces


def snap_to_nodes(
    points: list[NDArray[np.float64]], snap: float
):
    """返回一个函数：把点吸附到最近的给定节点（距离不超过 snap），否则原样返回。

    圆弧是离散成弦的，它的端点与"圆弧和相邻线段的交点"可能差几个 1e-4 mm；不吸附过去，
    这几条链就接不上（数值上差一点点，但接环靠的就是坐标一致）。
    """

    cell = max(snap, _EPS)
    buckets: dict[tuple[int, int], list[NDArray[np.float64]]] = {}
    for point in points:
        key = (int(np.floor(point[0] / cell)), int(np.floor(point[1] / cell)))
        buckets.setdefault(key, []).append(point)

    def snap_point(point: NDArray[np.float64]) -> NDArray[np.float64]:
        key = (int(np.floor(point[0] / cell)), int(np.floor(point[1] / cell)))
        best: NDArray[np.float64] | None = None
        best_distance = snap
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for candidate in buckets.get((key[0] + dx, key[1] + dy), ()):
                    distance = float(np.linalg.norm(candidate - point))
                    if distance <= best_distance:
                        best, best_distance = candidate, distance
        return point if best is None else best

    return snap_point


def split_at_intersections(
    pieces: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]],
    *,
    snap: float = 0.0,
) -> list[tuple[NDArray[np.float64], NDArray[np.float64], bool]]:
    """把所有线段在交点处切开；交点坐标两边共用，因此之后的节点是严格重合的。

    传了 snap 就把端点吸附到最近的交点上（距离不超过 snap）：圆弧离散点与交点之间
    那点零头只有靠这一步才能消掉，否则链会在那里断开。
    """

    count = len(pieces)
    if count < 2:
        return list(pieces)
    starts = np.array([piece[0] for piece in pieces], dtype=np.float64)
    ends = np.array([piece[1] for piece in pieces], dtype=np.float64)
    chords = [piece[2] for piece in pieces]
    spans = ends - starts

    # 每条线段上的切分点：(参数, 坐标)，坐标与配对的那条线段共用。
    cuts: list[list[tuple[float, NDArray[np.float64]]]] = [[] for _ in range(count)]
    nodes: list[NDArray[np.float64]] = []
    first, second = np.meshgrid(np.arange(count), np.arange(count), indexing="ij")
    pairs = first < second
    i_flat = first[pairs]
    j_flat = second[pairs]
    if i_flat.size:
        p = starts[i_flat]
        r = spans[i_flat]
        q = starts[j_flat]
        s = spans[j_flat]
        denominator = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
        usable = np.abs(denominator) > _EPS
        safe = np.where(usable, denominator, 1.0)
        delta = q - p
        t = (delta[:, 0] * s[:, 1] - delta[:, 1] * s[:, 0]) / safe
        u = (delta[:, 0] * r[:, 1] - delta[:, 1] * r[:, 0]) / safe
        crossing = usable & (t > _EPS) & (t < 1.0 - _EPS) & (u > _EPS) & (u < 1.0 - _EPS)
        for i, j, ti, uj in zip(
            i_flat[crossing], j_flat[crossing], t[crossing], u[crossing]
        ):
            point = 0.5 * (
                (starts[i] + ti * spans[i]) + (starts[j] + uj * spans[j])
            )
            cuts[int(i)].append((float(ti), point))
            cuts[int(j)].append((float(uj), point))
            nodes.append(point)

    snap_point = snap_to_nodes(nodes, snap) if snap > 0.0 and nodes else (lambda point: point)

    subsegments: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]] = []
    for index in range(count):
        ordered = sorted(cuts[index], key=lambda item: item[0])
        points: list[NDArray[np.float64]] = [snap_point(starts[index])]
        for parameter, point in ordered:
            if parameter <= _EPS or parameter >= 1.0 - _EPS:
                continue
            if float(np.linalg.norm(point - points[-1])) <= _MIN_PIECE_MM:
                continue
            points.append(point)
        points.append(snap_point(ends[index]))
        for begin, finish in zip(points, points[1:]):
            if float(np.linalg.norm(finish - begin)) > _MIN_PIECE_MM:
                subsegments.append((begin, finish, chords[index]))
    return subsegments


def _valid_subsegments(
    subsegments: list[tuple[NDArray[np.float64], NDArray[np.float64], bool]],
    polygon: NDArray[np.float64],
    distance: float,
    *,
    line_tolerance: float,
    chord_tolerance: float,
) -> list[tuple[NDArray[np.float64], NDArray[np.float64]]]:
    """只留下确实落在偏置区域边界上的子段。

    直线段与圆弧的弦用不同容差：弦的中点天然比半径近一个矢高，容差要盖住它；
    直线段没有这个误差，容差必须很紧，否则斜接点外那一小截延长线也会被当成边界。

    每个子段只查中点就够了：子段是按交点切开的，整段要么都在等距线上、要么都不在；
    圆弧的弦则是中点偏得最多（矢高），中点过了全段就都过。

    曲线形状一次会切出几千个子段，所以这里按块批量判、边向量也只算一次：实测圆形的
    环切从 3.5 s 降到 0.6 s 左右。
    """

    if not subsegments:
        return []
    edges = boundary_edges(polygon)
    starts = np.array([item[0] for item in subsegments], dtype=np.float64)
    ends = np.array([item[1] for item in subsegments], dtype=np.float64)
    tolerances = np.where(
        np.array([item[2] for item in subsegments]),
        chord_tolerance,
        line_tolerance,
    )
    midpoints = 0.5 * (starts + ends)

    keep = np.zeros(len(subsegments), dtype=bool)
    chunk = 2048
    for begin in range(0, len(subsegments), chunk):
        finish = min(begin + chunk, len(subsegments))
        block = midpoints[begin:finish]
        inside = point_in_polygon(block, polygon)
        gaps = np.abs(distance_to_edges(block, edges) - distance)
        keep[begin:finish] = inside & (gaps <= tolerances[begin:finish])

    return [
        (start, end)
        for (start, end, _chord), selected in zip(subsegments, keep.tolist())
        if selected
    ]


def _node_labels(points: NDArray[np.float64], grid: float = _NODE_GRID_MM) -> NDArray[np.int64]:
    """按网格把点归成节点，返回每个点的节点编号。

    交点坐标在两条线段之间是严格共用的，所以这里只是把浮点噪声归到一起；
    用字典而不是两两比较，曲线形状（几百条线段）才不会退化成 O(n²)。
    """

    keys = np.round(points / grid).astype(np.int64)
    mapping: dict[tuple[int, int], int] = {}
    labels = np.empty(points.shape[0], dtype=np.int64)
    for index, key in enumerate(map(tuple, keys.tolist())):
        labels[index] = mapping.setdefault(key, len(mapping))
    return labels


def _chain_loops(
    pieces: list[tuple[NDArray[np.float64], NDArray[np.float64]]],
) -> list[NDArray[np.float64]]:
    """把子段接成闭环。

    一个节点上有多条出路时选"转角最小"的那条——偏置曲线穿过交点时就是直着过去的。
    接不上的链直接丢弃：那说明几何退化（子段落在偏置边界之外，或者比刀具还窄的区域
    被整体侵蚀掉了），这种链本来就不该出现在刀路里。
    """

    if not pieces:
        return []
    starts = np.array([piece[0] for piece in pieces], dtype=np.float64)
    ends = np.array([piece[1] for piece in pieces], dtype=np.float64)
    endpoints = np.vstack([starts, ends])
    labels = _node_labels(endpoints)
    start_labels = labels[: len(pieces)]
    end_labels = labels[len(pieces):]
    representatives = {
        int(label): endpoints[labels == label].mean(axis=0) for label in np.unique(labels)
    }

    outgoing: dict[int, list[int]] = {}
    for index, label in enumerate(start_labels):
        outgoing.setdefault(int(label), []).append(index)

    used = [False] * len(pieces)
    loops: list[NDArray[np.float64]] = []
    for seed in range(len(pieces)):
        if used[seed]:
            continue
        seed_label = int(start_labels[seed])
        points: list[NDArray[np.float64]] = []
        current: int | None = seed
        while current is not None:
            used[current] = True
            points.append(representatives[int(start_labels[current])])
            tail = int(end_labels[current])
            if tail == seed_label and len(points) >= 3:
                loops.append(np.array(points, dtype=np.float64))
                break
            step = ends[current] - starts[current]
            length = float(np.linalg.norm(step))
            current = None
            if length <= _MIN_PIECE_MM:
                continue
            direction = step / length
            best_score = -2.0
            for candidate in outgoing.get(tail, []):
                if used[candidate]:
                    continue
                following = ends[candidate] - starts[candidate]
                following_length = float(np.linalg.norm(following))
                if following_length <= _MIN_PIECE_MM:
                    continue
                score = float(direction @ (following / following_length))
                if score > best_score:
                    best_score, current = score, candidate
    return loops


def _drop_repeats(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """去掉闭合环里连续重复的点。"""

    if points.shape[0] == 0:
        return points
    gaps = np.linalg.norm(np.diff(np.vstack([points, points[:1]]), axis=0), axis=1)
    keep = np.concatenate(([True], gaps[:-1] > _MIN_PIECE_MM))
    return points[keep]


def offset_loops(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    min_area_mm2: float = 0.5,
) -> list[NDArray[np.float64]]:
    """逆时针多边形向内偏置 distance >= 0，返回**所有**环（按面积从大到小）。

    细颈被吃掉时形状会分裂，这里就会给出多条环；一条都不剩时返回空列表。
    只做向内偏置：向外偏置要在凸角补圆弧，是另一套几何，因此负值直接报错。
    """

    if distance < 0.0:
        raise ValueError("offset_loops 只支持向内偏置（distance >= 0）")
    poly = ensure_ccw(polygon)
    if distance <= _EPS:
        return [poly]

    # 凹角的圆弧是离散成弦的，弦中点比半径近一个矢高 d·(1-cos(半步角))；
    # 容差必须盖住它，否则整段圆弧都会被判成"不在这条等距线上"而丢掉。
    step_angle = min(chord_mm / distance, _MAX_ARC_STEP_RAD)
    sagitta = distance * (1.0 - cos(step_angle / 2.0))
    chord_tolerance = max(1e-6, 1e-6 * distance, 1.5 * sagitta)
    line_tolerance = max(1e-9, _LINE_TOLERANCE_RATIO * distance)

    primitives = offset_primitives(poly, distance, chord_mm=chord_mm)
    pieces = _valid_subsegments(
        split_at_intersections(primitives, snap=chord_tolerance),
        poly,
        distance,
        line_tolerance=line_tolerance,
        chord_tolerance=chord_tolerance,
    )

    loops: list[NDArray[np.float64]] = []
    for loop in _chain_loops(pieces):
        loop = _drop_repeats(loop)
        if loop.shape[0] < 3:
            continue
        area = signed_area(loop)
        if area <= 0.0 or area < min_area_mm2:
            continue
        loops.append(loop)
    loops.sort(key=lambda item: -abs(signed_area(item)))
    return loops


def offset_polygon(
    polygon: NDArray[np.float64],
    distance: float,
    *,
    chord_mm: float = 0.5,
    min_area_mm2: float = 0.5,
) -> NDArray[np.float64] | None:
    """只要一条环时的便捷入口：返回面积最大的那条，没有合法环时返回 None。"""

    loops = offset_loops(polygon, distance, chord_mm=chord_mm, min_area_mm2=min_area_mm2)
    return loops[0] if loops else None


def resample_ring(polygon: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """按等弧长重采样一个闭合环（不重复首点）。"""

    ring = np.vstack([polygon, polygon[:1]])
    steps = np.linalg.norm(np.diff(ring, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(steps)))
    total = float(cumulative[-1])
    if total <= _EPS:
        return np.asarray(polygon, dtype=np.float64)
    count = max(3, int(ceil(total / max(step_mm, 1e-6))))
    targets = np.linspace(0.0, total, count, endpoint=False)
    return np.column_stack(
        (
            np.interp(targets, cumulative, ring[:, 0]),
            np.interp(targets, cumulative, ring[:, 1]),
        )
    )
