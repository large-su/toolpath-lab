"""Blend nested convex offsets into a genuinely continuous inward spiral.

The outer perimeter is cut first, then each revolution gradually transitions
to the next offset. Inspiration and limits are recorded in docs/contour-spiral.md.
"""

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import ParameterSet
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.contour import contour_cut, contour_parameters, prepare_rings
from toolpath_lab.planning.contour_geometry import MAX_PATH_POINTS, sample_ring, spiral_turn
from toolpath_lab.planning.registry import PLANNERS


@PLANNERS.register
class SpiralPlanner(Planner):
    """One finishing CUT with no internal rapid or ring-to-ring link moves."""
    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "连续螺旋刀路"
    description: ClassVar[str] = "凸选区外圈向中心连续渐进，切削中不抬刀；独立三轴策略"
    parameters: ClassVar[ParameterSet] = contour_parameters()

    def plan(self, context: PlanningContext) -> Toolpath:
        rings, sample, clockwise = prepare_rings(context)
        pieces = [sample_ring(rings[0], sample, clockwise)]
        point_count = len(pieces[0])
        for outer, inner in zip(rings[:-1], rings[1:]):
            turn = spiral_turn(outer, inner, sample, clockwise)[1:]
            point_count += len(turn)
            if point_count > MAX_PATH_POINTS:
                raise PlanningError("螺旋总刀点超过 10 万，请增大切宽/采样步长或缩小区域")
            pieces.append(turn)
        if len(rings) > 1:
            pieces.append(sample_ring(rings[-1], sample, clockwise)[1:])
        pieces.append(rings[-1].mean(axis=0).reshape(1, 2))
        planar = np.vstack(pieces)
        # Repeated seams must not introduce zero-length motion or playback artifacts.
        keep = np.concatenate(([True], np.linalg.norm(np.diff(planar, axis=0), axis=1) > 1e-9))
        cut = contour_cut(context, planar[keep], 0, "外圈→连续螺旋→中心")
        moves = (context.approach_move_down(cut.points[0]), cut,
                 context.retract_move_up(cut.points[-1]))
        return Toolpath(moves, self.id, self.label,
                        ("外圈完整走一圈，再逐圈连续内收，内圈闭合并到中心收尾",
                         "过渡为嵌套凸轮廓插值，非严格等距或自动避碰；曲面逐点贴面"),
                        {"contour": {"ring_count": len(rings), "winding": context.parameters["winding"],
                                     "boundary_offset_mm": context.tool.footprint_radius_mm},
                         "spiral": {"continuous_cut": True, "transition_count": len(rings) - 1}})
