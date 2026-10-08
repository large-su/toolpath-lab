"""Entry moves: the cutting segment that brings the tool down to the layer it is about to cut.

A plunge is a rapid, so there is nothing to say about it. A ramp or a helix is a **cutting** move: it
descends at `ramp_angle_deg`, so its length follows from the depth of cut and that angle alone. A ramp
is a straight 3D segment, which makes it exactly `depth / sin(angle)`; a helix travels the same angle
along its own circumference, so it needs `depth / (2 * pi * radius * tan(angle))` turns -- rounded up
to whole turns, because the entry has to come back to the XY it started above to merge into the cut.

That is where the surprise lives: a 1° ramp into a 5 mm cut is 286 mm of cutting before the first pass
starts, and a helix with a small radius makes up the same angle in many more turns of its own (and can
land a whole extra turn, up to `2 * pi * radius` more travel, because of the rounding). Neither entry
is clipped against the region outline either, so a long one walks out of the part. The plan therefore
reports what it built -- see `PlanningContext.entry_moves` -- instead of leaving it to be discovered on
the machine.

The measurement is taken from the finished toolpath rather than recomputed from the parameters, so it
counts what the machine will really do: one entry per layer once step-down has stacked the path, and
any entry a third party plugin builds with the shared entry builder. The convention it reads is the one
the CSV export already documents: a **cutting move without a pass index** is an entry (links and rapids
are not cutting, and a pass carries an index).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import RegionShape, polygon_bounds

#: How each entry mode is named, both in the move label and in the plan's notes: the labels themselves
#: are user visible text, so they are Chinese (CONTRIBUTING / docs/extending.md section 5).
ENTRY_MODE_LABELS: dict[str, str] = {"ramp": "斜坡", "helix": "螺旋"}
#: Suffix of a labelled entry move, so it reads like "layer 2" + the mode + this suffix.
ENTRY_LABEL_SUFFIX = "进刀"

#: Lengths within this of each other are reported as "every entry the same length".
_SAME_LENGTH_MM = 0.005
#: Distances below this are reported as zero (they are grid and rounding noise, not a real reach).
_EPS = 0.005


@dataclass(frozen=True, slots=True)
class EntrySummary:
    """What the entry moves of one plan cost."""

    count: int
    total_mm: float
    shortest_mm: float
    longest_mm: float
    modes: tuple[str, ...] = ()
    #: How far the longest entry reaches outside the region's bounding box (0 when it stays inside).
    outside_mm: float = 0.0

    def note(self) -> str:
        """The line this summary contributes to the plan's notes (user visible, so Chinese)."""

        mode = "、".join(self.modes)
        if self.count == 1:
            lengths = f"{self.longest_mm:.2f} mm"
        elif self.longest_mm - self.shortest_mm <= _SAME_LENGTH_MM:
            lengths = f"每段 {self.longest_mm:.2f} mm"
        else:
            lengths = f"每段 {self.shortest_mm:.2f} ~ {self.longest_mm:.2f} mm"
        reach = (
            f"，最长一段有 {self.outside_mm:.2f} mm 在区域轮廓之外（进刀段不裁剪到轮廓）"
            if self.outside_mm > _EPS
            else ""
        )
        head = f"进刀：{mode} " if mode else "进刀："
        return (
            f"{head}{self.count} 段，{lengths}（合计 {self.total_mm:.2f} mm）{reach}；"
            "进刀段是切削移动，已计入切削长度与工时"
        )


def _mode_name(label: str) -> str:
    """The mode a labelled entry move belongs to, or "" for an entry this module cannot name."""

    for name in ENTRY_MODE_LABELS.values():
        if name and name in label:
            return name
    return ""


def _overshoot_mm(
    points: NDArray[np.float64], bounds: tuple[float, float, float, float]
) -> float:
    """How far the polyline reaches outside the [x_min, x_max, y_min, y_max] rectangle."""

    x_min, x_max, y_min, y_max = bounds
    east_west = np.maximum(np.maximum(x_min - points[:, 0], points[:, 0] - x_max), 0.0)
    north_south = np.maximum(np.maximum(y_min - points[:, 1], points[:, 1] - y_max), 0.0)
    return float(np.max(np.hypot(east_west, north_south)))


def _region_bounds(region: RegionShape) -> tuple[float, float, float, float]:
    """The region's bounding box as (x_min, x_max, y_min, y_max)."""

    polygon = np.asarray(region.boundary(), dtype=np.float64)
    (x_min, x_max), (y_min, y_max) = polygon_bounds(polygon)
    return (x_min, x_max, y_min, y_max)


def measure_entry(toolpath: Toolpath, region: RegionShape) -> EntrySummary | None:
    """Measure the entry moves of a finished toolpath, or None when the plan only plunges.

    Consecutive entry segments count as one entry: corner slowdown (or a plugin) may cut a single
    descent into several moves, and those pieces still form the one entry.
    """

    bounds = _region_bounds(region)
    lengths: list[float] = []
    modes: list[str] = []
    outside = 0.0
    in_entry = False
    for move in toolpath.moves:
        is_entry = move.kind is MoveKind.CUT and move.pass_index < 0
        if not is_entry:
            in_entry = False
            continue
        if not in_entry:
            lengths.append(0.0)
            modes.append(_mode_name(move.label))
            in_entry = True
        lengths[-1] += move.length_mm
        outside = max(outside, _overshoot_mm(move.points, bounds))
    if not lengths:
        return None
    return EntrySummary(
        count=len(lengths),
        total_mm=float(sum(lengths)),
        shortest_mm=float(min(lengths)),
        longest_mm=float(max(lengths)),
        modes=tuple(name for name in dict.fromkeys(modes) if name),
        outside_mm=outside,
    )
