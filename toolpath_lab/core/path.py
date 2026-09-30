"""刀路模型。

所有策略都返回同一种东西：一串有序的**运动段**。每个运动段是一条折线加一个进给速度和
一个类型。播放、G-code 导出、统计都读这一份结构，因此新增策略不需要再写任何"适配层"。

运动段类型
----------
cut   沿一刀的切削进给
link  把两刀连起来、不抬刀的短进给
rapid 不切削的定位（抬刀、横移、下刀）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.mathutil import cumulative_lengths


class MoveKind(str, Enum):
    """单个运动段的类型。"""

    CUT = "cut"
    LINK = "link"
    RAPID = "rapid"


MOVE_KIND_LABELS: dict[str, str] = {
    MoveKind.CUT.value: "切削进给",
    MoveKind.LINK.value: "连接进给",
    MoveKind.RAPID.value: "快速移动",
}


@dataclass(frozen=True, slots=True)
class Move:
    """刀路里的一段运动。"""

    kind: MoveKind
    points: NDArray[np.float64]
    feed_mm_per_min: float
    pass_index: int = -1
    label: str = ""

    def __post_init__(self) -> None:
        points = np.array(self.points, dtype=np.float64, copy=True).reshape(-1, 3)
        if points.shape[0] < 2:
            raise ParameterError("一段运动至少需要两个点")
        if not np.all(np.isfinite(points)):
            raise ParameterError("运动段的坐标必须都是有限值")
        if not isfinite(self.feed_mm_per_min) or self.feed_mm_per_min <= 0:
            raise ParameterError("运动段的进给速度必须是有限正数")
        points.setflags(write=False)
        object.__setattr__(self, "points", points)

    @property
    def length_mm(self) -> float:
        if self.points.shape[0] < 2:
            return 0.0
        return float(np.linalg.norm(np.diff(self.points, axis=0), axis=1).sum())

    @property
    def is_cutting(self) -> bool:
        return self.kind is not MoveKind.RAPID

    @property
    def duration_s(self) -> float:
        """按自己的进给速度走完这段所需的时间。"""

        return self.length_mm / self.feed_mm_per_min * 60.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "kind_label": MOVE_KIND_LABELS[self.kind.value],
            "feed_mm_per_min": self.feed_mm_per_min,
            "pass_index": self.pass_index,
            "label": self.label,
            "length_mm": self.length_mm,
            "points": [[round(float(value), 4) for value in row] for row in self.points],
        }


@dataclass(frozen=True, slots=True)
class Toolpath:
    """一条完整刀路：有序运动段 + 来源信息。"""

    moves: tuple[Move, ...] = field(default_factory=tuple)
    planner: str = ""
    planner_label: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.moves:
            raise ParameterError("刀路至少需要一段运动")

    @property
    def pass_count(self) -> int:
        return len({move.pass_index for move in self.moves if move.pass_index >= 0})

    @property
    def point_count(self) -> int:
        return int(sum(move.points.shape[0] for move in self.moves))

    def _length(self, kinds: tuple[MoveKind, ...]) -> float:
        return float(sum(move.length_mm for move in self.moves if move.kind in kinds))

    @property
    def cut_length_mm(self) -> float:
        return self._length((MoveKind.CUT,))

    @property
    def rapid_length_mm(self) -> float:
        return self._length((MoveKind.RAPID,))

    @property
    def total_length_mm(self) -> float:
        return float(sum(move.length_mm for move in self.moves))

    @property
    def estimated_time_s(self) -> float:
        return float(sum(move.duration_s for move in self.moves))

    @property
    def cutting_time_s(self) -> float:
        return float(sum(move.duration_s for move in self.moves if move.is_cutting))

    def statistics(self) -> dict[str, Any]:
        return {
            "pass_count": self.pass_count,
            "move_count": len(self.moves),
            "point_count": self.point_count,
            "cut_length_mm": self.cut_length_mm,
            "rapid_length_mm": self.rapid_length_mm,
            "total_length_mm": self.total_length_mm,
            "cutting_time_s": self.cutting_time_s,
            "estimated_time_s": self.estimated_time_s,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "planner_label": self.planner_label,
            "notes": list(self.notes),
            "moves": [move.to_payload() for move in self.moves],
            "statistics": self.statistics(),
        }


def retract_move(
    start: NDArray[np.float64],
    end: NDArray[np.float64],
    safe_z_mm: float,
    feed_mm_per_min: float,
) -> Move:
    """抬刀 → 横移 → 下刀 这段最经典的快速定位。"""

    start = np.asarray(start, dtype=np.float64).reshape(3)
    end = np.asarray(end, dtype=np.float64).reshape(3)
    safe_z = max(float(safe_z_mm), float(start[2]), float(end[2]))
    points = np.array(
        [
            start,
            [start[0], start[1], safe_z],
            [end[0], end[1], safe_z],
            end,
        ],
        dtype=np.float64,
    )
    return Move(MoveKind.RAPID, points, feed_mm_per_min, label="抬刀-横移-下刀")


def polyline_length(points: NDArray[np.float64]) -> float:
    """折线长度（供测试与外部脚本使用）。"""

    array = np.asarray(points, dtype=np.float64)
    if array.shape[0] < 2:
        return 0.0
    return float(cumulative_lengths(array)[-1])
