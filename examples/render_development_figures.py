"""生成开发汇报用的演示图（本分支新增功能的可视化）。

在项目根目录运行：

    python examples/render_development_figures.py            # 输出到 figures/
    python examples/render_development_figures.py --outdir 图

生成 5 张图：
1. 刀具形态与咬入半径（平底/球头/圆鼻）；
2. 三种刀路的路网对比（往复/单向/螺旋）；
3. 材料切除仿真的高度场（切净 / 留脊 / 球头浅切 / 螺旋）；
4. 多策略评分；
5. 覆盖率随切宽的变化 + 建议切宽。

图纸只依赖 matplotlib（仅用于出图，不属于运行时依赖）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from toolpath_lab.core.region import CircleRegion, build_region  # noqa: E402
from toolpath_lab.core.tool import Tool, ToolKind  # noqa: E402
from toolpath_lab.evaluation import (  # noqa: E402
    default_candidates,
    evaluate_strategies,
    suggest_stepover,
)
from toolpath_lab.planning import run_plan  # noqa: E402
from toolpath_lab.simulation import simulate_removal  # noqa: E402

for family in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"):
    matplotlib.rcParams["font.sans-serif"] = [family, "DejaVu Sans"]
    break
matplotlib.rcParams["axes.unicode_minus"] = False
matplotlib.rcParams["figure.dpi"] = 130
matplotlib.rcParams["savefig.bbox"] = "tight"

CUT, LINK, RAPID = "#1f6fd0", "#8fb8e8", "#d9534f"


def tool(kind: str, diameter: float = 10.0, corner: float = 2.0) -> Tool:
    return Tool(ToolKind(kind), diameter_mm=diameter, length_mm=40.0, corner_radius_mm=corner)


def draw_tool_profile(ax, tool_obj: Tool, color: str) -> None:
    profile = np.array(tool_obj.profile_mm(), dtype=float)
    ax.plot(np.concatenate([profile[:, 0], -profile[:, 0][::-1]]),
            np.concatenate([profile[:, 1], profile[:, 1][::-1]]),
            color=color, linewidth=2)
    ax.fill(np.concatenate([profile[:, 0], -profile[:, 0][::-1]]),
            np.concatenate([profile[:, 1], profile[:, 1][::-1]]),
            color=color, alpha=0.18)


def figure_tools(outdir: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.4))
    flat, ball, bull = tool("flat"), tool("ball"), tool("bull")
    for ax, t, color in ((ax1, flat, "#1f6fd0"),):
        draw_tool_profile(ax, t, color)
        draw_tool_profile(ax, ball, "#e08a00")
        draw_tool_profile(ax, bull, "#2e9e6b")
    ax1.set_title("三种刀具的刀尖轮廓（D10）")
    ax1.set_xlabel("径向 r /mm")
    ax1.set_ylabel("轴向 z /mm")
    ax1.set_xlim(-6, 6)
    ax1.set_ylim(-0.6, 12)
    ax1.grid(alpha=0.25)
    ax1.legend(handles=[
        plt.Line2D([], [], color="#1f6fd0", label="平底刀 R5"),
        plt.Line2D([], [], color="#e08a00", label="球头刀 R5"),
        plt.Line2D([], [], color="#2e9e6b", label="圆鼻刀 R5 / Rc2"),
    ], loc="upper right", fontsize=8)

    depths = np.linspace(0.0, 2.0, 200)
    for t, color, label in ((flat, "#1f6fd0", "平底刀"), (ball, "#e08a00", "球头刀"),
                            (bull, "#2e9e6b", "圆鼻刀 Rc2")):
        radii = [t.cutting_footprint_radius_mm(float(d)) for d in depths]
        ax2.plot(depths, radii, color=color, linewidth=2, label=label)
    ax2.set_title("咬入半径随轴向切深 ap 的变化")
    ax2.set_xlabel("轴向切深 ap /mm")
    ax2.set_ylabel("实际切除半径 /mm")
    ax2.grid(alpha=0.25)
    ax2.legend(fontsize=8)
    fig.savefig(outdir / "fig1_tool_shapes.png")
    plt.close(fig)


def draw_path(ax, toolpath, title: str) -> None:
    colors = {"cut": CUT, "link": LINK, "rapid": RAPID}
    drawn = set()
    for move in toolpath.moves:
        points = move.points
        kind = move.kind.value
        label = {"cut": "切削", "link": "连接", "rapid": "快移"}[kind]
        ax.plot(points[:, 0], points[:, 1], color=colors[kind],
                linewidth=1.4 if kind == "cut" else 0.9,
                linestyle="-" if kind != "rapid" else "--",
                label=label if label not in drawn else None)
        drawn.add(label)
    ax.set_title(title)
    ax.set_aspect("equal")
    ax.grid(alpha=0.2)
    ax.tick_params(labelsize=8)
    ax.set_xlabel("X /mm", fontsize=8)
    ax.set_ylabel("Y /mm", fontsize=8)


def figure_paths(outdir: Path) -> None:
    region = CircleRegion(diameter_mm=60.0)
    t = tool("flat")
    variants = [
        ("raster", {"mode": "zigzag", "stepover_mm": 7.5}),
        ("raster", {"mode": "one_way", "stepover_mm": 7.5}),
        ("spiral", {"stepover_mm": 7.5}),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(11.0, 3.9))
    for ax, (planner_id, parameters) in zip(axes, variants):
        outcome = run_plan(planner_id=planner_id, tool=t, region=region,
                           parameters=dict(parameters, feed_mm_per_min=800.0))
        title = (f"{outcome.toolpath.planner_label}"
                 f"（抬刀 {sum(1 for m in outcome.toolpath.moves if m.kind.value == 'rapid')} 段）")
        draw_path(ax, outcome.toolpath, title)
    axes[0].legend(fontsize=8, loc="lower center", ncol=3, frameon=False,
                   bbox_to_anchor=(1.75, -0.42))
    fig.savefig(outdir / "fig2_paths.png")
    plt.close(fig)


def figure_removal(outdir: Path) -> None:
    region = build_region("square", {"side_mm": 60.0})
    flat_t, ball_t = tool("flat"), tool("ball")
    cases = [
        ("平底刀 步距 4 mm（切净）", flat_t, "raster",
         {"mode": "zigzag", "stepover_mm": 4.0}, 1.0),
        ("平底刀 步距 14 mm（留脊）", flat_t, "raster",
         {"mode": "zigzag", "stepover_mm": 14.0}, 1.0),
        ("球头刀 ap0.2 步距 6 mm（漏切）", ball_t, "raster",
         {"mode": "zigzag", "stepover_mm": 6.0}, 0.2),
        ("螺旋刀路 步距 6 mm（切净）", flat_t, "spiral",
         {"stepover_mm": 6.0}, 1.0),
    ]
    fig, axes = plt.subplots(1, 4, figsize=(12.6, 3.5))
    for ax, (title, t, planner_id, parameters, depth) in zip(axes, cases):
        toolpath = run_plan(planner_id=planner_id, tool=t, region=region,
                            parameters=dict(parameters, feed_mm_per_min=800.0)).toolpath
        report = simulate_removal(toolpath, t, region, resolution_mm=0.5,
                                  axial_depth_mm=depth)
        heights = np.where(report.inside, report.heights, np.nan)
        image = ax.imshow(heights, origin="lower", cmap="YlOrRd", vmin=0.0, vmax=depth,
                          extent=[report.bounds[0], report.bounds[1],
                                  report.bounds[2], report.bounds[3]])
        ax.set_title(f"{title}\n覆盖率 {report.metrics['coverage_ratio'] * 100:.1f}%",
                     fontsize=9)
        ax.tick_params(labelsize=7)
        fig.colorbar(image, ax=ax, fraction=0.046, label="残余高度 /mm")
    fig.savefig(outdir / "fig3_removal.png")
    plt.close(fig)


def figure_scores(outdir: Path) -> None:
    region = CircleRegion(diameter_mm=60.0)
    t = tool("flat")
    report = evaluate_strategies(
        tool=t, region=region,
        candidates=default_candidates(stepover_mm=suggest_stepover(t, 1.0),
                                      feed_mm_per_min=800.0),
        resolution_mm=1.0, axial_depth_mm=1.0,
    )
    labels = [f"{e.planner_label}\n{e.parameters.get('mode', e.parameters.get('direction', ''))}"
              for e in report.entries]
    metrics = ["quality", "efficiency", "air"]
    names = ["质量", "效率", "行程"]
    colors = ["#1f6fd0", "#e08a00", "#2e9e6b"]
    x = np.arange(len(labels))
    width = 0.24
    fig, ax = plt.subplots(figsize=(7.6, 3.6))
    for index, (metric, name, color) in enumerate(zip(metrics, names, colors)):
        values = [getattr(entry, f"{metric}_score") for entry in report.entries]
        bars = ax.bar(x + (index - 1) * width, values, width, label=name, color=color)
        ax.bar_label(bars, fmt="%.0f", fontsize=7)
    totals = [entry.total_score for entry in report.entries]
    for position, total in zip(x, totals):
        ax.text(position, 108, f"总分 {total:.1f}", ha="center", fontsize=9, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylim(0, 118)
    ax.set_ylabel("分数")
    ax.set_title("同一把刀、同一块区域的策略评分（圆形 Ø60，步距 7.5 mm）")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(fontsize=8, ncol=3, loc="lower right")
    fig.savefig(outdir / "fig4_scores.png")
    plt.close(fig)


def figure_coverage(outdir: Path) -> None:
    region = build_region("square", {"side_mm": 60.0})
    fig, ax = plt.subplots(figsize=(7.4, 3.6))
    for kind, color, label, depth in (("flat", "#1f6fd0", "平底刀 ap1.0", 1.0),
                                      ("bull", "#2e9e6b", "圆鼻刀 Rc2 ap1.0", 1.0),
                                      ("ball", "#e08a00", "球头刀 ap0.2", 0.2)):
        t = tool(kind)
        stepovers = np.arange(1.0, 16.5, 1.0)
        coverage = []
        for stepover in stepovers:
            toolpath = run_plan(planner_id="raster", tool=t, region=region,
                                parameters={"mode": "zigzag", "stepover_mm": float(stepover),
                                            "feed_mm_per_min": 800.0}).toolpath
            report = simulate_removal(toolpath, t, region, resolution_mm=1.0,
                                      axial_depth_mm=depth)
            coverage.append(report.metrics["coverage_ratio"] * 100.0)
        ax.plot(stepovers, coverage, marker="o", markersize=3, color=color, label=label)
        suggestion = suggest_stepover(t, depth)
        ax.axvline(suggestion, color=color, linestyle=":", alpha=0.7)
        ax.annotate(f"建议 {suggestion:g} mm", (suggestion, 20),
                    rotation=90, fontsize=8, color=color, va="bottom")
    ax.axhline(99.0, color="#999999", linestyle="--", linewidth=1)
    ax.text(15.6, 99.4, "切净线 99%", fontsize=8, color="#666666", ha="right")
    ax.set_xlabel("切宽 ae /mm")
    ax.set_ylabel("覆盖率 /%")
    ax.set_ylim(10, 108)
    ax.set_title("覆盖率随切宽的变化：刀具形状决定可用切宽")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="lower left")
    fig.savefig(outdir / "fig5_coverage_vs_stepover.png")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成开发汇报演示图")
    parser.add_argument("--outdir", default="figures", help="输出目录（默认 figures/）")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    outdir = (root / args.outdir) if not Path(args.outdir).is_absolute() else Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    figure_tools(outdir)
    figure_paths(outdir)
    figure_removal(outdir)
    figure_scores(outdir)
    figure_coverage(outdir)
    for path in sorted(outdir.glob("fig*.png")):
        print(f"已生成 {path.relative_to(root)}  ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
