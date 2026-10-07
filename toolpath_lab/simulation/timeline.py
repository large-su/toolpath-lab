"""Time parameterisation driven by feed rates.

A toolpath is geometry; playback needs time. Every move carries its own feed rate, so the timeline is
simply the accumulated "arc length / feed of that move": cutting moves use the cutting feed, rapid
moves the rapid feed, which is why the "estimated time" in the UI is not the total length divided by
one feed rate.

Sampling is capped at max_samples points to bound the payload, but the boundary of every move is kept,
so playback never interpolates across a move boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.mathutil import cumulative_lengths
from toolpath_lab.core.path import Move, MoveKind, Toolpath

#: Move kind codes used in the payload (integer runs in kind_runs, avoiding repeated strings).
KIND_CODES: dict[str, int] = {
    MoveKind.CUT.value: 0,
    MoveKind.LINK.value: 1,
    MoveKind.RAPID.value: 2,
}
KIND_CODE_LABELS: dict[int, str] = {value: key for key, value in KIND_CODES.items()}


@dataclass(frozen=True, slots=True)
class TimelineState:
    """Machine state at one playback instant."""

    time_s: float
    position: NDArray[np.float64]
    kind: str
    move_index: int
    progress: float


@dataclass(frozen=True, slots=True)
class Timeline:
    """Sampled time history of a toolpath."""

    times_s: NDArray[np.float64]
    positions: NDArray[np.float64]
    kind_codes: NDArray[np.int64]
    move_indices: NDArray[np.int64]
    duration_s: float

    @property
    def sample_count(self) -> int:
        return int(self.times_s.shape[0])

    def _runs(self, values: NDArray[np.int64]) -> list[list[int]]:
        """Encode a value array as [start index, value] runs."""

        if values.size == 0:
            return []
        changes = np.flatnonzero(np.diff(values) != 0) + 1
        starts = np.concatenate(([0], changes))
        return [[int(start), int(values[start])] for start in starts]

    def state_at(self, time_s: float) -> TimelineState:
        """Interpolate the state at an arbitrary instant."""

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

    def to_payload(self, *, time_decimals: int = 4, position_decimals: int = 3) -> dict[str, Any]:
        """Compact JSON form."""

        return {
            "duration_s": round(self.duration_s, 6),
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
    """Resample one move at equal arc length, always keeping both endpoints."""

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
    """Turn a toolpath into a sampled time history."""

    lengths = np.array([move.length_mm for move in toolpath.moves], dtype=np.float64)
    total_length = float(lengths.sum())
    budget = max(2 * len(toolpath.moves), int(max_samples))
    if total_length <= 1e-9:
        shares = np.full(len(toolpath.moves), 2, dtype=np.int64)
    else:
        shares = np.maximum(2, np.round(budget * lengths / total_length).astype(np.int64))
    if int(shares.sum()) > budget:
        shares = np.maximum(2, np.floor(shares * (budget / float(shares.sum()))).astype(np.int64))

    times: list[NDArray[np.float64]] = []
    positions: list[NDArray[np.float64]] = []
    kind_codes: list[NDArray[np.int64]] = []
    move_indices: list[NDArray[np.int64]] = []
    clock = 0.0

    for index, move in enumerate(toolpath.moves):
        sampled = _resample_move(move, int(shares[index]))
        steps = np.linalg.norm(np.diff(sampled, axis=0), axis=1)
        local = np.concatenate(([0.0], np.cumsum(steps))) / move.feed_mm_per_min * 60.0
        local_times = clock + local
        clock = float(local_times[-1])
        if positions and index > 0:
            # The previous move ends where this one starts, so drop the duplicated sample.
            sampled = sampled[1:]
            local_times = local_times[1:]
        times.append(local_times)
        positions.append(sampled)
        kind_codes.append(np.full(local_times.shape[0], KIND_CODES[move.kind.value], dtype=np.int64))
        move_indices.append(np.full(local_times.shape[0], index, dtype=np.int64))

    return Timeline(
        times_s=np.concatenate(times),
        positions=np.vstack(positions),
        kind_codes=np.concatenate(kind_codes),
        move_indices=np.concatenate(move_indices),
        duration_s=clock,
    )
