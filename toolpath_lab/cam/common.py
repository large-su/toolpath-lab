"""CAM 规划器的共用设施：运动段构造、深度分层、入刀/退刀。

刀路模型沿用基座的 :class:`~toolpath_lab.core.path.Move` / :class:`~toolpath_lab.core.path.Toolpath`，
因此播放、导出、统计、切削仿真全部可以直接复用，不需要为 CAM 再写一套。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Any, Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.tool import Tool

#: 层间连接时高于毛坯顶面的附加抬刀量（mm），避免擦到已加工面。
LINK_CLEARANCE_MM = 0.5
#: 螺旋/斜线下刀的最大半径，避免小刀具时半径过大。
MAX_RAMP_RADIUS_MM = 0.35
#: 兼容垫片：旧参数 ``stepover_mm`` 若仍传入，按这个比例反推 ratio。
#: 0.5 表示“绝对 5 mm 相当于 D10 的 50%”。不在 parameters 里的 mm 值会被忽略。
_STEPOVER_MM_FALLBACK_RATIO = 0.5
#: 层高键的归一精度（小数位）：层网格与同层去重登记簿共用它，吸收网格步进的
#: 浮点误差（1e-14）与网格面平面拟合噪声（实测 ±1e-7），避免同一物理高度出现
#: 幽灵层/登记对不上。层高只需 μm 级精度，1 nm 归一不损失加工语义。
LEVEL_KEY_DECIMALS = 6


def level_key(level_z: float) -> float:
    """层高按键：按 :data:`LEVEL_KEY_DECIMALS` 位小数归一，同一物理高度恒等。"""

    return round(float(level_z), LEVEL_KEY_DECIMALS)


def _resolve_stepover(parameters: Mapping[str, Any],
                      tool_diameter_mm: float) -> tuple[float, str]:
    """从 parameters 字典里解出步距（mm）与来源标签。"""

    ratio = parameters.get("stepover_ratio")
    if ratio is not None and float(ratio) > 0:
        clamped = float(np.clip(float(ratio), 0.05, 0.95))
        return float(clamped * tool_diameter_mm), "ratio"
    legacy_mm = parameters.get("stepover_mm")
    if legacy_mm is not None and float(legacy_mm) > 0:
        return float(legacy_mm), "legacy_mm"
    # 两个都没给，按 50% 兜底
    return float(_STEPOVER_MM_FALLBACK_RATIO * tool_diameter_mm), "default"


@dataclass(slots=True)
class MillingContext:
    """一次铣削刀路生成需要的全部输入。"""

    tool: Tool
    top_z: float
    floor_z: float
    parameters: Mapping[str, Any]
    #: 刀具中心可以走到的区域掩码与几何（由 :mod:`toolpath_lab.cam.boundary` 提供）
    region: Any = None
    warnings: list[str] = field(default_factory=list)

    # -- 常用参数 ----------------------------------------------------------
    @property
    def feed(self) -> float:
        return float(self.parameters["feed_mm_per_min"])

    @property
    def plunge_feed(self) -> float:
        return float(self.parameters["plunge_feed_mm_per_min"])

    @property
    def rapid_feed(self) -> float:
        return float(self.parameters["rapid_feed_mm_per_min"])

    @property
    def spindle_rpm(self) -> float:
        return float(self.parameters["spindle_rpm"])

    @property
    def stepover(self) -> float:
        """原始步距（mm）。已按 ``stepover_ratio`` 与直径换算；保留同名以便旧调用方使用。"""

        value, _ = _resolve_stepover(self.parameters, self.tool.diameter_mm)
        return value

    @property
    def effective_stepover(self) -> float:
        """实际下刀用的步距（mm），由 ``stepover_ratio`` 推出。

        比例参数天然不会超过直径，不存在“两条刀轨之间漏切”的问题；
        若外部仍传旧 ``stepover_mm``，原样返回并在 warn 里给出 deprecation 提示。
        """

        value, source = _resolve_stepover(self.parameters, self.tool.diameter_mm)
        if source == "legacy_mm":
            self.warn(
                "参数 ``stepover_mm`` 已废弃，请改用 ``stepover_ratio``（刀具直径比例）"
            )
        return value

    @property
    def cut_depth(self) -> float:
        return float(self.parameters["cut_depth_mm"])

    @property
    def stock_allowance(self) -> float:
        return float(self.parameters["stock_allowance_mm"])

    @property
    def finish_allowance(self) -> float:
        return float(self.parameters["finish_allowance_mm"])

    @property
    def safe_z(self) -> float:
        return float(self.top_z) + float(self.parameters["safe_height_mm"])

    @property
    def clearance(self) -> float:
        return float(self.parameters["clearance_mm"])

    # -- 进退刀（型腔铣读这组；老工程没有这些键时按默认值走） ----------------
    @property
    def entry_method(self) -> str:
        return str(self.parameters.get("entry_method", "auto"))

    @property
    def retract_method(self) -> str:
        return str(self.parameters.get("retract_method", "lift"))

    @property
    def level_transition(self) -> str:
        return str(self.parameters.get("level_transition", "ramp"))

    @property
    def ramp_angle_rad(self) -> float:
        """进刀与层间斜降的最大坡度（弧度）。"""

        degrees = float(self.parameters.get("ramp_angle_deg", 3.0))
        return float(np.radians(np.clip(degrees, 0.5, 45.0)))

    @property
    def entry_radius(self) -> float:
        """进刀圆弧/螺旋半径（mm）；0 表示按刀具直径自动取。"""

        value = float(self.parameters.get("entry_radius_mm", 0.0) or 0.0)
        return value if value > 0 else 0.25 * self.tool.diameter_mm

    @property
    def tool_radius(self) -> float:
        return self.tool.radius_mm

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def depth_levels(top_z: float, floor_z: float, cut_depth: float,
                 *, finish_allowance: float = 0.0) -> list[float]:
    """按每层切深把总深度分成若干层，返回每层的目标 Z（从高到低）。

    最后一层永远落在目标面上，因此底面余量由 ``finish_allowance`` 决定：
    留量时最后一层停在 ``floor_z + finish_allowance``。
    """

    depth = float(top_z) - float(floor_z)
    if depth <= 1e-9:
        return []
    step = max(float(cut_depth), 1e-6)
    target = float(floor_z) + max(0.0, float(finish_allowance))
    total = float(top_z) - target
    if total <= 1e-9:
        return []
    count = int(ceil(total / step - 1e-9))
    count = max(1, count)
    levels = [float(top_z) - index * total / count for index in range(1, count + 1)]
    # 层底夹到目标面，避免浮点误差
    levels[-1] = target
    return levels


def level_passes_per_depth(depth: float, cut_depth: float) -> int:
    return max(1, int(ceil(max(depth, 0.0) / max(cut_depth, 1e-6) - 1e-9)))


def stepped_levels(top_z: float, floor_targets: Sequence[float], cut_depth: float) -> list[float]:
    """多区域共用的层高网格：从 ``top_z`` 按每层切深递减，并把每个区域各自的
    加工底（``floor_targets``）并入网格作为该区域的末层，返回从高到低的全部层 Z。

    与 :func:`depth_levels`（单区域把总深**均分**）不同，这里层高是**绝对网格**，
    与区域个数、各自深度无关——同一高度的层在所有区域间天然对齐，"层优先"因此
    能把多个区域在该高度的加工安排在一起（UG 的 Level First）；层高不随切削顺序
    改变，层优先/深度优先只是遍历顺序不同，切下来的几何完全一致。
    """

    floors = [float(value) for value in floor_targets]
    if not floors:
        return []
    top = float(top_z)
    step = max(float(cut_depth), 1e-6)
    lowest = min(floors)
    grid: list[float] = []
    z = top - step
    while z > lowest + 1e-9:
        grid.append(z)
        z -= step
    # 按 6 位小数去重（1 nm）：网格步进的浮点误差是 1e-14 量级，而网格零件的
    # 面平面拟合还会把 floor_z 噪声到 ±1e-7（实测 40.00000015 对 40.0）。9 位
    # 小数吸收不了后者，同一物理高度会同时留下两个幽灵层，各自切一遍——
    # 这正是"同一层刀轨重叠"的一种来源。层高本身只需 μm 级精度，1 nm 归一
    # 不损失任何加工语义，也与 :class:`LevelCoverage` 的层键保持同一精度。
    merged = {round(value, LEVEL_KEY_DECIMALS) for value in grid + floors}
    return sorted(merged, reverse=True)


class LevelCoverage:
    """多面调度的"同层已切"登记簿：同一层高上，先切的区域把切过的单元登记下来。

    为什么需要：各加工面的栅格建在**各自轮廓的包围盒**上（原点、间距都可能不同），
    掩码没法直接比对；而"顶面内环并入可切区域"之后，顶面与它下方的型腔底在
    42 / 40 这些共享层上会切到同一块 XY——同一层刀轨重叠，材料被切两遍。
    这里统一换算成世界坐标：查询区域拿自己的格心，去已切区域的栅格上取值，
    被覆盖的格子从可切掩码里去掉（整层被覆盖就整层跳过）。谁先调度谁先覆盖，
    层优先/深度优先共用同一份登记，遍历顺序只影响"谁让谁"。

    两个面在某层不相交时 :meth:`exclude` 返回全 False 的掩码，对刀路毫无影响；
    某层从没被登记过时返回 ``None``，调用方原样走老路径（零开销）。
    """

    def __init__(self) -> None:
        self._entries: dict[float, list[tuple[Any, NDArray[np.bool_]]]] = {}

    @staticmethod
    def cut_mask(region: Any, level_z: float, tool: Any,
                 offset_mm: float) -> NDArray[np.bool_] | None:
        """该区域在这一层**实际会切到**的单元：层可切掩码 ∩ 刀心可行区域。

        环切/平行扫描的条带互相盖住（步距 ≤ 刀具直径）、中心清理与轮廓精修也都在
        这张掩码里，所以它可以保守地当作"这一层已经加工过的面积"。刀心可行区之外
        的贴壁窄带（靠精修那一刀）故意不登记——宁可重复一小条，也不能漏切材料。
        返回 ``None`` 表示这一层什么也切不到，无需登记。
        """

        mask = region.level_mask(level_z, tool) & region.offset_mask(offset_mm)
        return mask if bool(mask.any()) else None

    def record(self, region: Any, level_z: float,
               mask: NDArray[np.bool_] | None) -> None:
        """登记某个区域在某层切过的掩码（``None`` = 该层没切到东西，不登记）。"""

        if mask is None:
            return
        self._entries.setdefault(level_key(level_z), []).append((region, mask))

    def exclude(self, region: Any, level_z: float) -> NDArray[np.bool_] | None:
        """该区域在某层应**跳过**的单元：已被同层先切区域覆盖的部分。

        返回 ``None`` 表示这一层还没有任何登记（老路径，零开销）。
        """

        entries = self._entries.get(level_key(level_z))
        if not entries:
            return None
        covered = np.zeros(region.shape, dtype=bool)
        xs = region.bounds[0] + (np.arange(region.shape[0]) + 0.5) * region.cell_mm
        ys = region.bounds[1] + (np.arange(region.shape[1]) + 0.5) * region.cell_mm
        for source, mask in entries:
            covered |= _sample_grid(source, mask, xs, ys)
        return covered


def _sample_grid(region: Any, mask: NDArray[np.bool_],
                 xs: NDArray[np.float64], ys: NDArray[np.float64]
                 ) -> NDArray[np.bool_]:
    """把 ``region`` 栅格上的掩码按世界坐标取到查询格心（越界处为 False）。

    查询格心落在源格 ``[x0+i·s, x0+(i+1)·s)`` 内即取该源格的值；两套栅格原点/
    间距不同也没关系。索引误差最多半格（≤ cell/2），远小于刀具半径带来的覆盖
    余量，不影响"已切区域"的判定。
    """

    x0, y0 = float(region.bounds[0]), float(region.bounds[1])
    spacing = float(region.cell_mm)
    rows = np.floor((xs - x0) / spacing).astype(np.intp)
    cols = np.floor((ys - y0) / spacing).astype(np.intp)
    rows_ok = (rows >= 0) & (rows < mask.shape[0])
    cols_ok = (cols >= 0) & (cols < mask.shape[1])
    sampled = mask[np.ix_(np.clip(rows, 0, mask.shape[0] - 1),
                           np.clip(cols, 0, mask.shape[1] - 1))]
    return sampled & (rows_ok[:, None] & cols_ok[None, :])


def in_level_transfer(region: Any, tool_radius: float,
                      start_xy: NDArray[np.float64] | Sequence[float],
                      end_xy: NDArray[np.float64] | Sequence[float],
                      *, level_mask: NDArray[np.bool_] | None = None) -> bool:
    """刀心能否从 ``start_xy`` 直线平移到 ``end_xy`` 而不越出区域（层内转移判据）。

    为什么需要它：层内两段刀路之间"抬到安全面再插下来"是最大的空程来源
    （每层壁精修、面铣每一刀都各来一次）。只要转移直线的 XY 全程落在
    **刀心可行区**（距边界 ≥ 刀具半径，可再与本层可切掩码求交）之内，
    平移就既不会撞岛、也不会切出腔壁，可以直接在层内连过去；
    穿岛、凹角贴边、跨两个型腔之间的实体都会被判否，调用方回退到抬刀转移。

    判定按 ``cell_mm`` 的一半步长采样中间点（首末两点**豁免**：它们本身就是
    既有刀路点，格化误差若把端点判成出界，会白白放弃本来安全的层内连接）。
    """

    mask = region.offset_mask(float(tool_radius))
    if level_mask is not None:
        mask = mask & level_mask
    if mask.size == 0 or not bool(mask.any()):
        return False
    a = np.asarray(start_xy, dtype=np.float64).reshape(2)
    b = np.asarray(end_xy, dtype=np.float64).reshape(2)
    cell = float(region.cell_mm)
    length = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    steps = int(ceil(length / max(0.5 * cell, 1e-9)))
    if steps <= 1:
        # 原地（层间直上直下的 XY 不动）：刀心刚走过这里，垂直降层必然安全
        return True
    x0, y0 = float(region.bounds[0]), float(region.bounds[1])
    rows, cols = int(mask.shape[0]), int(mask.shape[1])
    for k in range(1, steps):
        point = a + (b - a) * (k / steps)
        i = int(np.floor((point[0] - x0) / cell))
        j = int(np.floor((point[1] - y0) / cell))
        if i < 0 or j < 0 or i >= rows or j >= cols:
            return False
        if not bool(mask[i, j]):
            return False
    return True


def positions_from_xy(points_xy: NDArray[np.float64], z: float) -> NDArray[np.float64]:
    planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    return np.column_stack((planar, np.full(planar.shape[0], float(z))))


class MoveBuilder:
    """累积运动段，并保证相邻段首尾相接。

    ``dropped`` 记录"因为退化而没能成为运动段"的次数，规划器在生成结束后会检查它：
    正常刀路里这个数应当是 0 或个位数，一下子变很大就说明参数或区域算错了。
    """

    def __init__(self, context: MillingContext) -> None:
        self.context = context
        self.moves: list[Move] = []
        self.dropped = 0
        self._last: NDArray[np.float64] | None = None
        self._pass_index = 0

    @property
    def last_point(self) -> NDArray[np.float64] | None:
        return None if self._last is None else self._last.copy()

    def cut(self, points: NDArray[np.float64], *, label: str = "") -> None:
        self._append(MoveKind.CUT, points, self.context.feed, label)

    def link(self, points: NDArray[np.float64], *, label: str = "刀间连接") -> None:
        self._append(MoveKind.LINK, points, self.context.feed, label)

    def rapid(self, points: NDArray[np.float64], *, label: str = "快速移动") -> None:
        self._append(MoveKind.RAPID, points, self.context.rapid_feed, label)

    def plunge(self, point: NDArray[np.float64], *, label: str = "下刀") -> None:
        """从当前位置以进给速度下到目标点。"""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = self._last if self._last is not None else target
        points = np.vstack([start, target])
        self._append(MoveKind.CUT, points, self.context.plunge_feed, label)

    def entry(self, points: NDArray[np.float64], *, label: str = "下刀") -> None:
        """进刀段（斜插 / 螺旋 / 圆弧）：走**下刀进给**，标签统一带「下刀」。

        与 :meth:`plunge` 的区别只是点列——一个直上直下，一个带着斜坡或圆弧。
        统一标签是刻意的：统计、测试与阅读刀路时"下刀"就是"从上方进入材料"这件事。
        """

        self._append(MoveKind.CUT, points, self.context.plunge_feed, label)

    @property
    def last_direction(self) -> NDArray[np.float64] | None:
        """最后一段的 XY 单位方向（切向退刀用）；不足两点或零长时为 None。"""

        if not self.moves:
            return None
        points = np.asarray(self.moves[-1].points, dtype=np.float64)
        if points.shape[0] < 2:
            return None
        delta = points[-1, :2] - points[-2, :2]
        length = float(np.hypot(delta[0], delta[1]))
        if length <= 1e-9:
            return None
        return delta / length

    def next_pass(self) -> int:
        self._pass_index += 1
        return self._pass_index - 1

    # -- 常用组合动作 ------------------------------------------------------
    def rapid_to_safe(self, point: NDArray[np.float64], *, label: str = "抬刀-定位") -> None:
        """抬到安全面 → 平移到目标点上方 → 快速下到目标高度（NX 的"转移/快速"）。

        整段用**一条**运动段表达，避免"中间某一步退化成单点、被丢弃后位置状态与
        刀路不一致"的问题——那会让紧随其后的切削段也被当成重复段丢掉。
        """

        target = np.asarray(point, dtype=np.float64).reshape(3)
        safe_z = max(self.context.safe_z, float(target[2]))
        if self._last is None:
            self.rapid(np.array([[target[0], target[1], safe_z], target]), label=label)
            return
        start = self._last
        path = np.array(
            [
                start,
                [start[0], start[1], safe_z],
                [target[0], target[1], safe_z],
                target,
            ],
            dtype=np.float64,
        )
        # 已经在安全面上时省掉抬刀点，减少空行程
        if abs(start[2] - safe_z) <= 1e-9:
            path = path[1:]
        self.rapid(path, label=label)

    def _append(self, kind: MoveKind, points: NDArray[np.float64], feed: float,
                label: str) -> None:
        array = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        if array.shape[0] == 0:
            return
        if array.shape[0] < 2:
            # 单点定位：只更新当前位置，不产生运动段。若本来就停在这里，什么都不做。
            if self._last is None or float(np.linalg.norm(array[0] - self._last)) > 1e-9:
                self._last = array[0].copy()
            return
        # 这里**不**把"首点与当前位置重合"的点剪掉：剪掉之后两点段就只剩一个点，
        # 整条切削段会消失（往复走刀的每一刀起点都恰好等于上一刀的终点）。
        # 保留下来的后果只是运动段开头多一个零长步，时间轴与统计都能正确处理。
        self.moves.append(
            Move(kind, array, feed, pass_index=self._pass_index, label=label)
        )
        self._last = array[-1].copy()

    def finish(self, *, planner: str, label: str, notes: Sequence[str]) -> Toolpath:
        if not self.moves:
            raise PlanningError("没有生成任何刀轨：请检查加工区域、刀具直径与步距")
        end = self._last
        if end is not None and end[2] < self.context.safe_z:
            self.rapid(np.array([end[0], end[1], self.context.safe_z]), label="退刀")
        return Toolpath(
            moves=tuple(self.moves),
            planner=planner,
            planner_label=label,
            notes=tuple(notes),
        )


__all__ = [
    "LINK_CLEARANCE_MM",
    "MAX_RAMP_RADIUS_MM",
    "MillingContext",
    "MoveBuilder",
    "depth_levels",
    "in_level_transfer",
    "level_key",
    "level_passes_per_depth",
    "positions_from_xy",
    "stepped_levels",
]
