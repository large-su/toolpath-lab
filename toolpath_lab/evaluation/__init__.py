"""刀路评价层（新增功能）。

把"刀路 → 仿真 → 指标 → 评分"这条链路收在一个包里：

    from toolpath_lab.evaluation import evaluate_strategies, format_table

    report = evaluate_strategies(tool=tool, region=region, axial_depth_mm=1.0)
    print(format_table(report))

设计原则：评价层只依赖 core / planning / simulation 的公开接口，不反向被它们依赖，
因此它既是库函数，也是 /api/evaluate 接口与 examples/evaluate_strategies.py 的实现。
"""

from toolpath_lab.evaluation.compare import (
    DEFAULT_WEIGHTS,
    ComparisonReport,
    StrategyScore,
    evaluate_strategies,
    format_table,
    registered_default_candidates,
    score_toolpath,
)
from toolpath_lab.evaluation.defaults import (
    DEFAULT_OVERLAP,
    default_candidates,
    suggest_stepover,
)

__all__ = [
    "DEFAULT_OVERLAP",
    "DEFAULT_WEIGHTS",
    "ComparisonReport",
    "StrategyScore",
    "default_candidates",
    "evaluate_strategies",
    "format_table",
    "registered_default_candidates",
    "score_toolpath",
    "suggest_stepover",
]
