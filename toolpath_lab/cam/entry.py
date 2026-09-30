"""进刀 / 退刀 / 层间过渡的几何生成（UG/NX「非切削移动」的对应物）。

型腔铣里刀具要解决三段"不切削但要命"的移动：

进刀 :func:`build_entry`
    从材料顶面 ``z_engage`` 下降到本层第一刀的起点。四档方式：

    * ``arc``   —— 先按斜插角降到位（或干脆不降），最后一段**沿刀轨切线方向**
      甩进切入点：切入方向与首刀方向一致，不在已加工面上啃出横向刀痕；
      落点本身贴壁（环切常从拐角起刀）时，整条弧沿切向往前挪几毫米再放下，
      扫完退回开切点——退回那段水平、同高、也校验可行；
    * ``helix`` —— 在可行区内绕着切入点螺旋下降，每圈下降一圈弦长 × tanθ，
      深进刀也不会像垂直插削那样把全刃长压进材料；
    * ``ramp``  —— 一条直线斜坡，坡度正好等于斜插角；
    * ``plunge`` —— 垂直直插（原行为，作为兜底，由调用方直接处理）。

    ``auto`` 按 arc → helix → ramp 依次尝试，前一档**放不下**才换下一档。
    无论哪一档都保证：坡度 ≤ 斜插角、XY 全程落在刀心可行区内、
    末点正好是本层第一刀的起点（这样后面的切削段不需要补连接）。
    平底时 Z 还单调不升；斜/曲面底上由调用方把每个点再抬到「刀轴不过切高度」
    （见 ``pocket_mill._clamp_to_axis``），为绕开凸起时可能回升一小段。

退刀 :func:`build_depart`
    ``retract_method="arc"`` 时从最后切削点沿切线甩出一段圆弧再抬刀，
    圆弧只升 ``L·tanθ``（剩下的由快速抬刀完成）——退刀时刀还在材料里，
    垂直升会在已加工面上留下一道竖直划痕。``lift`` 直接抬刀，这里返回 None。

层间过渡 :func:`build_transition`
    上一层终点到下一层起点的连接。原实现是**两点直线**（坡度不受控：XY 差 2 mm、
    层深 2 mm 就是 45° 直扎）。这里给两档更好的走法，越出可行区就退回上一档，
    最后兜底仍是那条已验证可行的直线：

    * ``ramp``      —— 先在层高上平移 ``run - d``，再按斜插角下降；
    * ``wall_ramp`` —— 沿腔壁等距环绕行下降，绕行长度天然远大于层深，
      坡度最平缓（UG 的「沿工件轮廓斜降」）。

约束的来源是 :mod:`toolpath_lab.cam.pocket_mill` 里的两个既有事实：
层内转移的直线已判定可行（``in_level_transfer``），所以**直线永远是可用的兜底**；
环与壁精修的起点本来就在可行区边界上，因此几何自己（等距环）就是可行的，
只需校验"接上去的那两段"。
"""

from __future__ import annotations

from math import ceil, pi
from typing import Any, Sequence

import numpy as np
from numpy.typing import NDArray

#: 切向圆弧的默认扫角：90° 足够表达"切向"，又不至于绕出去太远。
ARC_SWEEP_RAD = pi / 2
#: 扫角再小就退化成一个点，没有切向意义。
MIN_ARC_SWEEP_RAD = pi / 6
#: 螺旋圈数上限：再深的进刀也不值得生成上万个点，超了就换下一档。
MAX_HELIX_TURNS = 40
#: 进刀半径逐级减半的下限（mm）。
MIN_RADIUS_MM = 0.4
#: 半径最多减几档。
MAX_RADIUS_STEPS = 4
#: 圆弧/螺旋的采样步长下限（mm），再密只会让刀路文件白白膨胀。
MIN_ARC_STEP_MM = 0.2
#: 斜率判断的数值容差。
_TOL = 1e-9

#: 进刀方式 → 刀路标签里的中文名。标签统一带「下刀」，
#: 统计与既有测试正是按这两个字识别"从上方进入材料"的段。
ENTRY_LABELS = {"arc": "切向圆弧", "helix": "螺旋", "ramp": "斜插", "plunge": "垂直"}


class Feasible:
    """刀心可行区上的折线查询。

    与 :func:`toolpath_lab.cam.common.in_level_transfer` 同一套栅格口径：点落在格
    ``[x0+i·s, x0+(i+1)·s)`` 就取 ``mask[i, j]``；判折线时按**半格步长**采样中间点。
    首末两点豁免——它们是刀路自身的端点，格化误差不该把端点判成出界（与层内
    转移判据一致，那里豁免的是同一件事）。
    """

    __slots__ = ("mask", "x0", "y0", "cell", "shape")

    def __init__(self, region: Any, mask: NDArray[np.bool_]) -> None:
        self.mask = mask
        self.x0 = float(region.bounds[0])
        self.y0 = float(region.bounds[1])
        self.cell = max(float(region.cell_mm), 1e-6)
        self.shape = mask.shape

    def contains(self, xy: Sequence[float] | NDArray[np.float64]) -> bool:
        point = np.asarray(xy, dtype=np.float64).reshape(-1)[:2]
        i = int(np.floor((point[0] - self.x0) / self.cell))
        j = int(np.floor((point[1] - self.y0) / self.cell))
        if i < 0 or j < 0 or i >= self.shape[0] or j >= self.shape[1]:
            return False
        return bool(self.mask[i, j])

    def path_ok(self, points_xy: NDArray[np.float64]) -> bool:
        """中间采样点全部落在掩码内（首末两点豁免）。"""

        return self._scan(points_xy, skip_ends=True)

    def all_inside(self, points_xy: NDArray[np.float64]) -> bool:
        """每个点都要落在掩码内——闭合圆环用：首末是同一个点，豁免会漏掉它。"""

        return self._scan(points_xy, skip_ends=False)

    def _scan(self, points_xy: NDArray[np.float64], *, skip_ends: bool) -> bool:
        pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        count = int(pts.shape[0])
        if count == 0:
            return True
        if count == 1:
            return self.contains(pts[0])
        samples: list[NDArray[np.float64]] = []
        step = 0.5 * self.cell
        last_segment = count - 2
        for k in range(count - 1):
            a, b = pts[k], pts[k + 1]
            length = float(np.hypot(b[0] - a[0], b[1] - a[1]))
            divisions = max(1, int(ceil(length / step)))
            for m in range(divisions + 1):
                if skip_ends and ((k == 0 and m == 0)
                                  or (k == last_segment and m == divisions)):
                    continue
                samples.append(a + (b - a) * (m / divisions))
        if not samples:
            return True
        probe = np.asarray(samples, dtype=np.float64)
        rows = np.floor((probe[:, 0] - self.x0) / self.cell).astype(np.intp)
        cols = np.floor((probe[:, 1] - self.y0) / self.cell).astype(np.intp)
        inside = (rows >= 0) & (rows < self.shape[0]) & (cols >= 0) & (cols < self.shape[1])
        if not bool(inside.all()):
            return False
        return bool(self.mask[rows, cols].all())


# ---------------------------------------------------------------------- 基础几何
def _unit(vector: Sequence[float] | None) -> NDArray[np.float64] | None:
    if vector is None:
        return None
    v = np.asarray(vector, dtype=np.float64).reshape(-1)[:2]
    length = float(np.hypot(v[0], v[1]))
    if length <= _TOL:
        return None
    return v / length


def _perp(v: NDArray[np.float64]) -> NDArray[np.float64]:
    """逆时针旋转 90°。"""

    return np.array([-v[1], v[0]], dtype=np.float64)


def _rotate(v: NDArray[np.float64], radians: float) -> NDArray[np.float64]:
    cosine, sine = np.cos(radians), np.sin(radians)
    return np.array([v[0] * cosine - v[1] * sine,
                     v[0] * sine + v[1] * cosine], dtype=np.float64)


def _radii(radius_mm: float) -> list[float]:
    """进刀半径候选：先用给定值，放不下就逐级减半（最多 4 档）。"""

    radius = max(float(radius_mm), MIN_RADIUS_MM)
    values: list[float] = []
    for _ in range(MAX_RADIUS_STEPS):
        if radius < MIN_RADIUS_MM - _TOL:
            break
        values.append(float(radius))
        radius *= 0.5
    return values


def _approach_directions(tangent: NDArray[np.float64] | None) -> list[NDArray[np.float64]]:
    """进刀/斜降的**行进方向**候选。

    第一个永远是刀轨切向：从切入点背后沿切向斜插进来，落地即可直接开切，
    不需要额外的拐弯。之后是两侧法向与斜向，最后兜底八个罗盘方向。
    """

    if tangent is None:
        base = [np.array([1.0, 0.0]), np.array([0.0, 1.0]),
                np.array([-1.0, 0.0]), np.array([0.0, -1.0])]
    else:
        t = tangent
        n = _perp(t)
        base = [t, n, -n, -t, _rotate(t, pi / 4), _rotate(t, -pi / 4),
                _rotate(n, pi / 4), _rotate(n, -pi / 4)]
    return [np.asarray(v, dtype=np.float64) for v in base]


def _arc_points(center: NDArray[np.float64], radius: float, theta_start: float,
                theta_end: float, step_mm: float) -> NDArray[np.float64]:
    """按弧长采样一段圆弧（含首末两点），保证弦高不超过半格。"""

    sweep = abs(theta_end - theta_start)
    arc_length = radius * sweep
    divisions = max(3, int(ceil(arc_length / max(step_mm, MIN_ARC_STEP_MM))))
    thetas = np.linspace(theta_start, theta_end, divisions + 1)
    return np.column_stack((center[0] + radius * np.cos(thetas),
                            center[1] + radius * np.sin(thetas)))


def _circle_points(center: NDArray[np.float64], radius: float,
                   step_mm: float) -> NDArray[np.float64]:
    """闭合圆周采样（首末同点，用 :meth:`Feasible.all_inside` 判定）。"""

    circumference = 2.0 * pi * radius
    divisions = max(16, int(ceil(circumference / max(step_mm, MIN_ARC_STEP_MM))))
    thetas = np.linspace(0.0, 2.0 * pi, divisions, endpoint=False)
    return np.column_stack((center[0] + radius * np.cos(thetas),
                            center[1] + radius * np.sin(thetas)))


def _chord_length(points_xy: NDArray[np.float64]) -> float:
    """折线总**弦**长。

    坡度限制必须按弦长算，不能按理论弧长：圆弧离散成折线后，每小段的实际
    水平距离是弦（比弧短一丁点），按弧长分配高差会让每个小段都略微超标
    （实测 20° 时超标 9e-5，机床照单执行就是"比设定更陡"）。
    """

    pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    if pts.shape[0] < 2:
        return 0.0
    return float(np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1])).sum())


def _stack(xy: NDArray[np.float64], z_top: float, z_bottom: float) -> NDArray[np.float64]:
    """XY 折线 → 点列，Z 从 ``z_top`` 降到 ``z_bottom``（首末点取到端值）。

    高差按**累计弦长**分配，不是按点的序号：等距采样的圆弧/螺旋两者一样，
    但腔壁等距环只有拐角，边长从几毫米到几十毫米不等——按序号均分，短边那
    一小段会吃掉和长边一样的高差，坡度直接超标。按弦长分配则每段坡度
    恒等于 ``总高差 / 总长``。
    """

    pts = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    count = int(pts.shape[0])
    if count < 2:
        return np.column_stack((pts, np.full(count, float(z_top))))
    steps = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
    total = float(steps.sum())
    if total <= _TOL:
        fractions = np.linspace(0.0, 1.0, count)
    else:
        fractions = np.concatenate(([0.0], np.cumsum(steps))) / total
    zs = float(z_top) + (float(z_bottom) - float(z_top)) * fractions
    return np.column_stack((pts, zs))


# -------------------------------------------------------------------------- 进刀
def _ramp_entry(feasible: Feasible, target: NDArray[np.float64], z_engage: float,
                tangent: NDArray[np.float64] | None, angle_rad: float
                ) -> tuple[NDArray[np.float64], NDArray[np.float64]] | None:
    """直线斜插：一条坡度正好等于斜插角的直线落到切入点。"""

    depth = z_engage - float(target[2])
    if depth <= _TOL:
        return None
    run = depth / np.tan(angle_rad)
    if run < 0.5 * feasible.cell:
        # 水平距离比半格还短：斜坡退化成垂直，交给兜底的直插。
        return None
    for direction in _approach_directions(tangent):
        start = target[:2] - direction * run
        if not feasible.contains(start):
            continue
        if not feasible.path_ok(np.array([start, target[:2]])):
            continue
        points = np.array([[start[0], start[1], z_engage],
                           [target[0], target[1], target[2]]], dtype=np.float64)
        return start, points
    return None


def _anchor_shifts(cell: float) -> tuple[float, ...]:
    """圆弧落点沿切向往腔内挪的候选距离（mm）：0 = 就地放弧。

    环切的第一刀经常起在**拐角**上：圆心一放，圆就伸到腔壁外面去了，整档圆弧
    进刀在拐角处必然放不下。沿切向（本来就要走的方向）挪几毫米再把弧放下来，
    扫完退回真正开切的点——退回那一段在目标高度上、水平、同样校验可行，
    切向与深度都没变，只是多走几毫米。
    """

    return tuple(0.0 if k == 0 else k * cell for k in (0, 4, 8, 12, 16, 24, 32))


def _arc_entry(feasible: Feasible, target: NDArray[np.float64], z_engage: float,
               tangent: NDArray[np.float64] | None, angle_rad: float,
               radius_mm: float, step_mm: float) -> tuple[NDArray[np.float64],
                                                          NDArray[np.float64]] | None:
    """切向圆弧进刀：末段沿刀轨切线方向进入切入点。

    圆弧本身只能吃掉 ``L·tanθ`` 的高差（坡度限制），剩余高差由**前缀斜插**完成；
    一次 90° 弧不够就放长到 180°/270°/整圈（绕着甩，落点切向不变），
    还不够才换下一档半径；落点贴壁时先沿切向挪开（见 :func:`_anchor_shifts`）。
    """

    t = _unit(tangent)
    if t is None:
        return None  # 没有切向就没有"切向圆弧"
    depth = z_engage - float(target[2])
    normal = _perp(t)
    step = max(0.5 * feasible.cell, MIN_ARC_STEP_MM)
    for shift in _anchor_shifts(feasible.cell):
        anchor = target[:2] + shift * t
        if shift > 0.0 and not feasible.contains(anchor):
            continue
        for radius in _radii(radius_mm):
            for side in (1.0, -1.0):
                nn = normal * side
                center = anchor + radius * nn
                # 圆心在 ``anchor + r·n``：从 ``theta_end`` 逆时针扫回来，末点切向正好是 t。
                theta_end = float(np.arctan2(-nn[1], -nn[0]))
                for sweep in (ARC_SWEEP_RAD, pi, 1.5 * pi, 2.0 * pi):
                    if sweep < MIN_ARC_SWEEP_RAD:
                        continue
                    theta_start = theta_end - sweep
                    arc = _arc_points(center, radius, theta_start, theta_end, step)
                    # 高差按**弦长**封顶：离散后每个小段的实际坡度 = 高差 / 弦长，
                    # 用弧长算会让每一小段都比设定角度更陡一点（机床照单执行）。
                    arc_top = float(target[2]) + min(max(depth, 0.0),
                                                     _chord_length(arc)
                                                     * np.tan(angle_rad))
                    start = arc[0]
                    if not _same_xy(start, anchor) and not feasible.contains(start):
                        continue
                    if not feasible.path_ok(arc):
                        continue
                    landing = None
                    if not _same_xy(anchor, target[:2]):
                        landing = np.array([anchor, target[:2]])
                        if not feasible.path_ok(landing):
                            continue
                    prefix_run = (z_engage - arc_top) / np.tan(angle_rad)
                    if prefix_run <= 0.5 * feasible.cell:
                        # 弧自己吃得下全部高差：定位到弧顶直接开扫。
                        points = _stack(arc, z_engage, float(target[2]))
                    else:
                        # 前缀斜插：从 ``start`` 往后退 ``prefix_run``，降到弧顶高度。
                        # 首选方向是**弧在起点自己的切向**——斜插接进圆弧没有拐角，
                        # 切向被切走的时候再退回常规的切向/法向/罗盘方向。
                        entry_dir = np.array([-np.sin(theta_start), np.cos(theta_start)],
                                             dtype=np.float64)
                        points = None
                        for direction in [entry_dir, *_approach_directions(t)]:
                            begin = start - direction * prefix_run
                            if not feasible.contains(begin):
                                continue
                            if not feasible.path_ok(np.array([begin, start])):
                                continue
                            prefix = np.array(
                                [[begin[0], begin[1], z_engage],
                                 [start[0], start[1], arc_top]], dtype=np.float64)
                            body = _stack(arc, arc_top, float(target[2]))
                            # body[0] 就是 (start, arc_top)，与 prefix 的末点重复，丢掉。
                            points = np.vstack([prefix, body[1:]])
                            start = begin
                            break
                        if points is None:
                            continue  # 这个锚点放不下前缀，换下一档
                    if landing is not None:
                        points = np.vstack([points,
                                            [target[0], target[1], target[2]]])
                    return start, points
    return None


def _helix_entry(feasible: Feasible, target: NDArray[np.float64], z_engage: float,
                 angle_rad: float, radius_mm: float,
                 step_mm: float) -> tuple[NDArray[np.float64],
                                          NDArray[np.float64]] | None:
    """螺旋下降：绕着切入点转圈往下走，每圈吃掉一圈弦长 × tanθ。

    圆心优先取切入点本身，放不下就往八个方向挪 1~2 个半径。转满后刀停在圆上，
    再沿半径收进真正的开切点（那一段在目标高度上、水平、且全程校验可行）——
    圆心与落点重合时 ``atan2(0, 0)`` 是无定义的，所以**从不**从落点起绕。
    """

    depth = z_engage - float(target[2])
    if depth <= _TOL:
        return None
    for radius in _radii(radius_mm):
        steps_per_turn = max(12, int(ceil(2.0 * pi * radius / step_mm)))
        # 每圈能吃掉的深度按弦长算（见 _chord_length）：离散后的每个小段才是
        # 机床真正执行的坡度。
        chord_per_turn = steps_per_turn * 2.0 * radius * np.sin(pi / steps_per_turn)
        per_turn = chord_per_turn * np.tan(angle_rad)
        if per_turn <= _TOL:
            continue
        turns = int(ceil(depth / per_turn - _TOL))
        if turns < 1 or turns > MAX_HELIX_TURNS:
            continue
        for center in _helix_centers(target[:2], radius):
            circle = _circle_points(center, radius, step_mm)
            if not feasible.all_inside(circle):
                continue
            total = steps_per_turn * turns
            thetas = 2.0 * pi * np.arange(total + 1) / steps_per_turn
            xy = np.column_stack((center[0] + radius * np.cos(thetas),
                                  center[1] + radius * np.sin(thetas)))
            start = xy[0]
            if not feasible.path_ok(np.array([start, target[:2]])):
                continue
            points = _stack(xy, z_engage, float(target[2]))
            if not _same_xy(xy[-1], target[:2]):
                points = np.vstack([points, [target[0], target[1], target[2]]])
            return start, points
    return None


def _helix_centers(target: NDArray[np.float64], radius: float) -> list[NDArray[np.float64]]:
    """螺旋圆心候选：先原地，再向八个方向各挪 1、2 个半径。"""

    centers = [np.asarray(target, dtype=np.float64)]
    for angle in np.linspace(0.0, 2.0 * pi, 8, endpoint=False):
        direction = np.array([np.cos(angle), np.sin(angle)], dtype=np.float64)
        centers.append(np.asarray(target, dtype=np.float64) + direction * radius)
        centers.append(np.asarray(target, dtype=np.float64) + direction * 2.0 * radius)
    return centers


def _same_xy(a: NDArray[np.float64], b: NDArray[np.float64]) -> bool:
    return bool(abs(a[0] - b[0]) <= _TOL and abs(a[1] - b[1]) <= _TOL)


def build_entry(feasible: Feasible, target: Sequence[float], *,
                z_engage: float, tangent: Sequence[float] | None, method: str,
                angle_rad: float, radius_mm: float,
                ) -> tuple[NDArray[np.float64], NDArray[np.float64], str] | None:
    """生成一段进刀，返回 ``(定位 XY, 点列, 实际用的方式)``。

    ``点列[0]`` 恒等于 ``(定位 XY, z_engage)``、末点恒等于 ``target``：调用方先
    ``rapid_to_safe`` 把刀放到定位点上方并降到 ``z_engage``，再把整段点列作为
    **一段下刀进给**的切削段写入。该方式在当前区域放不下时返回 ``None``，
    调用方降级（``auto`` 换下一档，手动档报警后落回垂直直插）。
    """

    destination = np.asarray(target, dtype=np.float64).reshape(3)
    if method == "plunge":
        return None  # 明确要垂直直插：几何不在这里生成，调用方走原路
    # 进刀只会往下走：材料顶面比目标还低（调用方算错、或这一层已经切过）时
    # 按目标高度算，否则点列会反过来"往上走"。
    engage = max(float(z_engage), float(destination[2]))
    builders = {"arc": _arc_entry, "helix": _helix_entry, "ramp": _ramp_entry}
    order = [method] if method in builders else ["arc", "helix", "ramp"]
    step = max(0.5 * feasible.cell, MIN_ARC_STEP_MM)
    for name in order:
        if name == "arc":
            built = _arc_entry(feasible, destination, engage, tangent, angle_rad,
                               radius_mm, step)
        elif name == "helix":
            built = _helix_entry(feasible, destination, engage, angle_rad,
                                 radius_mm, step)
        else:
            built = _ramp_entry(feasible, destination, engage, tangent, angle_rad)
        if built is not None:
            start, points = built
            return start, points, name
    return None


# -------------------------------------------------------------------------- 退刀
def build_depart(feasible: Feasible, end_point: Sequence[float],
                 tangent: Sequence[float] | None, *, angle_rad: float,
                 radius_mm: float) -> NDArray[np.float64] | None:
    """切向退刀：从末点沿切线甩出一段圆弧并同步抬升，返回点列（含末点本身）。

    圆弧只抬 ``L·tanθ``，剩下的高差交给紧随其后的快速抬刀——退刀段的重点是
    **离开方向与刀轨切向一致**，而不是抬多高。放不下（甩出去就出界）时返回
    ``None``，调用方保持原来的"原地抬到安全面"。
    """

    t = _unit(tangent)
    if t is None:
        return None
    point = np.asarray(end_point, dtype=np.float64).reshape(3)
    normal = _perp(t)
    step = max(0.5 * feasible.cell, MIN_ARC_STEP_MM)
    for radius in _radii(radius_mm):
        for side in (1.0, -1.0):
            nn = normal * side
            center = point[:2] + radius * nn
            theta0 = float(np.arctan2(-nn[1], -nn[0]))
            for sweep in (ARC_SWEEP_RAD, pi):
                arc = _arc_points(center, radius, theta0, theta0 + sweep, step)
                top = float(point[2]) + _chord_length(arc) * np.tan(angle_rad)
                if not feasible.contains(arc[-1]):
                    continue
                if not feasible.path_ok(arc):
                    continue
                return _stack(arc, float(point[2]), top)
    return None


# -------------------------------------------------------------------- 层间过渡
def build_transition(feasible: Feasible, previous: Sequence[float],
                     target: Sequence[float], *, method: str, angle_rad: float,
                     wall_ring: NDArray[np.float64] | None = None
                     ) -> tuple[str, NDArray[np.float64]] | None:
    """上一层终点 → 下一层起点的连接点列，返回 ``(标签, 点列)``。

    ``None`` 表示这一档做不出来（或没有高差），调用方退回**两点直线**——那条直线
    已经由 ``in_level_transfer`` 判定可行，是最保守的兜底。任何新增点都在同一份
    可行区上校验过：层内平移不许穿岛、不许贴到腔壁外面去。
    """

    previous = np.asarray(previous, dtype=np.float64).reshape(3)
    target = np.asarray(target, dtype=np.float64).reshape(3)
    depth = float(previous[2] - target[2])
    if depth <= _TOL:
        return None  # 没有降层（同层转移），保持直线
    if method == "wall_ramp":
        built = _wall_transition(feasible, previous, target, depth, angle_rad, wall_ring)
        if built is not None:
            return "层内转移（沿壁斜降）", built
        # 沿壁放不下（没有环、或接不上去）→ 继续试受控斜降
        method = "ramp"
    if method == "ramp":
        built = _ramp_transition(feasible, previous, target, depth, angle_rad)
        if built is not None:
            return "层内转移（受控斜降）", built
    return None


def _ramp_transition(feasible: Feasible, previous: NDArray[np.float64],
                     target: NDArray[np.float64], depth: float,
                     angle_rad: float) -> NDArray[np.float64] | None:
    """受控斜降：先在层高上平移到 ``run`` 之外，再按斜插角直线降层。

    斜降段的水平投影恒为 ``run = depth / tanθ``，坡度正好等于斜插角；
    平移段落在**上一层的高度**上（那一层的表面刚刚被切过，平移不会啃到新料）。
    """

    run = depth / np.tan(angle_rad)
    straight = float(np.hypot(target[0] - previous[0], target[1] - previous[1]))
    if straight + 0.5 * feasible.cell >= run:
        return None  # 直线本来就够平缓
    forward = _unit(target[:2] - previous[:2])
    candidates: list[NDArray[np.float64]] = []
    if forward is not None:
        candidates.append(forward)
        candidates.append(_perp(forward))
        candidates.append(-_perp(forward))
        candidates.append(-forward)
    candidates.extend(_approach_directions(None))
    for direction in candidates:
        begin = target[:2] - direction * run
        if not feasible.contains(begin):
            continue
        if not feasible.path_ok(np.array([previous[:2], begin])):
            continue
        if not feasible.path_ok(np.array([begin, target[:2]])):
            continue
        return np.array([[previous[0], previous[1], previous[2]],
                         [begin[0], begin[1], previous[2]],
                         [target[0], target[1], target[2]]], dtype=np.float64)
    return None


def _wall_transition(feasible: Feasible, previous: NDArray[np.float64],
                     target: NDArray[np.float64], depth: float, angle_rad: float,
                     wall_ring: NDArray[np.float64] | None) -> NDArray[np.float64] | None:
    """沿壁斜降：沿腔壁等距环绕行下降（UG 的「沿工件轮廓斜降」）。

    环是调用方按 ``刀半径 + 余量`` 做出来的等距轮廓，贴着可行区的边界
    （壁精修那一刀走的就是它），所以环上的移动本身不查掩码——**接上去的两段**要查：
    ``previous`` → 环上最近点（层高平移）、环上另一点 → ``target``（目标高度平移）。
    两端接不上（中间隔着岛、凹角）就退回受控斜降。

    绕行长度 ``L`` 决定坡度：``L < run`` 时多绕整圈补够。绕行一圈就是几十毫米，
    比在腔中间硬拉一条 38 mm 的斜坡现实得多——这正是这一档存在的理由。
    """

    if wall_ring is None or wall_ring.shape[0] < 3:
        return None
    ring = np.asarray(wall_ring, dtype=np.float64).reshape(-1, 2)
    if not _same_xy(ring[0], ring[-1]):
        ring = np.vstack([ring, ring[:1]])
    count = ring.shape[0] - 1
    if count < 3:
        return None
    start_index = int(np.argmin(np.hypot(ring[:, 0] - previous[0],
                                         ring[:, 1] - previous[1])))
    end_index = int(np.argmin(np.hypot(ring[:, 0] - target[0],
                                       ring[:, 1] - target[1])))
    if start_index == end_index or (start_index % count) == (end_index % count):
        return None  # 两个投影落在同一点：绕了也白绕，退回斜降
    lengths = np.hypot(np.diff(ring[:, 0]), np.diff(ring[:, 1]))[:count]
    forward = int((end_index - start_index) % count)
    forward_length = float(sum(lengths[index % count] for index in range(forward)))
    step = 1 if forward_length <= 0.5 * float(lengths.sum()) else -1
    indices = _ring_indices(start_index % count, end_index % count, count, step)
    arc = ring[indices]
    arc_length = float(np.hypot(np.diff(arc[:, 0]), np.diff(arc[:, 1])).sum())
    if arc_length <= _TOL:
        return None
    # 两端的平移段：都在各自的层高上，必须落在可行区内
    if not feasible.path_ok(np.array([previous[:2], arc[0]])):
        return None
    if not feasible.path_ok(np.array([arc[-1], target[:2]])):
        return None
    run = depth / np.tan(angle_rad)
    loops = int(ceil(run / arc_length - _TOL))
    if loops > 0:
        # 需要的水平距离比这段弧还长：整圈补上，Z 全程线性下降
        arc = np.vstack([arc[:1], np.vstack([ring[:count]] * loops), arc[1:]])
    points = np.vstack([
        [previous[0], previous[1], previous[2]],
        _stack(arc, previous[2], float(target[2])),
        [target[0], target[1], target[2]],
    ])
    return points


def _ring_indices(start_index: int, end_index: int, count: int, step: int) -> list[int]:
    """沿环从 ``start_index`` 走到 ``end_index``（含两端）的下标序列。"""

    indices = [start_index]
    current = start_index
    guard = 0
    while current != end_index and guard <= count:
        current = (current + step) % count
        indices.append(current)
        guard += 1
    return indices


__all__ = [
    "ARC_SWEEP_RAD",
    "ENTRY_LABELS",
    "Feasible",
    "build_depart",
    "build_entry",
    "build_transition",
]
