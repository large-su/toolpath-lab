"""渲染《ToolpathLab 开发过程说明与功能介绍》PPT。

页序由 content.py 的 DECK_ORDER 决定：结构页（封面 / 目录 / 定位 / 技术栈 / 阶段 / 图表 /
工程实践 / 限制 / 结束）在代码里，功能页按 FEATURES 里的 deck 字典自动生成。

    python tools/docs/render_pptx.py --out build/docs --figures build/docs/figures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# 让脚本既能 `python tools/docs/render_pptx.py`、也能 `python -m tools.docs.render_pptx` 跑
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.docs import content  # noqa: E402
from tools.docs.paths import figures_dir, resolve_figure  # noqa: E402

JPG = "微软雅黑"
DARK = RGBColor(0x14, 0x2A, 0x2E)
TEAL = RGBColor(0x0E, 0x6B, 0x62)
TEAL_LT = RGBColor(0xE4, 0xF2, 0xF0)
GREY = RGBColor(0x56, 0x66, 0x66)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
AMBER = RGBColor(0xC2, 0x77, 0x0A)
AMBER_LT = RGBColor(0xFA, 0xF1, 0xDF)
COLOURS = {"teal": TEAL, "amber": AMBER, "grey": GREY}
FILLS = {"teal": TEAL_LT, "amber": AMBER_LT, "grey": RGBColor(0xEE, 0xF1, 0xF1)}

SW, SH = 13.333, 7.5


def _spec(item, size, colour):
    """文本规格：字符串 / ("文本", 加粗) / ("文本", 字号, 颜色, 加粗) 都接受。"""

    if not isinstance(item, tuple):
        return item, size, colour, False
    if len(item) == 2 and isinstance(item[1], bool):
        return item[0], size, colour, item[1]
    text, size_i, colour_i, bold_i = (list(item) + [size, colour, False])[:4]
    return text, size_i, colour_i, bool(bold_i)


class Deck:
    def __init__(self, figure_dir: Path) -> None:
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Inches(SW), Inches(SH)
        self.blank = self.prs.slide_layouts[6]
        self.figure_dir = figure_dir
        self.count = 0

    # -- 基础 ----------------------------------------------------------
    def slide(self, number=True):
        node = self.prs.slides.add_slide(self.blank)
        bg = node.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, self.prs.slide_width,
                                   self.prs.slide_height)
        bg.fill.solid()
        bg.fill.fore_color.rgb = WHITE
        bg.line.fill.background()
        bg.shadow.inherit = False
        self.count += 1
        if number:
            self.text(12.3, 6.95, 0.9, 0.35, [(str(self.count), 10, GREY, False)],
                      align=PP_ALIGN.RIGHT)
            self.text(0.78, 6.95, 6.0, 0.35,
                      [("ToolpathLab · 功能开发记录", 10, GREY, False)])
        return node

    def text(self, left, top, width, height, lines, *, align=PP_ALIGN.LEFT, spacing=8):
        box = node = self.prs.slides[-1].shapes.add_textbox(
            Inches(left), Inches(top), Inches(width), Inches(height))
        frame = box.text_frame
        frame.word_wrap = True
        if isinstance(lines, str):
            lines = [lines]
        for index, item in enumerate(lines):
            text, size_i, colour_i, bold_i = _spec(item, 15, DARK)
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.alignment = align
            paragraph.space_after = Pt(spacing)
            run = paragraph.add_run()
            run.text = text
            run.font.name = JPG
            run.font.size = Pt(size_i)
            run.font.bold = bool(bold_i)
            run.font.color.rgb = colour_i
        return frame

    def title(self, heading, sub=None, *, bar=True):
        node = self.prs.slides[-1]
        if bar:
            shape = node.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.18),
                                          self.prs.slide_height)
            shape.fill.solid()
            shape.fill.fore_color.rgb = TEAL
            shape.line.fill.background()
            shape.shadow.inherit = False
        self.text(0.75, 0.42, 11.8, 0.9, [(heading, 27, DARK, True)], spacing=2)
        if sub:
            self.text(0.78, 1.24, 11.9, 0.5, [(sub, 12.5, GREY, False)], spacing=0)

    def bullets(self, lines, *, left=0.85, top=1.95, width=6.0, size=14.5, gap=11):
        rendered = []
        for item in lines:
            text, size_i, colour_i, bold_i = _spec(item, size, DARK)
            rendered.append((f"·  {text}", size_i, colour_i, bold_i))
        return self.text(left, top, width, SH - top - 0.8, rendered, spacing=gap)

    def picture(self, spec, left, top, max_w, max_h, caption=None):
        path = resolve_figure(spec, self.figure_dir)
        width, height = Image.open(path).size
        scale = min(max_w / width, max_h / height)
        w, h = width * scale, height * scale
        x = left + (max_w - w) / 2
        self.prs.slides[-1].shapes.add_picture(str(path), Inches(x), Inches(top),
                                               Inches(w), Inches(h))
        if caption:
            self.text(left, top + h + 0.06, max_w, 0.35, [(caption, 10, GREY, False)],
                      align=PP_ALIGN.CENTER, spacing=0)
        return h

    def card(self, left, top, width, height, heading, lines, colour="teal"):
        base = COLOURS.get(colour, TEAL)
        node = self.prs.slides[-1]
        shape = node.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top),
                                      Inches(width), Inches(height))
        shape.fill.solid()
        shape.fill.fore_color.rgb = FILLS.get(colour, TEAL_LT)
        shape.line.color.rgb = base
        shape.line.width = Pt(1)
        shape.shadow.inherit = False
        frame = shape.text_frame
        frame.word_wrap = True
        frame.margin_left = Inches(0.18)
        frame.margin_top = Inches(0.12)
        self._fill(frame, [(heading, 15, base, True)] +
                   [(f"·  {line}", 12, DARK, False) for line in lines], spacing=5)

    @staticmethod
    def _fill(frame, lines, *, spacing=6):
        for index, item in enumerate(lines):
            text, size_i, colour_i, bold_i = _spec(item, 13, DARK)
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.space_after = Pt(spacing)
            run = paragraph.add_run()
            run.text = text
            run.font.name = JPG
            run.font.size = Pt(size_i)
            run.font.bold = bool(bold_i)
            run.font.color.rgb = colour_i

    def table(self, headers, rows, left, top, width, height, *, sizes=(11, 10)):
        node = self.prs.slides[-1]
        shape = node.shapes.add_table(len(rows) + 1, len(headers), Inches(left), Inches(top),
                                      Inches(width), Inches(height))
        tbl = shape.table
        for index, text in enumerate(headers):
            cell = tbl.cell(0, index)
            cell.text = ""
            run = cell.text_frame.paragraphs[0].add_run()
            run.text = text
            run.font.name = JPG
            run.font.size = Pt(sizes[0])
            run.font.bold = True
            run.font.color.rgb = WHITE
            cell.fill.solid()
            cell.fill.fore_color.rgb = TEAL
        for r, row in enumerate(rows, start=1):
            for c, text in enumerate(row):
                cell = tbl.cell(r, c)
                cell.text = ""
                run = cell.text_frame.paragraphs[0].add_run()
                run.text = str(text)
                run.font.name = JPG
                run.font.size = Pt(sizes[1])
                run.font.color.rgb = DARK
                cell.fill.solid()
                cell.fill.fore_color.rgb = WHITE if r % 2 else TEAL_LT
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        return tbl

    # -- 结构页 --------------------------------------------------------
    def cover(self):
        node = self.slide(number=False)
        band = node.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, self.prs.slide_width, Inches(2.9))
        band.fill.solid()
        band.fill.fore_color.rgb = TEAL
        band.line.fill.background()
        band.shadow.inherit = False
        self.text(0.9, 0.85, 11.6, 1.0, [("ToolpathLab", 40, WHITE, True)], spacing=2)
        self.text(0.95, 1.75, 11.6, 0.7,
                  [("功能开发记录 · 开发过程说明与功能介绍", 17, RGBColor(0xCF, 0xEA, 0xE6), False)],
                  spacing=0)
        self.text(0.9, 3.45, 11.5, 2.0,
                  [("桌面级 2.5D 刀路规划基座：把刀路算清楚、画清楚、验证清楚", 16, DARK, True),
                   (f"从接手基座到分层粗加工：{content.META['commits']} · "
                    f"{content.META['size'].split('；')[0]} · {content.META['tests']}", 13.5, GREY, False),
                   (content.META["date"], 12, GREY, False)], spacing=9)
        for index, (heading, lines, colour) in enumerate(content.DECK_COVER_CARDS):
            self.card(0.9 + index * 3.9, 5.25, 3.7, 1.4, heading, lines, colour)

    def toc(self):
        self.slide()
        self.title("目录", "五个部分：先说清定位与节奏，再用真实刀路演示功能，最后交代工程实践与边界")
        for index, (heading, lines, colour) in enumerate(content.DECK_TOC):
            col, row = index % 2, index // 2
            left = 0.85 + col * 6.0
            top = 2.0 + row * 1.75
            width = 5.7 if col == 0 else 5.6
            self.card(left, top, width, 1.5 if row < 2 else 1.2, heading, lines, colour)

    def positioning(self):
        self.slide()
        self.title("一、项目定位：把刀路算清楚",
                   "用规则区域替掉「导入模型 + 提取特征」那一整套前置环节")
        self.bullets(content.DECK_POSITIONING, top=2.05, size=14.5, gap=13)
        self.card(8.0, 2.05, 4.45, 4.4, "它刻意不做的事", content.DECK_NOT_DOING, "amber")

    def stack(self):
        self.slide()
        self.title("二、技术栈与规模", "依赖单向向下：规划内核可以脱离界面单独测试与调用")
        self.picture("generated/fig-architecture.png", 0.7, 1.75, 8.1, 4.6,
                     "图：core ← planning / simulation / export ← server ← web / electron")
        self.table(["层次", "技术选择", "规模"],
                   [[row[0], row[1].split("；")[0], row[2]] for row in content.STACK_ROWS],
                   8.95, 1.95, 3.7, 2.6, sizes=(10.5, 9.5))
        self.card(8.95, 4.7, 3.7, 1.6, "为什么这么选", content.DECK_STACK_CARD, "teal")

    def stages(self):
        self.slide()
        self.title("三、开发过程：五个阶段",
                   "先能跑起来 → 再扩能力 → 最后做工艺细化；每阶段测试全绿并提交")
        self.table(["阶段", "主要工作", "提交", "用例数"], content.STAGES,
                   0.85, 1.9, 11.6, 4.3, sizes=(11, 10))
        self.text(0.85, 6.35, 11.6, 0.6, [(content.PROCESS_NOTE, 12, AMBER, True)], spacing=0)

    def chart(self):
        self.slide()
        self.title("三、开发过程：测试随功能一起长",
                   "每个功能或修复都带回归测试；撤回柱面时用例数也如实下降")
        data = CategoryChartData()
        data.categories = content.CHART_CATEGORIES
        data.add_series("用例数", tuple(content.CHART_VALUES))
        chart = self.prs.slides[-1].shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.8), Inches(1.85), Inches(8.6),
            Inches(4.7), data).chart
        chart.has_legend = False
        chart.has_title = False
        plot = chart.plots[0]
        plot.gap_width = 55
        plot.has_data_labels = True
        plot.data_labels.font.size = Pt(9)
        plot.data_labels.font.name = JPG
        plot.data_labels.font.color.rgb = TEAL
        for axis in (chart.category_axis, chart.value_axis):
            axis.tick_labels.font.size = Pt(9)
            axis.tick_labels.font.name = JPG
        chart.value_axis.maximum_scale = 300
        self.card(9.7, 1.95, 3.0, 4.45, "为什么值得记一笔", content.CHART_WHY, "teal")

    def practices(self):
        self.slide()
        self.title("六、工程实践：两层测试守住不变量",
                   "263 项后端测试 + 13 项前端几何自检；每次改动都跑全套")
        invariants = content.TABLES["invariants"]
        self.table(invariants["headers"], invariants["rows"], 0.85, 1.9, 11.6, 2.9,
                   sizes=(11.5, 10.5))
        self.card(0.85, 5.05, 5.6, 1.7, "验证手段", content.CHECKS[:2] +
                  ["端到端：启动窗口后直接调 HTTP 接口校对数字"], "teal")
        self.card(6.85, 5.05, 5.6, 1.7, "另一种诚实", content.HONESTY, "amber")

    def limits(self):
        self.slide()
        self.title("七、已知限制与后续方向", "如实交代边界，比夸大能力更有用")
        self.card(0.85, 1.95, 5.6, 4.4, "已知限制", content.LIMITATIONS, "grey")
        self.card(6.85, 1.95, 5.6, 4.4, "后续可选方向", content.NEXT_STEPS, "teal")

    def thanks(self):
        node = self.slide()
        band = node.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, self.prs.slide_width, Inches(3.4))
        band.fill.solid()
        band.fill.fore_color.rgb = TEAL
        band.line.fill.background()
        band.shadow.inherit = False
        self.text(0.9, 1.0, 11.6, 1.0, [("谢谢", 38, WHITE, True)], spacing=2)
        self.text(0.95, 2.0, 11.6, 0.8,
                  [("刀路算得出来、看得见、验得清——这就是这次开发想守住的底线", 15,
                    RGBColor(0xD6, 0xEE, 0xEB), False)], spacing=0)
        self.text(0.9, 3.9, 11.6, 2.2, content.DECK_CLOSING, spacing=7)

    # -- 功能页 --------------------------------------------------------
    def feature(self, feature):
        deck = feature.get("deck")
        if not deck:
            return
        self.slide()
        self.title(deck["heading"], deck.get("sub"))
        picture = deck.get("picture")
        bullets = deck.get("bullets") or []
        cards = deck.get("cards") or []
        table = deck.get("table")
        if table:
            spec = content.TABLES[table]
            self.table(spec["headers"], spec["rows"], 0.85, 2.0, 11.6, 1.9, sizes=(11.5, 10.5))
            if bullets:
                self.bullets(bullets, top=4.25, size=13, gap=11, width=11.6)
        elif picture and cards and not bullets:
            self.picture(picture[0], 0.7, 1.85, 7.6, 4.4, picture[1] if len(picture) > 1 else None)
            top = 1.95
            for card in cards:
                self.card(8.9, top, 3.75, 1.85, card["heading"], card["lines"],
                          card.get("colour", "teal"))
                top += 2.0
        elif picture:
            self.picture(picture[0], 0.7, 1.8, 8.1, 4.6, picture[1] if len(picture) > 1 else None)
            if bullets:
                self.bullets(bullets, left=8.9, width=3.75, top=2.0, size=12.2, gap=9)
        else:
            if bullets:
                self.bullets(bullets, left=0.85, width=7.3, top=2.0, size=13.5, gap=11)
            top = 2.0
            for card in cards:
                self.card(8.4, top, 4.25, 2.1, card["heading"], card["lines"],
                          card.get("colour", "teal"))
                top += 2.3
        if deck.get("note"):
            self.text(0.85, 6.5, 11.6, 0.4, [(deck["note"], 11.5, GREY, True)], spacing=0)


STRUCTURE = {"cover": "cover", "toc": "toc", "positioning": "positioning", "stack": "stack",
             "stages": "stages", "chart": "chart", "practices": "practices",
             "limits": "limits", "thanks": "thanks"}


def build(out_dir: Path, figure_dir: Path) -> Path:
    deck = Deck(figure_dir)
    by_key = {feature["key"]: feature for feature in content.FEATURES}
    for key in content.DECK_ORDER:
        if key in STRUCTURE:
            getattr(deck, STRUCTURE[key])()
        else:
            feature = by_key.get(key)
            if feature is None:
                raise SystemExit(f"content.DECK_ORDER 里的 {key!r} 在 FEATURES 中不存在")
            deck.feature(feature)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "ToolpathLab-开发过程与功能介绍.pptx"
    deck.prs.save(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染功能介绍 PPT")
    parser.add_argument("--out", default="build/docs")
    parser.add_argument("--figures", default=None)
    args = parser.parse_args()
    figure_dir = Path(args.figures) if args.figures else figures_dir(args.out)
    target = build(Path(args.out), figure_dir)
    print(f"PPT: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
