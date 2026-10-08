"""按进给速度做时间参数化。

刀路是几何，播放需要的是时间。每段运动自带进给速度，所以时间轴就是"各段弧长 / 该段进给"
的累加：切削段用切削进给，快移段用快移速度，界面上的"预计工时"因此不是总长除以一个进给。

Original vertices are mandatory. max_samples budgets additional samples; it is
a soft limit when preserving all polyline corners requires more points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.mathutil import cumulative_lengths
from toolpath_lab.core.path import Move, MoveKind, Toolpath

#: 载荷里使用的运动类型编码（kind_runs 里是整数，省掉重复字符串）。
KIND_CODES: dict[str, int] = {
    MoveKind.CUT.value: 0,
    MoveKind.LINK.value: 1,
    MoveKind.RAPID.value: 2,
}
KIND_CODE_LABELS: dict[int, str] = {value: key for key, value in KIND_CODES.items()}


@dataclass(frozen=True, slots=True)
class TimelineState:
    """播放到某一时刻的机床状态。"""

    time_s: float
    position: NDArray[np.float64]
    kind: str
    move_index: int
    progress: float


@dataclass(frozen=True, slots=True)
class Timeline:
    """刀路的采样时间历史。"""

    times_s: NDArray[np.float64]
    positions: NDArray[np.float64]
    kind_codes: NDArray[np.int64]
    move_indices: NDArray[np.int64]
    duration_s: float

    @property
    def sample_count(self) -> int:
        return int(self.times_s.shape[0])

    def _runs(self, values: NDArray[np.int64]) -> list[list[int]]:
        """把取值数组编码成 [起始下标, 取值] 的游程。"""

        if values.size == 0:
            return []
        changes = np.flatnonzero(np.diff(values) != 0) + 1
        starts = np.concatenate(([0], changes))
        return [[int(start), int(values[start])] for start in starts]

    def state_at(self, time_s: float) -> TimelineState:
        """插值出任意时刻的状态。"""

        duration = max(self.duration_s, 1e-9)
        query = float(np.clip(time_s, 0.0, self.duration_s))
        index = int(np.searchsorted(self.times_s, query, side="right") - 1)
        index = int(np.clip(index, 0, self.sample_count - 1))
        nxt = min(index + 1, self.sample_count - 1)
        t0 = float(self.times_s[index])
        t1 = float(self.times_s[nxt])
        ratio = 0.0 if t1 <= t0 else (query - t0) / (t1 - t0)
        position = self.positions[index] + ratio * (self.positions[nxt] - self.positions[index])
        return TimelineState(
            time_s=query,
            position=position,
            kind=KIND_CODE_LABELS[int(self.kind_codes[index])],
            move_index=int(self.move_indices[index]),
            progress=float(query / duration),
        )

    def to_payload(self, *, time_decimals: int = 9, position_decimals: int = 6) -> dict[str, Any]:
        """紧凑的 JSON 形式。"""

        return {
            "duration_s": round(self.duration_s, time_decimals),
            "sample_count": self.sample_count,
            "times": [round(float(value), time_decimals) for value in self.times_s],
            "positions": [
                [round(float(value), position_decimals) for value in row]
                for row in self.positions
            ],
            "kind_runs": self._runs(self.kind_codes),
            "move_runs": self._runs(self.move_indices),
            "kind_codes": KIND_CODE_LABELS,
        }


def _resample_move(move: Move, samples: int) -> NDArray[np.float64]:
    """按等弧长重采样一段运动，两端点一定保留。"""

    points = move.points
    cumulative = cumulative_lengths(points)
    total = float(cumulative[-1])
    if samples <= 2 or total <= 1e-9:
        return points[[0, -1]]
    targets = np.linspace(0.0, total, samples)
    return np.column_stack(
        [np.interp(targets, cumulative, points[:, axis]) for axis in range(3)]
    )


def build_timeline(toolpath: Toolpath, *, max_samples: int = 4000) -> Timeline:
    """Preserve corners and original arc-length timing; budget applies to extra samples.

    Mandatory original vertices may exceed max_samples. Dropping those vertices
    would cut corners and shorten curved moves, so fidelity takes precedence.
    """
    prepared = []
    for index, move in enumerate(toolpath.moves):
        distances = cumulative_lengths(move.points)
        keep = np.concatenate(([True], np.diff(distances) > 0))
        prepared.append((index, move, distances[keep], move.points[keep]))
    active = [item for item in prepared if item[2][-1] > 0]
    if not active:
        move = toolpath.moves[-1]
        return Timeline(np.array([0.0]), move.points[-1:],
                        np.array([KIND_CODES[move.kind.value]]),
                        np.array([len(toolpath.moves) - 1]), 0.0)
    mandatory = sum(len(item[2]) - 1 for item in active) + 1
    extras = max(0, int(max_samples) - mandatory)
    total_length = sum(float(item[2][-1]) for item in active)
    times, positions, kinds, indices = [], [], [], []
    clock = 0.0
    for index, move, distances, original in active:
        extra = int(extras * float(distances[-1]) / total_length)
        targets = np.unique(np.concatenate((distances,
                            np.linspace(0, distances[-1], extra + 2)[1:-1])))
        sampled = np.column_stack([np.interp(targets, distances, original[:, axis])
                                   for axis in range(3)])
        local_times = clock + targets / move.feed_mm_per_min * 60.0
        if positions:
            if not np.allclose(positions[-1][-1], sampled[0], atol=1e-8, rtol=0):
                from toolpath_lab.core.errors import ParameterError
                raise ParameterError("运动段不连续，无法生成连续播放时间轴")
            # A shared boundary belongs to the outgoing interval.
            kinds[-1][-1] = KIND_CODES[move.kind.value]
            indices[-1][-1] = index
            sampled, local_times = sampled[1:], local_times[1:]
        clock += move.duration_s
        times.append(local_times)
        positions.append(sampled)
        kinds.append(np.full(len(local_times), KIND_CODES[move.kind.value], dtype=np.int64))
        indices.append(np.full(len(local_times), index, dtype=np.int64))
    return Timeline(np.concatenate(times), np.vstack(positions),
                    np.concatenate(kinds), np.concatenate(indices), clock)
