"""自适应环切：把切宽自动收紧到覆盖率达标。

环切的切宽是"要不要返工"的关键——切宽大于刀具直径会在两刀之间留下残余条带，而覆盖率分析
正好能量出这件事。这个策略就是把两者接起来：先用请求里的切宽算一遍、量一下覆盖率，没到目标
就按系数收窄重算，最多几轮，最后返回**性价比最好**的那一次。

几条规矩（`tests/test_adaptive.py` 逐条钉住）：

- 只在覆盖率**确实提升**时才继续收紧。覆盖率并不是切宽越小越高：残留的多少由刀具几何决定
  （圆刀切不到尖角），到这个极限之后再收窄只是白走刀。实测哑铃形切宽 2 mm 时 99.75%，
  收到 1 mm 反而降到 92.96%（内圈细到被最小环面积过滤掉了）。
- 还要看**工时**：收窄切宽靠的是多走几圈，代价是线性的。实测方形 80、D6 从切宽 6 mm 收到
  4.2 mm，工时涨 34% 换 +0.55 个点（划算）；再收到 2.94 mm，工时涨 84% 只换 +0.02 个点
  （不划算）。所以有「工时上限」这个参数：默认不超过首轮的 2 倍，超出就把那一轮排除在候选
  之外，并说明"要达标得多花多少时间"。每一轮的切宽、覆盖率、环数、切削长度与工时都写进
  notes，这条性价比曲线可以直接读。
- 到不了目标就如实说：返回预算内最好的一次，并用 warnings 说明是"工时上限挡住了"、
  "再收也不提升"还是"轮数不够"。
- 轮数、收紧系数、切宽下限、工时上限都是参数，界面上可调；不存在无界循环。

它继承环切策略、只重写 plan()，参数集在环切的基础上再加五项——"加一个策略就是一个类"，
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

#: 一轮实测：切宽、覆盖率、环数、切削长度、工时。
Round = tuple[float, float, int, float, float]


@PLANNERS.register
class AdaptiveContourPlanner(ContourPlanner):
    """先按请求的切宽算一遍，再按需收紧，直到覆盖率达标或者不再划算。"""

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
                break  # 已经超出工时上限，再收只会更贵
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

        assert best is not None  # 预算是首轮的倍数，所以首轮必在候选里
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
