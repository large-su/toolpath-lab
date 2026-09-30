"""为开发记录文档与 PPT 生成数据配图。

四张图：分层粗加工剖面、毛坯示意、模块分层、测试增长。
**分层那张用真实规划结果绘制**，不是示意图；测试增长的数字来自 content.py。

    python tools/docs/figures.py --out build/docs/figures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 让脚本既能 `python tools/docs/figures.py`、也能 `python -m tools.docs.figures` 跑
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.docs import content  # noqa: E402

BG = (7, 16, 20)
PANEL = (13, 26, 31)
LINE = (44, 62, 66)
TOOL = (255, 204, 0)
TEAL = (84, 214, 196)
ORANGE = (255, 167, 38)
VIOLET = (176, 124, 240)
STOCK = (40, 54, 62)
PART = (96, 118, 138)
TEXT = (137, 155, 153)
WHITE = (232, 240, 240)
RED = (255, 138, 128)

FONT_CANDIDATES = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]
BOLD_CANDIDATES = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"]


def _font(candidates, size):
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def fonts():
    return (_font(FONT_CANDIDATES, 15), _font(FONT_CANDIDATES, 13),
            _font(FONT_CANDIDATES, 12), _font(BOLD_CANDIDATES, 17))


def layered(out: Path) -> None:
    """分层粗加工剖面：层高与每层的 x 范围都取自真实刀路。"""

    from toolpath_lab.core.path import MoveKind
    from toolpath_lab.core.region import build_region
    from toolpath_lab.core.tool import Tool, ToolKind
    from toolpath_lab.planning import run_plan

    import numpy as np

    font, small, tiny, bold = fonts()
    side, cap, angle = 80.0, 80.0, 60.0
    region = build_region("ramp", {"side_mm": side, "angle_deg": angle, "cap_z_mm": cap})
    outcome = run_plan(
        planner_id="raster",
        tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
        region=region,
        parameters={"mode": "one_way", "stepover_mm": 6.0, "direction_deg": 0.0,
                    "feed_mm_per_min": 600.0, "layer_depth_mm": 20.0, "stock_margin_mm": 2.0},
    )
    cuts = [m for m in outcome.toolpath.moves
            if m.kind is MoveKind.CUT and m.label != "沿面切入"]
    levels: dict[float, list] = {}
    surface = []
    for move in cuts:
        if float(np.ptp(move.points[:, 2])) < 1e-9:
            levels.setdefault(round(float(move.points[0][2]), 3), []).append(move)
        else:
            surface.append(move)
    stock_top = cap + 2.0

    width, height = 1080, 470
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    left, right, top_edge, bottom = 92, 620, 96, height - 76
    scale_x = (right - left) / side
    scale_z = (bottom - top_edge) / (stock_top + 8.0)

    def px(x, z):
        return (left + (x + side / 2) * scale_x, bottom - z * scale_z)

    draw.text((24, 20), "分层粗加工：毛坯按「每层深度」逐层铣掉，平台高度单独补一层",
              fill=WHITE, font=bold)
    draw.text((24, 46), "斜坡 80×80、60°、Z 上限 80、余量 2、每层 20（D6 平底刀）"
                        "——下面每条横线都是真实刀路", fill=TEXT, font=tiny)

    draw.rectangle([px(-40, stock_top), px(40, -20)], outline=VIOLET, width=2)
    xs = np.linspace(-side / 2, side / 2, 240)
    tops = region.height_at(np.column_stack((xs, np.zeros_like(xs))))
    body = [px(-40, -20)] + [px(float(x), float(z)) for x, z in zip(xs, tops)] + [px(40, -20)]
    draw.polygon(body, fill=STOCK)
    draw.line([px(float(x), float(z)) for x, z in zip(xs, tops)], fill=(126, 148, 158), width=2)

    top_level = max(levels)
    for z in sorted(levels, reverse=True):
        moves = levels[z]
        x_lo = min(float(m.points[:, 0].min()) for m in moves)
        x_hi = max(float(m.points[:, 0].max()) for m in moves)
        plateau = abs(z - cap) < 1e-6
        draw.line([px(x_lo, z), px(x_hi, z)], fill=ORANGE if plateau else TEAL,
                  width=3 if plateau else 2)
        draw.text((px(x_lo, z)[0] - 4, px(x_lo, z)[1] + (-20 if z == top_level else -16)),
                  f"Z={z:g}" + ("（平台清料）" if plateau else ""),
                  fill=ORANGE if plateau else TEAL, font=tiny, anchor="la")
    if surface:
        move = surface[0]
        draw.line([px(float(p[0]), float(p[2])) for p in move.points], fill=WHITE, width=3)

    crease = float(region.crease_x_mm)
    draw.line([px(crease, -20), px(crease, cap)], fill=RED, width=1)
    draw.text((px(crease, 74)[0] - 26, px(crease, 74)[1]), "折痕", fill=RED, font=tiny)
    draw.line([px(-40, -20), px(40, -20)], fill=LINE, width=1)
    draw.text((px(-40, -20)[0], bottom + 8), "工件底面", fill=TEXT, font=tiny)

    x0 = 656
    draw.text((x0, 96), "这一刀路怎么来的", fill=WHITE, font=font)
    notes = [
        "① 毛坯顶面 = 加工面最高点 + 余量 = 82",
        "② 从顶面往下每层 20 → 82 / 62 / 42 / 22",
        "③ 再补一层落在平台高度 80",
        "   （少了它，平顶之上的 2 mm 没人清）",
        "④ 每层只切「加工面低于这一层」的范围：",
        "   越往下越窄，到了 Z 上限则是整块",
        "⑤ 放不下刀具的层跳过并提醒",
        "⑥ 最后沿加工面精加工一遍（白线）",
    ]
    for index, line in enumerate(notes):
        draw.text((x0, 126 + index * 21), line, fill=TEXT, font=tiny)
    legend = [(TEAL, "分层粗削"), (ORANGE, "平台清料层"), (WHITE, "沿加工面精加工"), (VIOLET, "毛坯轮廓")]
    for index, (colour, label) in enumerate(legend):
        y = height - 118 + index * 22
        draw.line([(x0, y + 7), (x0 + 26, y + 7)], fill=colour, width=3)
        draw.text((x0 + 34, y), label, fill=TEXT, font=tiny)
    image.save(out / "fig-layered.png")


def blank(out: Path) -> None:
    """毛坯示意：正视 + 俯视，竖直面贴紧、只有顶面留余量。"""

    font, small, tiny, bold = fonts()
    width, height = 1000, 460
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.text((24, 20), "生成毛坯：竖直面贴紧区域，只有顶面留余量（默认 2 mm）",
              fill=WHITE, font=bold)
    draw.text((24, 46), "方形 → 长方体；圆形 → 竖直圆柱。底面与工件底面齐平，"
                        "刀路与界面显示共用同一个余量值", fill=TEXT, font=tiny)
    margin_px = 16

    def front(x, y, w, h, margin, cylindrical):
        draw.rectangle([x, y, x + w, y + h], outline=VIOLET, width=2, fill=(24, 34, 40))
        draw.rectangle([x, y + margin, x + w, y + h], fill=PART)
        draw.line([x - 6, y + margin, x + w + 6, y + margin], fill=(150, 172, 182), width=1)
        if cylindrical:
            draw.line([x + w * 0.16, y + margin + 6, x + w * 0.16, y + h - 6],
                      fill=(58, 74, 84), width=1)
            draw.line([x + w * 0.84, y + margin + 6, x + w * 0.84, y + h - 6],
                      fill=(58, 74, 84), width=1)
        draw.line([x + w + 12, y, x + w + 12, y + margin], fill=ORANGE, width=2)
        draw.line([x + w + 8, y, x + w + 16, y], fill=ORANGE, width=1)
        draw.line([x + w + 8, y + margin, x + w + 16, y + margin], fill=ORANGE, width=1)
        draw.text((x + w + 22, y + margin / 2 - 8), "余量 2", fill=ORANGE, font=tiny)

    def top(x, y, size, cylindrical):
        if cylindrical:
            draw.ellipse([x, y, x + size, y + size], outline=VIOLET, width=2)
            draw.ellipse([x + 1, y + 1, x + size - 1, y + size - 1], outline=TEAL, width=1)
        else:
            draw.rectangle([x, y, x + size, y + size], outline=VIOLET, width=2)
            draw.rectangle([x + 1, y + 1, x + size - 1, y + size - 1], outline=TEAL, width=1)

    front(120, 118, 150, 190, margin_px, False)
    top(300, 150, 126, False)
    draw.text((120, 80), "方形：长方体毛坯", fill=TEAL, font=font)
    draw.text((120, 330), "竖直面贴紧 → 俯视只看到一圈", fill=TEXT, font=tiny)
    front(560, 118, 150, 190, margin_px, True)
    top(760, 150, 126, True)
    draw.text((560, 80), "圆形：竖直圆柱毛坯", fill=TEAL, font=font)
    draw.text((560, 330), "圆柱直径 = 区域直径（贴紧）", fill=TEXT, font=tiny)
    draw.text((24, height - 54), "顶面余量是刀路参数：界面上的毛坯与分层粗加工算的是同一块料"
                                 "——毛坯顶面就是分层粗加工的起点。", fill=TEXT, font=small)
    image.save(out / "fig-blank.png")


def architecture(out: Path) -> None:
    """代码分层：依赖单向向下。"""

    font, small, tiny, bold = fonts()
    width, height = 940, 430
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.text((22, 18), "代码分层：依赖单向向下，规划内核可以脱离界面单独跑", fill=WHITE, font=bold)

    def box(x, y, w, h, heading, items, colour):
        draw.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=PANEL, outline=colour, width=2)
        draw.text((x + 12, y + 8), heading, fill=colour, font=font)
        for index, item in enumerate(items):
            draw.text((x + 14, y + 32 + index * 18), item, fill=TEXT, font=tiny)

    box(30, 60, 250, 116, "core（领域层）",
        ["tool / region / path", "parameters / registry / payload"], TEAL)
    box(310, 60, 250, 116, "planning（策略层）",
        ["base：加工面、分层、安全面", "raster / follow_periphery"], TEAL)
    box(30, 196, 250, 96, "simulation / export", ["时间参数化、G-code"], (120, 180, 240))
    box(310, 196, 250, 96, "server（HTTP）", ["catalog / schema / service / app"],
        (120, 180, 240))
    box(30, 312, 530, 78, "web（原生 ES 模块 + vendored three.js）/ electron 桌面壳",
        ["参数面板按 registry 自动生成；视口只吃后端下发的几何", ""], ORANGE)
    for y in (176, 292):
        draw.line([(155, y), (155, y + 20)], fill=LINE, width=2)
    draw.line([(155, 312), (155, 312)], fill=LINE, width=2)
    for x in (155, 435):
        draw.line([(x, 176 if x == 435 else 292), (x, 196 if x == 435 else 292)], fill=LINE, width=2)
    x0 = 600
    draw.text((x0, 60), "看点", fill=WHITE, font=font)
    points = [
        "· 参数用 spec() 声明：界面、校验、",
        "  文档、接口载荷自动跟上",
        "· 加工面由区域回答（height_at /",
        "  surface_breaks / machining_boundary）",
        "  → 策略只管 XY，Z 全自动",
        "· 三维几何由后端下发，前端不做数学",
        f"· {content.META['size'].split('；')[0]}",
    ]
    for index, line in enumerate(points):
        draw.text((x0, 92 + index * 20), line, fill=TEXT, font=tiny)
    image.save(out / "fig-architecture.png")


def tests(out: Path) -> None:
    """测试增长：数字直接来自 content.py，保证与正文一致。"""

    font, small, tiny, bold = fonts()
    width, height = 960, 470
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.text((24, 20), "测试随功能一起长：每个功能/修复都带回归测试", fill=WHITE, font=bold)
    draw.text((24, 46), "python -m unittest discover -s tests（每阶段结束时的用例数）",
              fill=TEXT, font=tiny)
    stages = list(zip(content.CHART_CATEGORIES, content.CHART_VALUES))
    bar_x, bar_w, bar_y, gap = 330, 520, 72, 30
    peak = max(value for _, value in stages)
    baseline = content.CHART_VALUES[0]
    for index, (label, value) in enumerate(stages):
        y = bar_y + index * gap
        draw.text((24, y + 3), label, fill=TEXT, font=small)
        bar = int(bar_w * value / peak)
        draw.rounded_rectangle([bar_x, y, bar_x + bar, y + 20], radius=5, fill=(26, 78, 80))
        draw.rounded_rectangle([bar_x, y, bar_x + int(bar_w * baseline / peak), y + 20],
                               radius=5, fill=(32, 100, 100))
        draw.text((bar_x + bar + 12, y + 2), str(value), fill=TEAL, font=small)
    draw.line([(24, height - 62), (width - 24, height - 62)], fill=(40, 56, 60), width=1)
    draw.text((24, height - 52), "另有前端几何自检 13 项（node tools/check_frontend_geometry.mjs）："
                                 "刀路抬升保留真实 Z、", fill=TEXT, font=tiny)
    draw.text((24, height - 34), "三种刀具刀尖位置、工件法向、毛坯贴合与摆放——"
                                 "这些只有肉眼能看出来，Python 测试覆盖不到。", fill=TEXT, font=tiny)
    image.save(out / "fig-tests.png")


BUILDERS = {"layered": layered, "blank": blank, "architecture": architecture, "tests": tests}


def build(out: Path, only: list[str] | None = None) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    names = only or list(BUILDERS)
    paths = []
    for name in names:
        BUILDERS[name](out)
        path = out / f"fig-{name}.png"
        paths.append(path)
        print(f"  配图 {path.name}  {Image.open(path).size}")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="生成开发记录配图")
    parser.add_argument("--out", default="build/docs/figures", help="输出目录")
    parser.add_argument("--only", nargs="*", choices=list(BUILDERS), help="只生成某几张")
    args = parser.parse_args()
    out = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    build(out, args.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
