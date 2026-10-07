"""自适应环切：把切宽自动收紧到覆盖率达标。

环切的切宽是"要不要返工"的关键——切宽大于刀具直径会在两刀之间留下残余条带，而覆盖率分析
正好能量出这件事。这个策略就是把两者接起来：先用请求里的切宽算一遍、量一下覆盖率，没到目标
就按系数收窄重算，最多几轮，最后返回**最好**的那一次。

几条规矩（`tests/test_adaptive.py` 逐条钉住）：

- 只在覆盖率**确实提升**时才继续收紧。覆盖率并不是切宽越小越高：残留的多少由刀具几何决定
  （圆刀切不到尖角），到这个极限之后再收窄只是白走刀。实测哑铃形切宽 2 mm 时 99.75%，
  收到 1 mm 反而降到 92.96%（内圈细到被最小环面积过滤掉了）。
- 到不了目标就如实说：返回最好的一次，并用 warnings 说明"收到多少就不再提升"，
  让用户知道这是几何极限、不是漏算。
- 轮数、收紧系数、切宽下限都是参数，界面上可调；不存在无界循环。

它继承环切策略、只重写 plan()，参数集在环切的基础上再加四个——"加一个策略就是一个类"，
这里连刀路逻辑都是复用的。
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

#: 覆盖率提升小于这个值（比例，0.0005 = 0.05 个百分点）就认为"再收也不会更好"。
_MIN_IMPROVEMENT = 5e-4


@PLANNERS.register
class AdaptiveContourPlanner(ContourPlanner):
    """先按请求的切宽算一遍，再按需收紧，直到覆盖率达标或不再提升。"""

    id: ClassVar[str] = "adaptive_contour"
    label: ClassVar[str] = "自适应环切"
    description: ClassVar[str] = "环切 + 覆盖率闭环：切宽自动收紧到覆盖率达标，到不了就如实说明"
    parameters: ClassVar[ParameterSet] = ContourPlanner.parameters + ParameterSet(
        (
            spec("coverage_target", "目标覆盖率", K.FLOAT, 99.5, minimum=50.0,
                 maximum=100.0, step=0.5, unit="%", group="刀路",
                 help="低于它就收窄切宽重算；圆刀切不到尖角，方形大约到 99.9% 就是极限"),
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
        floor = self.require_positive(
            float(context.parameters["min_stepover_mm"]), "切宽下限 min_stepover_mm"
        )
        if not 0.0 < factor < 1.0:
            raise PlanningError(f"收紧系数必须在 0 与 1 之间（收到 {factor!r}）")
        start = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )

        stepover = max(start, floor)
        history: list[tuple[float, float, int]] = []
        best: tuple[float, Toolpath, Coverage] | None = None
        plateau = False
        for attempt in range(rounds + 1):
            toolpath = super().plan(
                replace(context, parameters={**context.parameters, "stepover_mm": stepover})
            )
            coverage = measure_coverage(toolpath, context.region, context.tool)
            history.append((stepover, coverage.ratio, toolpath.pass_count))
            if best is None or coverage.ratio > best[2].ratio:
                best = (stepover, toolpath, coverage)
            if coverage.ratio >= target:
                break
            # 先判"不再提升"再判轮数：到不了目标时，"再收也没用"比"轮数用完"有用得多。
            if len(history) >= 2 and coverage.ratio <= history[-2][1] + _MIN_IMPROVEMENT:
                plateau = True
                break
            if attempt >= rounds:
                break
            tighter = max(floor, stepover * factor)
            if tighter >= stepover - 1e-9:
                break
            stepover = tighter

        assert best is not None  # rounds >= 0，至少算过一遍
        stepover, toolpath, coverage = best
        achieved = coverage.ratio >= target
        if not achieved:
            if plateau:
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
            f"{toolpath.pass_count}"
        )
        detail = "；".join(
            f"{value:g} mm → {ratio * 100:.2f}%（{rings} 环）" for value, ratio, rings in history
        )
        return replace(
            toolpath,
            planner=self.id,
            planner_label=self.label,
            notes=(summary, f"各轮实测：{detail}") + toolpath.notes,
        )
