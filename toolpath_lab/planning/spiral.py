"""Spiral contouring: one continuous cut per level, stepping in by one stepover per revolution.

Contour leaves a pocket ring by ring: it walks one closed offset, then jumps to the next one (a link
when the rings are nested, a retract when a concave neck split the shape). This strategy walks the same
offsets without ever leaving the cut, because here every revolution *is* the transition: the ring being
followed is blended into the next one as it goes round,

    P(i) = (1 - t_i) * A(i) + t_i * B(i),      t_i = i / N,

so the revolution starts on ring A and ends on ring B. Point i of A and point i of B have to belong to
the same place on the shape, so both rings are resampled to the same number of points by arc length and
the inner ring is rotated onto its point closest to where the revolution started. That is also what lets
the *next* revolution continue from where this one stopped, and what makes a fallback transition as
short as it can be.

Three consequences shape the toolpath, and each is measured by the tests:

- consecutive revolutions sit exactly one stepover apart everywhere: at a given angle the j-th
  revolution is at `d_j + t * stepover`, so the coverage matches ring-by-ring contouring, only without
  a single lift inside a level;
- the **outermost ring is cut in full first** and the innermost one last: a blend merely *touches* the
  outer ring at its start, so a pure blend would leave the band along the wall uncut, and the innermost
  ring is what covers the core the offset chain leaves behind;
- a transition that cannot be blended -- two sibling rings after a concave neck, or levels whose loop
  counts differ -- falls back to the contour behaviour (a link when nested, a retract when not) and the
  notes count them, because there is no continuous way across a neck at cutting depth.

The price of the continuity is one extra revolution (the innermost ring is walked in full even though
the last blend already reached it), so this strategy is a little longer than contouring and much calmer:
no full-width engagement between rings, no lift and no rapid inside a level.
"""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import PlanningContext
from toolpath_lab.planning.contour import _DIRECTION_LABELS, ContourPlanner
from toolpath_lab.planning.coverage import measure_coverage
from toolpath_lab.planning.geometry2d import signed_area
from toolpath_lab.planning.registry import PLANNERS

_EPS = 1e-9

#: Gaps shorter than this are not transitions: after alignment they are the same point on a ring.
_GAP_MM = 1e-6

#: Coverage (fraction) a spiral may lose against ring-by-ring contouring before it steps aside. A spiral
#: always loses its seam, so this is deliberately loose: a shape that really does not suit one loses an
#: order of magnitude more (see `_keep_or_fall_back`).
_FALLBACK_COVERAGE_LOSS = 0.02


@PLANNERS.register
class SpiralPlanner(ContourPlanner):
    """Contour rings blended into one continuous spiral from the wall to the core."""

    id: ClassVar[str] = "spiral"
    label: ClassVar[str] = "螺旋环切"
    description: ClassVar[str] = (
        "同一层从外圈螺旋到中心：一圈进一个切宽，层内不抬刀也不留连接段（凹处分裂时回退）"
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        ring_direction = str(context.parameters["ring_direction"])
        rings = self.sampled_rings(context, stepover, sample_step, ring_direction)
        if not rings:
            raise PlanningError(
                f"螺旋环切没有生成任何刀轨：刀具贴壁间隙 {context.cutting_radius_mm:g} mm "
                "已经超过区域的内切半径，请减小刀具直径或扩大区域"
            )

        moves: list[Move] = []
        previous: NDArray[np.float64] | None = None
        previous_ring: NDArray[np.float64] | None = None
        previous_level = -1
        index = 0
        blended = 0
        linked = 0
        lifted = 0
        reversed_pairs = 0
        total = sum(len(level) for level in rings)
        seen = 0
        for level_index, level in enumerate(rings):
            for ring in level:
                seen += 1
                open_ring = ring[:-1]  # the sampled rings are closed; the blends work on the open ones
                if previous is None:
                    positions = context.to_positions(ring)
                    moves.extend(context.entry_moves(positions[0], positions[1] - positions[0]))
                elif (
                    len(level) == 1
                    and len(rings[previous_level]) == 1
                    and self._is_nested(ring, previous_ring)
                ):
                    # A spiral cannot alternate: blending a ring into a ring that runs the other way
                    # would pair opposite sides of the pocket and leave a band uncut, so the inner ring
                    # is turned to follow the outer one (the notes say when that happened).
                    if not self._same_winding(previous_ring, open_ring):
                        open_ring = open_ring[::-1]
                        reversed_pairs += 1
                    revolution, open_ring = self._spiral_revolution(previous_ring, open_ring)
                    positions = context.to_positions(revolution)
                    blended += 1
                else:
                    # No blend possible: a sibling ring after a concave neck, or a change of loop count.
                    # Start it at its point closest to where the tool is, so the transition -- a link when
                    # the ring is nested, a retract when it is not -- is as short as it can be and never
                    # cuts across the pocket.
                    open_ring = self._align_ring(open_ring, previous[:2])
                    positions = context.to_positions(np.vstack([open_ring, open_ring[:1]]))
                    gap = float(np.linalg.norm(previous[:2] - positions[0][:2]))
                    if gap > _GAP_MM:
                        if self._is_nested(ring, previous_ring):
                            moves.append(context.link_move(previous, positions[0]))
                            linked += 1
                        else:
                            moves.append(context.rapid_between(previous, positions[0]))
                            lifted += 1
                moves.append(
                    Move(
                        MoveKind.CUT,
                        positions,
                        context.feed_mm_per_min,
                        pass_index=index,
                        label=f"第 {index + 1} 圈",
                    )
                )
                previous = positions[-1]
                previous_ring = open_ring
                previous_level = level_index
                index += 1
        moves.append(context.retract_move_up(previous))

        toolpath = Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"螺旋环切：{len(rings)} 个偏置层合成 {index} 圈连续刀路"
                f"（切宽 {stepover:g} mm，采样步长 {sample_step:g} mm，"
                f"环绕向 {_DIRECTION_LABELS[ring_direction]}）",
                (
                    f"最外圈先整圈走完，之后 {blended} 处环间过渡都在切削中完成："
                    "层内不抬刀、不留连接段"
                    if linked == 0 and lifted == 0
                    else f"最外圈先整圈走完，另有 {blended} 处环间过渡在切削中完成；"
                    f"{linked} 处连接进给、{lifted} 处抬刀快移"
                    "（凹形分裂出的兄弟环或环数变化，无法连续过渡）"
                ),
                *(
                    ["螺旋必须同向：交替环绕向在螺旋里跟随最外圈的方向，内圈不再反向"]
                    if reversed_pairs
                    else []
                ),
                f"边界固定内缩一个刀具贴壁间隙（R{context.cutting_radius_mm:g} mm），"
                f"安全高度 {context.safe_height_mm:g} mm、"
                f"快移 {context.rapid_feed_mm_per_min:g} mm/min",
            ),
        )
        return self._keep_or_fall_back(toolpath, context)

    def _keep_or_fall_back(self, spiral: Toolpath, context: PlanningContext) -> Toolpath:
        """Keep the spiral, or return a ring-by-ring contour when the shape does not suit one.

        A spiral is a *single curve whose distance to the wall grows along it*, so it sweeps the band
        between two rings only while the material is machined from **one** front. A thin wall, a U-shaped
        bar or any pocket whose two sides face each other is cut from two fronts at once; ring-by-ring
        contouring advances both of them with every ring, while a spiral reaches the second front at a
        different point of its revolution and leaves the middle of the wall uncut (measured: 73 % against
        95 % coverage on the U shape, 86 % against 99 % on the dumbbell).

        That is a limit of the method, not a bug to tune away, so the planner measures both toolpaths and
        keeps the spiral only when it does not lose more than a couple of points of coverage. Otherwise
        the contour result is returned with a note that says why -- a worse path is never shipped
        silently.
        """

        plain = super().plan(context)
        spiral_coverage = measure_coverage(spiral, context.region, context.tool).ratio
        plain_coverage = measure_coverage(plain, context.region, context.tool).ratio
        if spiral_coverage >= plain_coverage - _FALLBACK_COVERAGE_LOSS:
            return replace(
                spiral,
                notes=spiral.notes
                + (
                    f"螺旋与环切的覆盖率对比：{spiral_coverage * 100:.1f}% vs "
                    f"{plain_coverage * 100:.1f}%（单面进刀的形状两者一致，差的是接缝那一圈）",
                ),
            )
        return replace(
            plain,
            notes=plain.notes
            + (
                f"此形状不适合螺旋：材料两面同时进刀（薄壁 / 凹形），螺旋一圈只从一面进刀，"
                f"覆盖率会从 {plain_coverage * 100:.1f}% 掉到 {spiral_coverage * 100:.1f}%，"
                "已按环切逐圈走",
            ),
        )

    # -- the spiral itself --------------------------------------------------
    @staticmethod
    def _same_winding(first: NDArray[np.float64], second: NDArray[np.float64]) -> bool:
        """Whether two open rings run the same way round (both clockwise or both counter-clockwise)."""

        return (signed_area(first) > 0.0) == (signed_area(second) > 0.0)

    def _spiral_revolution(
        self, outer: NDArray[np.float64], inner: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """One revolution from `outer` to `inner`, plus the inner ring as it is left behind.

        Both rings are resampled to the same number of points (the larger of the two) so that points can
        be paired one to one, and the pairing itself is a **projection**: every point of the outer ring
        is paired with the closest point of the inner one, walking the inner ring monotonically forward.
        Pairing by arc-length *fraction* instead looks equivalent and is not: the two rings are not
        similar figures, so a fraction slides along the shape wherever features differ in length -- on a
        U-shaped pocket it slides by up to 20 mm along the slot wall, the swept chords run sideways, and
        a 7 mm band of the floor is left uncut (73 % coverage instead of 99 %). A projection keeps every
        chord perpendicular to the rings, which is what makes the sweep cover the band between them.
        """

        count = max(outer.shape[0], inner.shape[0])
        start = self._resample_to(outer, count)
        second = self._resample_to(inner, count)
        rotated, partners = self._project(start, second)
        fractions = np.arange(count, dtype=np.float64) / count
        blend = start + fractions[:, None] * (rotated[partners] - start)
        end = rotated[partners[-1]]
        # The next revolution has to begin where this one stopped, so the inner ring is handed back
        # rotated onto that point.
        return np.vstack([blend, end]), np.roll(rotated, -int(partners[-1]), axis=0)

    @staticmethod
    def _project(
        outer: NDArray[np.float64], inner: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.intp]]:
        """Pair every outer point with an inner point, walking the inner ring monotonically forward.

        Returns the inner ring rotated so that index 0 is the partner of `outer[0]`, plus one partner
        index per outer point. The walk only ever moves forward, so the pairing cannot jump across a
        narrow slot and the revolution cannot twist; it is then spread over the whole ring so that the
        seam between two revolutions does not leave the far end of the inner ring unvisited.
        """

        count = outer.shape[0]
        rotated = np.roll(inner, -int(np.argmin(np.linalg.norm(inner - outer[0], axis=1))), axis=0)
        partners = np.zeros(count, dtype=np.intp)
        cursor = 0
        for position in range(count):
            here = outer[position]
            while cursor + 1 < count:
                current = float(np.linalg.norm(rotated[cursor] - here))
                following = float(np.linalg.norm(rotated[cursor + 1] - here))
                if following >= current:
                    break
                cursor += 1
            partners[position] = cursor
        span = int(partners[-1])
        if 0 < span < count - 1:
            partners = (partners * (count - 1) // span).astype(np.intp)
        return rotated, partners

    @staticmethod
    def _align_ring(
        ring: NDArray[np.float64], point: NDArray[np.float64], *, window_fraction: float = 1.0
    ) -> NDArray[np.float64]:
        """The same open ring, started at its point closest to `point` within a window.

        The window matters on concave shapes: in a narrow slot the point closest to the outer ring's
        start can be the *other* side of the slot, and pairing those two would make the revolution run
        across the pocket. A blend therefore only rotates a ring by a small fraction of its length
        (`window_fraction`), while a fallback transition -- which really does want the shortest move --
        searches the whole ring.
        """

        count = ring.shape[0]
        window = max(1, int(round(count * max(0.0, min(window_fraction, 1.0)))))
        candidates = np.arange(-window, window + 1) % count
        distances = np.linalg.norm(ring[candidates] - point, axis=1)
        return np.roll(ring, -int(candidates[int(np.argmin(distances))]), axis=0)

    @staticmethod
    def _resample_to(points: NDArray[np.float64], count: int) -> NDArray[np.float64]:
        """`count` points spaced by arc length along a ring, starting at its first point."""

        ring = points
        if ring.shape[0] > 1 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        closed = np.vstack([ring, ring[:1]])
        lengths = np.linalg.norm(np.diff(closed, axis=0), axis=1)
        cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
        total = float(cumulative[-1])
        if total <= _EPS:
            return np.repeat(ring[:1], count, axis=0)
        fractions = np.arange(count, dtype=np.float64) * (total / count)
        return np.column_stack(
            (
                np.interp(fractions, cumulative, closed[:, 0]),
                np.interp(fractions, cumulative, closed[:, 1]),
            )
        )
