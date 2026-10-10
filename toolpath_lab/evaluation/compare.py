"""刀路质量评价与多策略对比（新增功能，本分支的核心创新点）。

传统做法是"选一条刀路，看长度和时间"。但长度短不等于加工合格：切宽开大了会留残余，
切宽开小了工时翻倍，抬刀多则接刀痕多。于是这里把三件事拼成一个可比较的分数：

======================  ==================================================
维度                    取值方式
======================  ==================================================
质量 Quality            覆盖率（材料切除仿真给出）× 100
效率 Efficiency         最快策略的工时 / 本策略工时 × 100（相对效率）
行程 Air                主切削长度 / 总长度 × 100（抬刀与连接越少越高）
======================  ==================================================

总分 = 0.5·质量 + 0.3·效率 + 0.2·行程（权重可传参，默认值在本文档与 PPT 中说明）。

这份评价不是"拍脑袋的加权"，它把工程经验显式化：
- 覆盖率直接来自 Z-map 仿真，是"加工到位没有"的硬指标；
- 效率用相对值而不是绝对值，避免不同工况下量纲不可比；
- 行程占比惩罚"抬刀多、空跑多"的策略——这正是往复与单向、环切与螺旋的本质差别。

把权重与仿真参数一起写进结果（``weighting`` / ``simulation``），
因此任何一次比较都是可复现的：换权重只需重新调用，不需要重跑刀路设计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import MoveKind, Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.evaluation.defaults import default_candidates
from toolpath_lab.planning.registry import PLANNERS
from toolpath_lab.planning.service import run_plan
from toolpath_lab.simulation.removal import simulate_removal

#: 默认权重：质量 / 效率 / 行程。三者之和必须为 1。
DEFAULT_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)


@dataclass(frozen=True, slots=True)
class StrategyScore:
    """一条候选策略的评分明细。"""

    planner_id: str
    planner_label: str
    parameters: Mapping[str, Any]
    statistics: Mapping[str, Any]
    removal_metrics: Mapping[str, float]
    quality_score: float
    efficiency_score: float
    air_score: float
    total_score: float
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "planner_id": self.planner_id,
            "planner_label": self.planner_label,
            "parameters": dict(self.parameters),
            "statistics": {key: round(float(value), 4) for key, value in self.statistics.items()},
            "removal": {key: round(float(value), 6) for key, value in self.removal_metrics.items()},
            "scores": {
                "quality": round(self.quality_score, 2),
                "efficiency": round(self.efficiency_score, 2),
                "air": round(self.air_score, 2),
                "total": round(self.total_score, 2),
            },
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """一次多策略对比的完整结果（已按总分排序）。"""

    entries: tuple[StrategyScore, ...] = field(default_factory=tuple)
    tool: Mapping[str, Any] = field(default_factory=dict)
    region: Mapping[str, Any] = field(default_factory=dict)
    weighting: Mapping[str, float] = field(default_factory=dict)
    simulation: Mapping[str, float] = field(default_factory=dict)

    @property
    def best(self) -> StrategyScore:
        if not self.entries:
            raise ParameterError("对比结果为空")
        return self.entries[0]

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool": dict(self.tool),
            "region": dict(self.region),
            "weighting": dict(self.weighting),
            "simulation": dict(self.simulation),
            "best_planner_id": self.best.planner_id,
            "entries": [entry.to_payload() for entry in self.entries],
        }


def _air_length(toolpath: Toolpath) -> float:
    """非主切削行程：抬刀横移（rapid）+ 刀/环之间的连接进给（link）。"""

    return float(
        sum(
            move.length_mm
            for move in toolpath.moves
            if move.kind in (MoveKind.RAPID, MoveKind.LINK)
        )
    )


def _retract_count(toolpath: Toolpath) -> int:
    return int(sum(1 for move in toolpath.moves if move.kind is MoveKind.RAPID))


def score_toolpath(
    toolpath: Toolpath,
    *,
    coverage_ratio: float,
    total_time_s: float,
    best_time_s: float,
    weights: tuple[float, float, float],
) -> tuple[float, float, float, float]:
    """按质量 / 效率 / 行程三项给出分数与总分。"""

    quality = max(0.0, min(1.0, coverage_ratio)) * 100.0
    efficiency = 100.0 * min(1.0, best_time_s / max(total_time_s, 1e-9))
    air = 100.0 * toolpath.cut_length_mm / max(toolpath.total_length_mm, 1e-9)
    total = weights[0] * quality + weights[1] * efficiency + weights[2] * air
    return quality, efficiency, air, total


def evaluate_strategies(
    *,
    tool: Tool,
    region: RegionShape,
    candidates: Sequence[tuple[str, Mapping[str, Any]]] | None = None,
    resolution_mm: float = 1.0,
    axial_depth_mm: float = 1.0,
    weights: tuple[float, float, float] = DEFAULT_WEIGHTS,
    with_removal: bool = True,
) -> ComparisonReport:
    """对同一把刀、同一块区域跑若干策略，给出可排序的评价表。

    `candidates` 缺省时用 :func:`toolpath_lab.evaluation.defaults.default_candidates`
    给出的常用组合（往复 / 单向 / 螺旋 / 已注册的环切）。
    """

    if len(weights) != 3 or abs(sum(weights) - 1.0) > 1e-9 or min(weights) < 0:
        raise ParameterError("权重必须是三个非负数且之和为 1，例如 (0.5, 0.3, 0.2)")
    if resolution_mm <= 0 or axial_depth_mm <= 0:
        raise ParameterError("仿真分辨率与轴向切深必须是正数")

    planned: list[tuple[str, Mapping[str, Any], Toolpath, tuple[str, ...]]] = []
    for planner_id, parameters in (candidates if candidates is not None else default_candidates()):
        PLANNERS.get(planner_id)  # 未注册时立刻报错，错误信息里会列出可用策略
        outcome = run_plan(planner_id=planner_id, tool=tool, region=region,
                           parameters=dict(parameters))
        planned.append((planner_id, dict(parameters), outcome.toolpath, outcome.warnings))
    if not planned:
        raise ParameterError("至少需要一个候选策略")

    times = [toolpath.estimated_time_s for _, _, toolpath, _ in planned]
    best_time = min(times)

    entries: list[StrategyScore] = []
    for planner_id, parameters, toolpath, warnings in planned:
        if with_removal:
            report = simulate_removal(
                toolpath, tool, region,
                resolution_mm=resolution_mm, axial_depth_mm=axial_depth_mm,
            )
            metrics = dict(report.metrics)
            coverage = float(metrics["coverage_ratio"])
        else:  # 只要效率时不做仿真，覆盖率按 100% 记（并在结果里注明）
            metrics = {}
            coverage = 1.0
        quality, efficiency, air, total = score_toolpath(
            toolpath,
            coverage_ratio=coverage,
            total_time_s=toolpath.estimated_time_s,
            best_time_s=best_time,
            weights=weights,
        )
        statistics = dict(toolpath.statistics())
        statistics["air_length_mm"] = _air_length(toolpath)
        statistics["retract_count"] = _retract_count(toolpath)
        entries.append(
            StrategyScore(
                planner_id=planner_id,
                planner_label=PLANNERS.get(planner_id).label,
                parameters=parameters,
                statistics=statistics,
                removal_metrics=metrics,
                quality_score=quality,
                efficiency_score=efficiency,
                air_score=air,
                total_score=total,
                warnings=warnings,
            )
        )

    entries.sort(key=lambda entry: (-entry.total_score, entry.planner_id, str(entry.parameters)))
    return ComparisonReport(
        entries=tuple(entries),
        tool=tool.describe(),
        region=region.describe(),
        weighting={"quality": weights[0], "efficiency": weights[1], "air": weights[2]},
        simulation={"resolution_mm": resolution_mm, "axial_depth_mm": axial_depth_mm,
                    "with_removal": with_removal},
    )


def registered_default_candidates() -> list[tuple[str, dict[str, Any]]]:
    """当前注册表里能直接跑的默认候选（供脚本与接口使用）。"""

    return [(planner_id, dict(parameters)) for planner_id, parameters in default_candidates()
            if planner_id in PLANNERS]


def format_table(report: ComparisonReport) -> str:
    """把评价表排成等宽文本，便于命令行查看与粘贴进文档。"""

    header = ("策略", "切宽", "覆盖%", "工时s", "抬刀", "切削mm", "空程mm", "质量", "效率", "行程", "总分")
    rows = [header]
    for entry in report.entries:
        rows.append(
            (
                entry.planner_label,
                f"{entry.parameters.get('stepover_mm', '-')}",
                f"{entry.removal_metrics.get('coverage_ratio', 1.0) * 100:.1f}",
                f"{entry.statistics['estimated_time_s']:.1f}",
                f"{int(entry.statistics['retract_count'])}",
                f"{entry.statistics['cut_length_mm']:.0f}",
                f"{entry.statistics['air_length_mm']:.0f}",
                f"{entry.quality_score:.1f}",
                f"{entry.efficiency_score:.1f}",
                f"{entry.air_score:.1f}",
                f"{entry.total_score:.1f}",
            )
        )
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(header))]
    lines = [
        "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) for row in rows
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)
