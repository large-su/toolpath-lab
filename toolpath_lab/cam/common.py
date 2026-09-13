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
        return float(self.parameters["stepover_mm"])

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
    "level_passes_per_depth",
    "positions_from_xy",
]
