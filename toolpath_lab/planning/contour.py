"""Contour (equidistant outline) toolpath.

Starting from the region outline, every inward offset by one stepover gives one layer, until the
offset degenerates:

1. the first layer offsets the outline inwards by the tool's footprint radius on the machining
   plane, so the tool never cuts outside the region;
2. every further layer offsets inwards by one more stepover;
3. one layer may produce **several loops**: once a concave neck is eaten away by the offset, the
   shape splits into pieces that are not connected to each other;
4. every loop is discretised at the sampling step into a closed polyline, and neighbouring rings run
   in alternating directions (climb/conventional);
5. transitions between rings come in two flavours: a ring nested inside the previous one is reached
   with a link move at cutting depth; sibling rings split off in the same layer, or cases where the
   nesting cannot be decided, retract to the safe height and rapid over -- crossing a narrow neck at
   cutting depth would bite into the material.

The offset geometry lives in planning/geometry2d.py (convex miters, reflex arc joins, splitting at
self-intersections, chaining by smallest turn), which also explains why the early version's habit of
re-connecting the surviving vertices in their original order was wrong.

Known limitation: rings are connected by straight moves only, there are no lead-in/lead-out arcs.

**The offset extension cap is not a correctness switch**: a convex corner's miter point sits
d*tan(turn/2) away from the vertex, which looks like "sharp corners need long extensions", but once
the miter point falls outside the shifted segment the two adjacent edges are necessarily shorter than
the required reach, so the material there is at most 2L*sin(interior/2) < 2d wide -- narrower than the
tool, eroded away completely, and that edge should not appear on the offset boundary at all. That is
why `geometry2d` applies a small numerical margin; the case where nothing is left (a region too thin
for the tool) ends up in the PlanningError below.

**Ring winding versus climb/conventional milling**: `offset_loops` returns counter-clockwise loops.
Contouring walks from the outside in, so the unmachined material is always on the **inside** of the
ring (the ring outside it has already been cut), therefore

- **counter-clockwise** = the cutting edge moves with the feed at the contact point = **climb**;
- **clockwise** = **conventional**.

That follows from "spindle M03 (clockwise seen from above) + right-hand tool". With M04, or when the
material is on the other side of the path (machining an outer profile rather than this region), climb
and conventional swap -- the parameter is named after the geometric winding and the convention is
documented in the parameter table of the README. The raster strategy has no such parameter: one pass
is climb on one side and conventional on the other, there is no single answer, and zigzag alternates
by itself.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import MOTION_PARAMETERS, Planner, PlanningContext
from toolpath_lab.planning.geometry2d import offset_loops, point_in_polygon, resample_ring
from toolpath_lab.planning.registry import PLANNERS

#: Loops whose offset area is below this (mm^2) are dropped: a fragment too thin for a pass.
_MIN_RING_AREA_MM2 = 0.5

#: Ring direction -> wording used in the notes.
_DIRECTION_LABELS = {
    "alternate": "交替（顺铣/逆铣）",
    "climb": "全顺铣（逆时针）",
    "conventional": "全逆铣（顺时针）",
}


@PLANNERS.register
class ContourPlanner(Planner):
    """Contour toolpath that offsets the region outline inwards ring by ring."""

    id: ClassVar[str] = "contour"
    label: ClassVar[str] = "环切"
    description: ClassVar[str] = "从轮廓逐圈向内偏置（等距轮廓），凹形状分裂出的环会分别加工"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路",
                 help="每环离散成折线的点距；越小越贴合曲线，刀点也越多"),
            spec("ring_direction", "环绕向", K.CHOICE, "alternate", group="刀路",
                 choices=(
                     Choice("alternate", "交替（顺铣/逆铣）"),
                     Choice("climb", "全顺铣（逆时针）"),
                     Choice("conventional", "全逆铣（顺时针）"),
                 ),
                 help="顺逆铣：按 M03 主轴 + 右手刀具，逆时针为顺铣；约定见 README「参数与固定值」"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    ) + MOTION_PARAMETERS

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        ring_direction = str(context.parameters["ring_direction"])
        boundary = context.boundary
        distance = context.cutting_radius_mm

        layers: list[list[NDArray[np.float64]]] = []
        while True:
            loops = offset_loops(boundary, distance, min_area_mm2=_MIN_RING_AREA_MM2)
            if not loops:
                break
            layers.append(loops)
            distance += stepover
        if not layers:
            raise PlanningError(
                f"环切没有生成任何刀轨：刀具贴壁间隙 {context.cutting_radius_mm:g} mm "
                "已经超过区域的内切半径，请减小刀具直径或扩大区域"
            )

        moves: list[Move] = []
        previous: NDArray[np.float64] | None = None
        previous_loop: NDArray[np.float64] | None = None
        index = 0
        for loops in layers:
            for loop in loops:
                sampled = resample_ring(loop, sample_step)
                closed = np.vstack([sampled, sampled[:1]])
                if self._should_reverse(ring_direction, index):
                    closed = closed[::-1]
                positions = context.to_positions(closed)
                if previous is None:
                    moves.extend(
                        context.entry_moves(positions[0], positions[1] - positions[0])
                    )
                elif previous_loop is not None and self._is_nested(loop, previous_loop):
                    moves.append(context.link_move(previous, positions[0]))
                else:
                    moves.append(context.rapid_between(previous, positions[0]))
                moves.append(
                    Move(
                        MoveKind.CUT,
                        positions,
                        context.feed_mm_per_min,
                        pass_index=index,
                        label=f"第 {index + 1} 环",
                    )
                )
                previous = positions[-1]
                previous_loop = loop
                index += 1
        moves.append(context.retract_move_up(previous))

        ring_count = sum(len(loops) for loops in layers)
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"环切：共 {ring_count} 环（{len(layers)} 层），切宽 {stepover:g} mm，"
                f"采样步长 {sample_step:g} mm，环绕向 {_DIRECTION_LABELS[ring_direction]}",
                "同层分裂出的环之间抬刀快移，套在里面的环之间用连接进给",
                f"边界固定内缩一个刀具贴壁间隙（R{context.cutting_radius_mm:g} mm），"
                f"安全高度 {context.safe_height_mm:g} mm、"
                f"快移 {context.rapid_feed_mm_per_min:g} mm/min",
            ),
        )

    @staticmethod
    def _should_reverse(ring_direction: str, index: int) -> bool:
        """Whether this ring runs the other way round.

        offset_loops returns counter-clockwise loops, which is climb milling (material inside the
        ring); so "climb" keeps them as they are, "conventional" reverses every one of them, and
        "alternate" flips by index (the behaviour that used to be the only one).
        """

        if ring_direction == "climb":
            return False
        if ring_direction == "conventional":
            return True
        return index % 2 == 1

    @staticmethod
    def _is_nested(inner: NDArray[np.float64], outer: NDArray[np.float64]) -> bool:
        """Whether inner lies inside outer (decides link move versus retract between rings)."""

        return bool(point_in_polygon(inner[:1], outer)[0])
