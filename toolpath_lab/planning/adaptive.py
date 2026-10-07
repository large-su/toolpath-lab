"""Adaptive contouring: tighten the stepover automatically until the coverage target is met.

The stepover of a contour path decides whether rework is needed -- a stepover larger than the tool
diameter leaves residual strips between passes, and the coverage analysis can measure exactly that.
This strategy connects the two: plan once with the requested stepover, measure the coverage, and if
the target is not met tighten by a factor and plan again, a bounded number of rounds, returning the
round with the **best value for money**.

The rules (each pinned by `tests/test_adaptive.py`):

- Only keep tightening while the coverage **actually improves**. Coverage is not monotonic in the
  stepover: how much is left depends on the tool geometry (a round tool cannot cut a sharp corner),
  and once that limit is reached, tightening only wastes passes. Measured on the dumbbell shape: a
  2 mm stepover gives 99.75%, tightening to 1 mm *drops* to 92.96% (the inner rings become thin
  enough to be filtered out by the minimum ring area).
- Also watch the **machining time**: tightening buys coverage by walking more rings, at a linear
  cost. Measured on an 80 mm square with D6: 6 mm -> 4.2 mm costs 34% more time for +0.55 points
  (worth it); 4.2 mm -> 2.94 mm costs 84% more for +0.02 points (not worth it). Hence the "time
  limit" parameter: by default no more than twice the first round, and a round beyond it is left out
  of the candidates, with a message saying how much time the target would cost. Every round's
  stepover, coverage, ring count, cutting length and time go into the notes, so the value-for-money
  curve can simply be read.
- If the target cannot be reached, say so: return the best round within the budget and use warnings
  to distinguish "blocked by the time limit", "tightening no longer helps" and "out of rounds".
- Rounds, tightening factor, stepover floor and time limit are all parameters, adjustable in the UI;
  there is no unbounded loop.

It subclasses the contour strategy and only overrides plan(), adding five parameters to the contour
parameter set -- "a strategy is one class", and here even the toolpath logic is reused.
"""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning.base import PlanningContext
from toolpath_lab.planning.contour import ContourPlanner
from toolpath_lab.planning.coverage import Coverage, measure_coverage
from toolpath_lab.planning.registry import PLANNERS

#: A coverage gain below this (ratio, 0.0005 = 0.05 percentage points) counts as "no better".
_MIN_IMPROVEMENT = 5e-4

#: One measured round: stepover, coverage ratio, ring count, cutting length, machining time.
Round = tuple[float, float, int, float, float]


@PLANNERS.register
class AdaptiveContourPlanner(ContourPlanner):
    """Plan with the requested stepover, then tighten while it is worth it."""

    id: ClassVar[str] = "adaptive_contour"
    label: ClassVar[str] = "自适应环切"
    description: ClassVar[str] = (
        "环切 + 覆盖率闭环：切宽自动收紧到覆盖率达标，同时守住工时上限，到不了就如实说明"
    )
    parameters: ClassVar[ParameterSet] = ContourPlanner.parameters + ParameterSet(
        (
            spec("coverage_target", "目标覆盖率", K.FLOAT, 99.5, minimum=50.0,
                 maximum=100.0, step=0.5, unit="%", group="刀路",
                 help="低于它就收窄切宽重算；圆刀切不到尖角，方形大约到 99.9% 就是极限"),
            spec("max_time_ratio", "工时上限", K.FLOAT, 2.0, minimum=1.0, maximum=10.0,
                 step=0.1, unit="×", group="刀路",
                 help="收紧后的工时不超过第一次的这么多倍；超出就不采用那一轮"),
            spec("max_rounds", "最多重算", K.INT, 3, minimum=0, maximum=8, group="刀路",
                 help="最多再收紧几次；每轮都会多花一点时间"),
            spec("stepover_factor", "收紧系数", K.FLOAT, 0.7, minimum=0.3, maximum=0.95,
                 step=0.05, group="刀路", help="每轮把切宽乘以这个系数"),
            spec("min_stepover_mm", "切宽下限", K.FLOAT, 1.0, minimum=0.2, maximum=20.0,
                 step=0.1, unit="mm", group="刀路",
                 help="再小也不收：切宽太小只会让环数暴涨"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        target = float(context.parameters["coverage_target"]) / 100.0
        rounds = int(context.parameters["max_rounds"])
        factor = float(context.parameters["stepover_factor"])
        budget_factor = float(context.parameters["max_time_ratio"])
        floor = self.require_positive(
            float(context.parameters["min_stepover_mm"]), "切宽下限 min_stepover_mm"
        )
        if not 0.0 < factor < 1.0:
            raise PlanningError(f"收紧系数必须在 0 与 1 之间（收到 {factor!r}）")
        if budget_factor < 1.0:
            raise PlanningError(f"工时上限不能小于首轮（收到 {budget_factor!r}）")
        start = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )

        stepover = max(start, floor)
        history: list[Round] = []
        best: tuple[float, Toolpath, Coverage, float] | None = None
        blocked: tuple[float, float] | None = None
        plateau = False
        start_time = 0.0
        budget = 0.0
        for attempt in range(rounds + 1):
            toolpath = super().plan(
                replace(context, parameters={**context.parameters, "stepover_mm": stepover})
            )
            coverage = measure_coverage(toolpath, context.region, context.tool)
            spent = toolpath.estimated_time_s
            if not history:
                start_time = spent
                budget = start_time * budget_factor
            history.append(
                (stepover, coverage.ratio, toolpath.pass_count, toolpath.cut_length_mm, spent)
            )
            within_budget = spent <= budget + 1e-9
            if within_budget and (best is None or coverage.ratio > best[2].ratio):
                best = (stepover, toolpath, coverage, spent)
            if not within_budget and blocked is None:
                blocked = (stepover, spent)
            if coverage.ratio >= target:
                break
            if not within_budget:
                break  # already past the time limit, tightening only gets more expensive
            # Test "no longer improving" before the round limit: when the target is out of reach,
            # "tightening no longer helps" is far more useful than "out of rounds".
            if len(history) >= 2 and coverage.ratio <= history[-2][1] + _MIN_IMPROVEMENT:
                plateau = True
                break
            if attempt >= rounds:
                break
            tighter = max(floor, stepover * factor)
            if tighter >= stepover - 1e-9:
                break
            stepover = tighter

        assert best is not None  # the budget is a multiple of the first round, so it is eligible
        stepover, toolpath, coverage, spent = best
        achieved = coverage.ratio >= target
        if not achieved:
            if blocked is not None:
                blocked_stepover, blocked_time = blocked
                context.warn(
                    f"自适应环切：要达标得把工时从 {start_time:.1f} s 提到 {blocked_time:.1f} s"
                    f"（首轮的 {blocked_time / start_time:.2f} 倍，切宽 {blocked_stepover:g} mm），"
                    f"超过工时上限 {budget_factor:g} 倍，所以保留的是切宽 {stepover:g} mm、"
                    f"覆盖率 {coverage.ratio * 100:.2f}% 的那次；愿意多花时间就把「工时上限」调大。"
                )
            elif plateau:
                context.warn(
                    f"自适应环切：目标覆盖率 {target * 100:g}% 达不到——切宽收到 {stepover:g} mm "
                    f"后覆盖率不再提升（{coverage.ratio * 100:.2f}%）。这些残留多半是比刀具还窄的"
                    "角落（尖角、细颈），换更小的切宽也没用。"
                )
            else:
                context.warn(
                    f"自适应环切：共算了 {len(history)} 次（最细到切宽 "
                    f"{min(item[0] for item in history):g} mm）仍未达到目标覆盖率 "
                    f"{target * 100:g}%（现在 {coverage.ratio * 100:.2f}%）；可以放宽目标、"
                    "增加重算轮数，或者换更小的刀具。"
                )

        summary = (
            f"自适应环切：目标覆盖率 {target * 100:g}%，切宽 {start:g} → {stepover:g} mm"
            f"（共算 {len(history)} 次{'，已达标' if achieved else '，未达标'}），覆盖率 "
            f"{history[0][1] * 100:.2f}% → {coverage.ratio * 100:.2f}%，环数 {history[0][2]} → "
            f"{toolpath.pass_count}，工时 {start_time:.1f} → {spent:.1f} s"
            f"（首轮的 {spent / start_time:.2f} 倍）"
        )
        detail = "；".join(
            f"{value:g} mm → {ratio * 100:.2f}%（{rings} 环，{length:.0f} mm，{seconds:.1f} s）"
            for value, ratio, rings, length, seconds in history
        )
        return replace(
            toolpath,
            planner=self.id,
            planner_label=self.label,
            notes=(summary, f"各轮实测：{detail}") + toolpath.notes,
        )
