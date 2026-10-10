"""示例：多策略对比与材料切除仿真（本分支新增功能）。

用法：

    python examples/evaluate_strategies.py
    python examples/evaluate_strategies.py --region circle --diameter 80 --stepover 6
    python examples/evaluate_strategies.py --tool ball --depth 0.2

它会针对同一把刀、同一块区域跑若干刀路策略，逐条做材料切除仿真，
打印一张"质量 / 效率 / 行程"的评价表，并给出推荐策略。
"""

from __future__ import annotations

import argparse

from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.evaluation import (
    default_candidates,
    evaluate_strategies,
    format_table,
    suggest_stepover,
)


def build_tool(args: argparse.Namespace) -> Tool:
    return Tool(
        kind=ToolKind(args.tool),
        diameter_mm=args.diameter,
        length_mm=args.length,
        corner_radius_mm=args.corner,
    )


def build_region_from_args(args: argparse.Namespace):
    if args.region == "circle":
        return build_region("circle", {"diameter_mm": args.diameter_region})
    if args.region == "rounded_rect":
        return build_region("rounded_rect", {
            "side_x_mm": args.diameter_region,
            "side_y_mm": args.diameter_region * 0.75,
            "corner_radius_mm": args.diameter_region * 0.1,
        })
    return build_region("square", {"side_mm": args.diameter_region})


def main() -> None:
    parser = argparse.ArgumentParser(description="刀路策略对比与材料切除仿真")
    parser.add_argument("--tool", choices=["flat", "ball", "bull"], default="flat")
    parser.add_argument("--diameter", type=float, default=10.0, help="刀具直径 mm")
    parser.add_argument("--length", type=float, default=40.0, help="刀具长度 mm")
    parser.add_argument("--corner", type=float, default=2.0, help="圆鼻刀圆角半径 mm")
    parser.add_argument("--region", choices=["square", "circle", "rounded_rect"], default="square")
    parser.add_argument("--diameter-region", dest="diameter_region", type=float, default=60.0,
                        help="区域尺寸 mm（方形为边长，圆形为直径）")
    parser.add_argument("--stepover", type=float, default=None, help="切宽 mm（缺省按刀具自动建议）")
    parser.add_argument("--depth", type=float, default=1.0, help="轴向切深 ap mm")
    parser.add_argument("--resolution", type=float, default=1.0, help="仿真网格 mm")
    parser.add_argument("--feed", type=float, default=800.0, help="进给速度 mm/min")
    args = parser.parse_args()

    tool = build_tool(args)
    region = build_region_from_args(args)
    stepover = args.stepover if args.stepover is not None else suggest_stepover(tool, args.depth)

    print(f"刀具：{tool.describe()}")
    print(f"区域：{region.id} 面积 {region.describe()['area_mm2']:.0f} mm²")
    print(f"轴向切深 ap = {args.depth:g} mm，咬入半径 "
          f"{tool.cutting_footprint_radius_mm(args.depth):.2f} mm，建议切宽 {stepover:g} mm")
    print()

    report = evaluate_strategies(
        tool=tool,
        region=region,
        candidates=default_candidates(stepover_mm=stepover, feed_mm_per_min=args.feed),
        resolution_mm=args.resolution,
        axial_depth_mm=args.depth,
    )
    print(format_table(report))
    print()
    best = report.best
    print(f"推荐：{best.planner_label}  综合分 {best.total_score:.1f}"
          f"（覆盖率 {best.removal_metrics['coverage_ratio'] * 100:.1f}%，"
          f"工时 {best.statistics['estimated_time_s']:.1f} s，"
          f"抬刀 {int(best.statistics['retract_count'])} 次）")
    for entry in report.entries:
        for warning in entry.warnings:
            print(f"  提醒（{entry.planner_label}）：{warning}")


if __name__ == "__main__":
    main()
