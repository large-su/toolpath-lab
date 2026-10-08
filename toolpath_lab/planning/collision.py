"""Holder collision: does the part of the tool above the flutes fit into the pocket?

The 2.5D model has vertical walls: everything inside the region is machined away down to `depth_mm`,
everything outside stays solid from the top face down. The tool is modelled as two cylinders -- the
cutting head of radius R up to `flute_mm`, and the shank of radius `shank_radius_mm` from there up to
`length_mm` -- which is also exactly what the 3D view draws. Two things can go wrong once a cut is
deeper than the flutes:

- **the shank hits the wall**. Its lowest point sits at `flute - depth`, so it enters the pocket as soon
  as the cut is deeper than the flutes; from there it needs `shank_radius_mm` of room to the wall, while
  the planner only guarantees the *cutter* its own clearance (`cutting_radius_mm`). A shank wider than
  that rubs the wall wherever the path runs near the outline -- and every strategy leaves the outermost
  pass exactly at that clearance, so the closest approach is normally the number to compare against;
- **the tool is too short**. With `depth > length` the tool's top end, where the holder starts, is below
  the top face. The holder itself is not modelled (only the geometry up to `length_mm` is), so this is
  reported as a length problem rather than as a holder collision.

The tool's own radius is a function of height, so the check asks, per point, for the widest radius the
tool presents *inside the pocket* there -- the tapered flank up to the flutes, or the shank above them.
That makes the tolerance below necessary: the planner already insets the path by exactly that flank
radius at the deepest layer, so a tapered tool measures a margin of zero and the offset rounding must
not turn it into a collision. A tapered flank therefore never collides on its own; only the shank above
the flutes (which the inset does not know about) or a tool that is too short can.

Both are warnings, not errors: flute and shank are the caller's estimate of a real tool, and a plan that
would rub is still a plan worth looking at.

Two deliberate exclusions, both about what the numbers mean:

- only **passes** are checked (links too, since they run at cutting depth). A ramp or helix *entry* is a
  cutting move without a pass index and may leave the region on purpose; the entry note already reports
  how far, and what meets material out there is the cutting head, not the shank;
- only points **inside** the region count as "in the pocket". A point outside it is not in the pocket at
  all, so it says nothing about the shank fitting into one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.geometry2d import distance_to_boundary, point_in_polygon

_EPS = 1e-9

#: Interference below this is not reported: the planner insets a tapered tool's path by exactly the
#: flank radius at the deepest layer, so the measured margin there is zero and the offset's polygonal
#: rounding would otherwise show up as a collision of a few microns.
_TOLERANCE_MM = 0.01


def _tool_radius_mm(tool: Tool, depth_mm: float) -> float:
    """Widest radius the tool presents in a cut of this depth (the flank, or the shank above it)."""

    flank = tool.wall_clearance_mm(min(depth_mm, tool.flute_mm))
    if depth_mm <= tool.flute_mm + _EPS:
        return flank
    return max(flank, tool.shank_radius_mm)


@dataclass(frozen=True, slots=True)
class HolderCheck:
    """What the tool geometry above the flutes would do in this pocket."""

    flute_mm: float
    shank_radius_mm: float
    tool_length_mm: float
    #: Deepest tip depth the toolpath reaches (0 for a plan that only skims the top face).
    deepest_cut_mm: float
    #: Cutting points inside the region whose tip is deeper than the flutes (0 = the shank stays out).
    engaged_points: int
    #: Widest radius the tool presents in the pocket over those points (the flank or the shank).
    widest_radius_mm: float
    #: Smallest distance from the tool axis to the region outline among those points.
    clearance_mm: float | None
    #: Tightest room left over anywhere in the pocket (negative = interference), None when not engaged.
    margin_mm: float | None

    @property
    def shortfall_mm(self) -> float:
        """How far the tool overlaps the wall at its tightest point (0 when it clears)."""

        if self.margin_mm is None:
            return 0.0
        return max(0.0, -self.margin_mm)

    @property
    def length_shortfall_mm(self) -> float:
        """How much deeper the cut is than the tool is long (0 when the holder stays above the part)."""

        return max(0.0, self.deepest_cut_mm - self.tool_length_mm)

    @property
    def collides(self) -> bool:
        return self.shortfall_mm > _EPS or self.length_shortfall_mm > _EPS

    def describe(self) -> dict[str, Any]:
        """Compact JSON form for the plan response."""

        return {
            "flute_mm": round(self.flute_mm, 4),
            "shank_radius_mm": round(self.shank_radius_mm, 4),
            "widest_radius_mm": round(self.widest_radius_mm, 4),
            "tool_length_mm": round(self.tool_length_mm, 4),
            "deepest_cut_mm": round(self.deepest_cut_mm, 4),
            "engaged_points": self.engaged_points,
            "clearance_mm": None if self.clearance_mm is None else round(self.clearance_mm, 4),
            "margin_mm": None if self.margin_mm is None else round(self.margin_mm, 4),
            "shortfall_mm": round(self.shortfall_mm, 4),
            "length_shortfall_mm": round(self.length_shortfall_mm, 4),
            "collides": self.collides,
        }


def _is_entry(move: Any) -> bool:
    """A cutting move without a pass index is an entry (see planning/entry.py for the convention)."""

    return move.kind is MoveKind.CUT and move.pass_index < 0


def _empty(tool: Tool, deepest: float) -> HolderCheck:
    return HolderCheck(
        flute_mm=tool.flute_mm,
        shank_radius_mm=tool.shank_radius_mm,
        tool_length_mm=tool.length_mm,
        deepest_cut_mm=deepest,
        engaged_points=0,
        widest_radius_mm=0.0,
        clearance_mm=None,
        margin_mm=None,
    )


def check_holder(toolpath: Toolpath, region: RegionShape, tool: Tool) -> HolderCheck:
    """Measure the tool above the flutes against the pocket walls."""

    polygon = np.asarray(region.boundary(), dtype=np.float64).reshape(-1, 2)
    flute = tool.flute_mm
    deepest = 0.0
    engaged: list[np.ndarray] = []
    depths_there: list[np.ndarray] = []
    for move in toolpath.moves:
        if not move.is_cutting or _is_entry(move):
            continue
        depths = -move.points[:, 2]
        deepest = max(deepest, float(depths.max()))
        deeper_than_flute = depths > flute + _EPS
        if deeper_than_flute.any():
            engaged.append(move.points[deeper_than_flute, :2])
            depths_there.append(depths[deeper_than_flute])
    if not engaged:
        return _empty(tool, deepest)

    points = np.vstack(engaged)
    point_depths = np.concatenate(depths_there)
    inside = point_in_polygon(points, polygon)
    if not inside.any():
        return _empty(tool, deepest)
    kept_points, kept_depths = points[inside], point_depths[inside]
    distances = distance_to_boundary(kept_points, polygon)
    # The tool's own radius depends on the depth at that point, so the room each point leaves is its
    # distance minus the radius the tool presents there; the tightest of those is the margin.
    radii = np.array([_tool_radius_mm(tool, float(depth)) for depth in kept_depths])
    margin = float((distances - radii).min())
    return HolderCheck(
        flute_mm=flute,
        shank_radius_mm=tool.shank_radius_mm,
        tool_length_mm=tool.length_mm,
        deepest_cut_mm=deepest,
        engaged_points=int(inside.sum()),
        widest_radius_mm=float(radii.max()),
        clearance_mm=float(distances.min()),
        # A margin inside the tolerance is reported as zero: it is offset rounding, not a fit problem.
        margin_mm=0.0 if abs(margin) <= _TOLERANCE_MM else margin,
    )


def holder_warnings(check: HolderCheck) -> list[str]:
    """Warnings for a pocket the tool geometry above the flutes does not fit into."""

    messages: list[str] = []
    if check.length_shortfall_mm > _EPS:
        messages.append(
            f"刀具长度不够：最深切到 {check.deepest_cut_mm:.2f} mm，而刀具长度只有 "
            f"{check.tool_length_mm:g} mm —— 刀具上端（夹头）会伸进工件，"
            f"请把刀具长度加到至少 {check.deepest_cut_mm:.2f} mm"
        )
    if check.shortfall_mm > _EPS and check.clearance_mm is not None:
        messages.append(
            f"刀柄碰撞：切深超过刀刃长度 {check.flute_mm:g} mm 后刀柄（R{check.shank_radius_mm:g} mm）"
            f"伸进型腔，最近处离壁只有 {check.clearance_mm:.2f} mm，差 "
            f"{check.shortfall_mm:.2f} mm —— 会啃到壁（换更长刀刃的刀、减小刀柄直径，"
            "或减小吃刀深度）"
        )
    return messages


def holder_note(check: HolderCheck) -> str | None:
    """A note for a shank that does enter the pocket and still clears the wall (None otherwise)."""

    if check.engaged_points == 0 or check.collides or check.margin_mm is None:
        return None
    return (
        f"刀柄检查：切深超过刀刃 {check.flute_mm:g} mm 后刀柄（R{check.shank_radius_mm:g} mm）"
        f"进入型腔，最近处离壁 {check.clearance_mm:.2f} mm，最紧处余量 "
        f"{check.margin_mm:.2f} mm"
    )
