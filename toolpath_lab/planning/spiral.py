"""Continuous outward spirals inside a centred convex region.

Offset edge half-planes by the tool footprint, grow a scaled copy of the
inset boundary, then finish with one complete boundary pass. Exact vertex
angles preserve corners without an interpolated radius lookup table.
"""

from __future__ import annotations

from math import ceil, pi
from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.registry import PLANNERS

_EPS = 1e-9
_MAX_POINTS = 1_000_000


@PLANNERS.register
class SpiralPlanner(Planner):
    """从中心向外的连续螺旋刀路。"""

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋"
    description: ClassVar[str] = "从中心连续向外螺旋，一刀走完，空行程少，适合圆形与方形区域"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两圈螺旋的径向间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路", help="沿螺旋线的点间距"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )

        boundary = context.boundary
        footprint = context.tool.footprint_radius_mm
        edges = np.roll(boundary, -1, axis=0) - boundary
        normals = np.column_stack((edges[:, 1], -edges[:, 0]))
        normals /= np.linalg.norm(normals, axis=1)[:, None]
        limits = np.sum(normals * boundary, axis=1)
        if np.any(boundary @ normals.T > limits + _EPS):
            raise PlanningError("螺旋目前仅支持包含原点的凸区域")
        limits -= footprint
        if np.any(limits <= _EPS):
            raise PlanningError("螺旋未生成任何刀轨：区域过小或刀具过大")

        # Adjacent offset edges intersect at the inset polygon's vertices.
        matrices = np.stack((np.roll(normals, 1, axis=0), normals), axis=1)
        rhs = np.column_stack((np.roll(limits, 1), limits))
        try:
            inset = np.linalg.solve(matrices, rhs[..., None])[..., 0]
        except np.linalg.LinAlgError as error:
            raise PlanningError("螺旋区域包含退化或共线边") from error
        if np.any(inset @ normals.T > limits + 1e-7):
            raise PlanningError("螺旋区域内缩后需要简化轮廓")

        max_radius = float(np.linalg.norm(inset, axis=1).max())
        turns = max(1, int(ceil(max_radius / stepover)))
        theta_total = turns * 2.0 * pi
        theta_end = theta_total + 2.0 * pi
        count = max(4, int(ceil(theta_end * max_radius / sample_step)) + 1)
        if count + (turns + 1) * len(inset) > _MAX_POINTS:
            raise PlanningError("螺旋采样点过多：请增大切宽或采样步长")
        vertex_angles = np.mod(np.arctan2(inset[:, 1], inset[:, 0]), 2.0 * pi)
        corners = (np.arange(turns + 1)[:, None] * (2.0 * pi) + vertex_angles).ravel()
        angles = np.unique(np.concatenate((np.linspace(0.0, theta_end, count), corners)))
        planar = np.empty((len(angles), 2), dtype=np.float64)
        # Chunk ray/edge intersections to bound temporary memory for large paths.
        for start in range(0, len(angles), 4096):
            theta = angles[start:start + 4096]
            directions = np.column_stack((np.cos(theta), np.sin(theta)))
            projection = directions @ normals.T
            distances = np.full_like(projection, np.inf)
            np.divide(limits, projection, out=distances, where=projection > _EPS)
            radii = distances.min(axis=1) * np.minimum(theta / theta_total, 1.0)
            planar[start:start + len(theta)] = directions * radii[:, None]

        # Retain corner samples and subdivide chords to honour the sample step.
        delta = np.diff(planar, axis=0)
        subdivisions = np.maximum(1, np.ceil(np.linalg.norm(delta, axis=1) / sample_step).astype(int))
        if int(subdivisions.sum()) + 1 > _MAX_POINTS:
            raise PlanningError("螺旋采样点过多：请增大切宽或采样步长")
        indices = np.repeat(np.arange(len(delta)), subdivisions)
        starts = np.repeat(np.cumsum(subdivisions) - subdivisions, subdivisions)
        fractions = (np.arange(len(indices)) - starts) / subdivisions[indices]
        planar = np.vstack((planar[indices] + fractions[:, None] * delta[indices], planar[-1]))
        positions = context.to_positions(planar)
        if stepover > 2.0 * footprint:
            context.warn("切宽大于刀具足迹直径，可能留下未加工区域")

        moves = [
            context.approach_move_down(positions[0]),
            Move(
                MoveKind.CUT,
                positions,
                context.feed_mm_per_min,
                pass_index=0,
                label="连续螺旋",
            ),
            context.retract_move_up(positions[-1]),
        ]
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"连续螺旋：{turns} 圈，另加一圈边界收尾，"
                f"切宽 {stepover:g} mm，外圈贴合区域边界",
                f"边界内缩一个刀具足迹半径（R{footprint:g} mm）",
            ),
        )
