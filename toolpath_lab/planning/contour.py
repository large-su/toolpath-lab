"""Nested contour finishing for convex regions and parameterized height fields."""

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import Choice, ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.contour_geometry import MAX_PATH_POINTS, offset_rings, sample_ring
from toolpath_lab.planning.registry import PLANNERS


def contour_parameters():
    """Use one parameter contract for contour and continuous spiral planning."""
    return ParameterSet((
        spec("stepover_mm", "切宽 ae", K.FLOAT, 3.0, minimum=0.5, maximum=50,
             step=0.5, unit="mm", group="刀路", help="向内偏置间距；不保证严格等残留高度"),
        spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.2, maximum=10,
             step=0.2, unit="mm", group="刀路", help="曲面会根据特征尺度进一步加密；最多 10 万刀点"),
        spec("winding", "环绕方向", K.CHOICE, "ccw", group="刀路", choices=(
            Choice("ccw", "逆时针"), Choice("cw", "顺时针")),
             help="只定义观察方向，不自动判定顺铣或逆铣"),
        spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10,
             maximum=10000, step=50, unit="mm/min", group="刀路"),
    ))


def prepare_rings(context):
    """Share safe offset inputs and warnings without changing existing planners."""
    step = float(context.parameters["stepover_mm"])
    if step > context.tool.diameter_mm:
        context.warn("切宽大于刀具直径，环间可能留有未切除区域")
    rings = offset_rings(context.boundary, context.tool.footprint_radius_mm, step)
    return rings, float(context.parameters["sample_step_mm"]), context.parameters["winding"] == "cw"


def contour_cut(context, planar, index, label):
    """Lift the full sampled polygon onto the surface and preserve playback vertices."""
    planar = context.sample_cut_points(planar)
    if len(planar) > MAX_PATH_POINTS:
        raise PlanningError("曲面加密刀点超过 10 万，请增大采样步长或缩小曲面区域")
    return Move(MoveKind.CUT, context.to_positions(planar), context.feed_mm_per_min,
                pass_index=index, label=label, preserve_vertices=True)


@PLANNERS.register
class ContourPlanner(Planner):
    """Cut closed, constant-offset rings from the outside inward."""
    id: ClassVar[str] = "contour"
    label: ClassVar[str] = "环切刀路"
    description: ClassVar[str] = "凸选区逐圈向内；曲面贴面采样、圈间安全抬刀；不支持凹选区"
    parameters: ClassVar[ParameterSet] = contour_parameters()

    def plan(self, context: PlanningContext) -> Toolpath:
        rings, sample, clockwise = prepare_rings(context)
        moves, previous, point_count = [], None, 0
        for index, ring in enumerate(rings):
            cut = contour_cut(context, sample_ring(ring, sample, clockwise), index, f"第 {index + 1} 环")
            point_count += len(cut.points)
            if point_count > MAX_PATH_POINTS:
                raise PlanningError("总刀点超过 10 万，请增大切宽/采样步长或缩小区域")
            moves.append(context.approach_move_down(cut.points[0]) if previous is None
                         else context.link_move(previous, cut.points[0]))
            moves.append(cut)
            previous = cut.points[-1]
        # Close the degenerate center left when the last nonzero offset collapses.
        center = rings[-1].mean(axis=0)
        core = contour_cut(context, np.vstack((previous[:2], center)), len(rings), "中心收尾")
        if point_count + len(core.points) > MAX_PATH_POINTS:
            raise PlanningError("总刀点超过 10 万，请增大切宽/采样步长或缩小区域")
        moves.append(core)
        moves.append(context.retract_move_up(core.points[-1]))
        return Toolpath(tuple(moves), self.id, self.label,
                        ("凸选区环切，中心收尾；曲面圈间使用全局安全高度",),
                        {"contour": {"ring_count": len(rings), "winding": context.parameters["winding"],
                                     "boundary_offset_mm": context.tool.footprint_radius_mm}})
