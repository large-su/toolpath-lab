"""Raster (parallel scanline) toolpath.

Two modes:

- **Zigzag**: every other pass runs the other way and neighbouring passes are joined at their ends,
  which is efficient;
- **One-way**: every pass runs the same way, and between passes the tool retracts to the safe height
  and comes back to the start; slower, but the cutting conditions are identical for every pass
  (climb/conventional stays fixed).

The recipe is simple, and this is the piece of the base worth reading first:

1. rotate the region outline into the "cutting frame": u along the cutting direction, v across it;
2. lay one pass line every stepover in v;
3. intersect every pass line with the region (scanline), giving the intervals inside it (a chord for
   a circle, the full width for a square);
4. shrink both ends of an interval by the tool's footprint radius to get the pass's start and end;
5. let the mode decide the direction and the connection between passes, then add plunge and retract.

A pass has only two points because the machining plane is flat -- which is exactly what a base
should look like: to machine a curved surface, discretise each pass line at the sampling step.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import direction_2d
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.planning.base import MOTION_PARAMETERS, Planner, PlanningContext
from toolpath_lab.planning.geometry2d import scanline_intervals
from toolpath_lab.planning.registry import PLANNERS

_MODE_LABELS = {"zigzag": "往复", "one_way": "单向"}

#: Boundary handling -> wording.
#: "Offset outwards (negative offset)" is deliberately not offered: a scanline cannot produce
#: intervals outside the outline, so a negative offset silently drops the v-direction pass lines,
#: leaving the cut outside the region in u while an asymmetric uncut band remains in v -- that is
#: not "offsetting outwards", it is simply wrong. To leave material, use stock_allowance_mm (an
#: allowance along the outline), whose geometry is self-consistent.
_BOUNDARY_LABELS = {"inset": "内缩一个刀具半径", "none": "贴轮廓（不内缩）"}

#: Add one more pass when the leftover at the last pass is below this fraction of the stepover, so
#: the region really gets machined out.
_ALIGN_TOLERANCE = 0.05


@PLANNERS.register
class RasterPlanner(Planner):
    """Parallel scanlines, zigzag or one-way."""

    id: ClassVar[str] = "raster"
    label: ClassVar[str] = "栅格刀路"
    description: ClassVar[str] = "平行扫描线：往复（之字形）或单向（每刀抬刀返回）"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("mode", "走刀模式", K.CHOICE, "zigzag", group="刀路", choices=(
                Choice("zigzag", "往复 Zigzag"),
                Choice("one_way", "单向 One-way"),
            )),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两条刀线的间距"),
            spec("boundary_mode", "边界处理", K.CHOICE, "inset", group="刀路",
                 choices=(
                     Choice("inset", "内缩一个刀具半径"),
                     Choice("none", "贴轮廓（不内缩）"),
                 ),
                 help="内缩保证刀不切出区域；贴轮廓表示刀心走在轮廓线上，会切出区域一圈"),
            spec("stock_allowance_mm", "边界余量", K.FLOAT, 0.0, minimum=0.0, maximum=20.0,
                 step=0.5, unit="mm", group="刀路",
                 help="在轮廓内侧再留一圈余量（精加工前留量）；0 表示切到轮廓",
                 visible_if={"boundary_mode": "inset"}),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="刀路", help="扫描线的行进方向；切宽方向与之垂直"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    ) + MOTION_PARAMETERS

    def plan(self, context: PlanningContext) -> Toolpath:
        mode = str(context.parameters["mode"])
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        offset = self._boundary_offset(context)
        self._warn_if_stepover_too_large(context, stepover)

        boundary = context.boundary
        u_axis = direction_2d(float(context.parameters["direction_deg"]))
        v_axis = np.array([-u_axis[1], u_axis[0]], dtype=np.float64)
        frame = np.column_stack((u_axis, v_axis))
        planar = boundary @ frame

        levels = self._pass_levels(planar[:, 1], stepover, offset)
        passes: list[tuple[float, float, float]] = []
        for level in levels:
            for interval in scanline_intervals(planar, float(level)):
                start = interval.start + offset
                end = interval.end - offset
                if end - start <= 1e-6:
                    continue
                passes.append((start, end, float(level)))
        if not passes:
            raise PlanningError(
                "没有生成任何刀轨：请检查区域尺寸、刀具直径与切宽是否匹配"
            )

        moves: list[Move] = []
        first = self._to_world(passes[0], frame, reverse=False)
        moves.append(context.approach_move_down(context.to_positions(first)[0]))

        previous: np.ndarray | None = None
        for index, (start, end, level) in enumerate(passes):
            reverse = mode == "zigzag" and index % 2 == 1
            planar_points = np.array(
                [[end, level], [start, level]] if reverse else [[start, level], [end, level]],
                dtype=np.float64,
            )
            positions = context.to_positions(planar_points @ frame.T)
            if previous is not None:
                moves.append(
                    context.link_move(previous, positions[0])
                    if mode == "zigzag"
                    else context.rapid_between(previous, positions[0])
                )
            moves.append(context.cut_move(planar_points @ frame.T, pass_index=index,
                                          label=f"第 {index + 1} 刀"))
            previous = positions[-1]

        moves.append(context.retract_move_up(previous))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(context, mode, stepover, len(passes)),
        )

    # -- internal steps ----------------------------------------------------
    @staticmethod
    def _boundary_offset(context: PlanningContext) -> float:
        """Boundary mode plus allowance -> offset of the path from the outline (positive insets)."""

        mode = str(context.parameters.get("boundary_mode", "inset"))
        if mode == "none":
            context.warn(
                "边界处理选了贴轮廓：刀心走在轮廓线上，刀会切出区域外一个刀具半径"
            )
            return 0.0
        allowance = max(0.0, float(context.parameters.get("stock_allowance_mm", 0.0)))
        return context.cutting_radius_mm + allowance

    @staticmethod
    def _to_world(
        item: tuple[float, float, float], frame: np.ndarray, *, reverse: bool
    ) -> np.ndarray:
        start, end, level = item
        planar = np.array([[end, level], [start, level]] if reverse else [[start, level], [end, level]])
        return planar @ frame.T

    @staticmethod
    def _pass_levels(
        v_values: np.ndarray, stepover: float, offset: float
    ) -> np.ndarray:
        """Lay passes across v at the stepover, insetting both ends by the tool footprint radius."""

        v_start = float(v_values.min()) + offset
        v_end = float(v_values.max()) - offset
        if v_end - v_start < -1e-6:
            raise PlanningError(
                f"刀具足迹半径 {offset:g} mm 已经超过区域在该方向上的宽度，"
                "请减小刀具直径或扩大区域"
            )
        count = int(np.floor((v_end - v_start) / stepover + 1e-9)) + 1
        levels = v_start + np.arange(count, dtype=np.float64) * stepover
        if (v_end - float(levels[-1])) > _ALIGN_TOLERANCE * stepover:
            levels = np.append(levels, v_end)
        return levels

    @staticmethod
    def _warn_if_stepover_too_large(context: PlanningContext, stepover: float) -> None:
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，"
                "两刀之间会留下未切除的残余"
            )

    @staticmethod
    def _notes(
        context: PlanningContext, mode: str, stepover: float, pass_count: int
    ) -> tuple[str, ...]:
        direction = float(context.parameters["direction_deg"])
        boundary_mode = str(context.parameters.get("boundary_mode", "inset"))
        boundary = _BOUNDARY_LABELS.get(boundary_mode, _BOUNDARY_LABELS["inset"])
        allowance = float(context.parameters.get("stock_allowance_mm", 0.0))
        allowance_note = f"，边界余量 {allowance:g} mm" if allowance > 0.0 else ""
        return (
            f"{_MODE_LABELS[mode]}走刀，共 {pass_count} 刀，"
            f"切宽 {stepover:g} mm，走刀方向 {direction:g}°",
            f"边界处理：{boundary}（刀具贴壁间隙 R{context.cutting_radius_mm:g} mm）"
            f"{allowance_note}，安全高度 {context.safe_height_mm:g} mm、"
            f"快移 {context.rapid_feed_mm_per_min:g} mm/min",
        )
