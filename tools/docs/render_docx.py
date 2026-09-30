"""渲染《ToolpathLab 功能开发记录》Word 文档。

内容全部来自 tools/docs/content.py，样式与已交付版本一致：

    python tools/docs/render_docx.py --out build/docs --figures build/docs/figures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# 让脚本既能 `python tools/docs/render_docx.py`、也能 `python -m tools.docs.render_docx` 跑
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.docs import content  # noqa: E402
from tools.docs.paths import figures_dir, resolve_figure  # noqa: E402

CJK = "微软雅黑"
DARK = RGBColor(0x14, 0x2A, 0x2E)
TEAL = RGBColor(0x0E, 0x5C, 0x55)
GREY = RGBColor(0x5A, 0x6A, 0x6A)
AMBER = RGBColor(0xB0, 0x6A, 0x08)


class Doc:
    def __init__(self) -> None:
        self.doc = Document()
        section = self.doc.sections[0]
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2.1)
        section.top_margin = section.bottom_margin = Cm(1.9)
        normal = self.doc.styles["Normal"]
        normal.font.name = CJK
        normal.font.size = Pt(10.5)
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK)
        normal.element.rPr.rFonts.set(qn("w:hAnsi"), CJK)

    # -- 基础排版 ------------------------------------------------------
    @staticmethod
    def _cjk(run, name=CJK):
        run.font.name = name
        rpr = run._element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        for key in ("w:eastAsia", "w:ascii", "w:hAnsi"):
            fonts.set(qn(key), name)

    def heading(self, text, level=1):
        node = self.doc.add_heading(text, level=level)
        for run in node.runs:
            self._cjk(run)
            run.font.color.rgb = TEAL if level <= 1 else DARK
            run.font.size = Pt({0: 26, 1: 15.5, 2: 12.5}.get(level, 11.5))
            run.bold = True
        node.paragraph_format.space_before = Pt(14 if level == 1 else 10)
        node.paragraph_format.space_after = Pt(6)
        return node

    def para(self, text, *, size=10.5, bold=False, color=None, italic=False,
             space_after=6, align=None):
        node = self.doc.add_paragraph()
        node.paragraph_format.space_after = Pt(space_after)
        node.paragraph_format.line_spacing = 1.28
        if align is not None:
            node.alignment = align
        run = node.add_run(text)
        self._cjk(run)
        run.font.size = Pt(size)
        run.bold = bold
        run.italic = italic
        if color is not None:
            run.font.color.rgb = color
        return node

    def bullet(self, text, *, size=10.5):
        node = self.doc.add_paragraph(style="List Bullet")
        node.paragraph_format.space_after = Pt(3)
        node.paragraph_format.line_spacing = 1.24
        run = node.add_run(text)
        self._cjk(run)
        run.font.size = Pt(size)
        return node

    def figure(self, path, caption, width=15.4):
        node = self.doc.add_paragraph()
        node.alignment = WD_ALIGN_PARAGRAPH.CENTER
        node.paragraph_format.space_before = Pt(6)
        node.paragraph_format.space_after = Pt(2)
        node.add_run().add_picture(str(path), width=Cm(width))
        cap = self.doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(10)
        run = cap.add_run(caption)
        self._cjk(run)
        run.font.size = Pt(8.5)
        run.italic = True
        run.font.color.rgb = GREY

    def table(self, headers, rows, widths, *, size=9):
        node = self.doc.add_table(rows=1, cols=len(headers))
        node.style = "Light Grid Accent 1"
        node.autofit = False
        layout = OxmlElement("w:tblLayout")
        layout.set(qn("w:type"), "fixed")
        node._tbl.tblPr.append(layout)
        for index, text in enumerate(headers):
            cell = node.rows[0].cells[index]
            cell.text = ""
            run = cell.paragraphs[0].add_run(text)
            self._cjk(run)
            run.bold = True
            run.font.size = Pt(size)
        for row in rows:
            cells = node.add_row().cells
            for index, text in enumerate(row):
                cells[index].text = ""
                run = cells[index].paragraphs[0].add_run(str(text))
                self._cjk(run)
                run.font.size = Pt(size)
        for index, width in enumerate(widths):
            for row in node.rows:
                row.cells[index].width = Cm(width)
        self._table_grid(node, widths)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)
        return node

    @staticmethod
    def _table_grid(node, widths):
        """显式写 tblGrid 与 tblW：Word / LibreOffice 以 tblGrid 为准。"""

        tbl = node._tbl
        tbl_pr = tbl.tblPr
        old = tbl.find(qn("w:tblGrid"))
        if old is not None:
            tbl.remove(old)
        grid = OxmlElement("w:tblGrid")
        for width in widths:
            col = OxmlElement("w:gridCol")
            col.set(qn("w:w"), str(int(width * 567)))
            grid.append(col)
        tbl_pr.addnext(grid)
        for element in tbl_pr.findall(qn("w:tblW")):
            tbl_pr.remove(element)
        total = OxmlElement("w:tblW")
        total.set(qn("w:w"), str(int(sum(widths) * 567)))
        total.set(qn("w:type"), "dxa")
        tbl_pr.append(total)
        indent = OxmlElement("w:tblInd")
        indent.set(qn("w:w"), "0")
        indent.set(qn("w:type"), "dxa")
        tbl_pr.append(indent)


def build(out_dir: Path, figure_dir: Path) -> Path:
    meta = content.META
    doc = Doc()

    # -------- 封面
    doc.heading(meta["title"], level=0)
    doc.para(meta["subject"], size=13, color=TEAL, space_after=14)
    doc.table(["项目", "内容"],
              [["仓库", meta["repo"]], ["记录日期", meta["date"]],
               ["提交数", meta["commits"]], ["代码规模", meta["size"]],
               ["测试", meta["tests"]]],
              [3.0, 13.4])
    doc.para(meta["intro"], space_after=4)

    # -------- 1 项目概况
    doc.heading("1. 项目概况", level=1)
    doc.heading("1.1 定位与范围", level=2)
    for text in content.OVERVIEW:
        doc.para(text)
    doc.heading("1.2 技术栈与规模", level=2)
    doc.table(["层次", "技术选择", "规模"], content.STACK_ROWS, [2.2, 11.6, 2.6])
    doc.heading("1.3 代码分层", level=2)
    doc.para(content.ARCHITECTURE_LEAD)
    path, caption = "generated/fig-architecture.png", "图 1　代码分层与关键约定（参数声明式、加工面由区域回答、前端不做几何数学）"
    doc.figure(resolve_figure(path, figure_dir), caption)

    # -------- 2 开发过程
    doc.heading("2. 开发过程总览", level=1)
    doc.para(content.PROCESS_LEAD)
    doc.heading("2.1 阶段与节奏", level=2)
    doc.table(["阶段", "主要工作", "提交", "用例数"], content.STAGES, [2.6, 8.0, 3.0, 1.6])
    doc.heading("2.2 测试随功能一起长", level=2)
    doc.figure(resolve_figure("generated/fig-tests.png", figure_dir),
               "图 2　每个功能或修复都带回归测试；撤回柱面时用例数相应下降")
    doc.heading("2.3 提交历史", level=2)
    doc.table(["提交", "说明"], content.COMMITS, [2.4, 14.0])

    # -------- 3 功能开发记录
    doc.heading("3. 功能开发记录", level=1)
    doc.para("下面按“需求 → 实现 → 验证”记录每一项功能。需求一行是当时提出的原话或要点，"
             "验证一行是从仓库或接口跑出来的真实数字。")
    for feature in content.FEATURES:
        heading = feature.get("doc_heading")
        if not heading:
            continue  # 只出现在 PPT 里的条目
        doc.heading(heading, level=2)
        doc.para(f"需求：{feature['need']}", size=10)
        doc.para(f"实现：{feature['how']}", size=10)
        doc.para(f"验证：{feature['proof']}", size=10, space_after=4)
        table_key = feature.get("table")
        if table_key:
            spec = content.TABLES[table_key]
            doc.table(spec["headers"], spec["rows"], [4.4, 12.0], size=9.5)
        for key in ("figure", "figure2"):
            if feature.get(key):
                path, caption = feature[key]
                doc.figure(resolve_figure(path, figure_dir), caption)

    # -------- 4 决策
    doc.heading("4. 关键设计决策", level=1)
    decisions = content.TABLES["decisions"]
    doc.table(decisions["headers"], decisions["rows"], [4.4, 12.0])

    # -------- 5 质量保障
    doc.heading("5. 质量保障", level=1)
    doc.heading("5.1 两层测试", level=2)
    doc.para("后端与库用 unittest：几何裁剪与等距偏置、两种策略的刀路（平面 / 斜面 / 分层）与环距校核、"
             "安全高度、时间参数化、G-code 导出、HTTP 接口与静态资源，共 267 项。前端另有一层几何自检："
             "node tools/check_frontend_geometry.mjs，28 项，覆盖刀路抬升、刀具刀面、毛坯贴合与切削仿真。",
             size=10, space_after=6)
    doc.heading("5.2 被测试守住的关键不变量", level=2)
    invariants = content.TABLES["invariants"]
    doc.table(invariants["headers"], invariants["rows"], [7.6, 8.8])
    doc.heading("5.3 验证方法", level=2)
    for line in content.CHECKS:
        doc.bullet(line)

    # -------- 6/7 限制与后续
    doc.heading("6. 已知限制", level=1)
    for line in content.LIMITATIONS:
        doc.bullet(line)
    doc.heading("7. 后续可选方向", level=1)
    for line in content.NEXT_STEPS:
        doc.bullet(line)

    # -------- 附录
    doc.doc.add_page_break()
    doc.heading("附录 A　提交历史（含每次提交的用例数）", level=1)
    doc.table(["提交", "用例数", "内容"], content.COMMITS_WITH_TESTS, [2.2, 1.8, 12.4])
    doc.heading("附录 B　测试分布", level=1)
    files = content.TABLES["test-files"]
    doc.table(files["headers"], files["rows"], [4.6, 1.4, 10.4])
    doc.para(meta["closing"], size=9, italic=True, color=GREY)

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{meta.get('file', meta['title'])}.docx"
    doc.doc.save(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染功能开发记录 Word")
    parser.add_argument("--out", default="build/docs")
    parser.add_argument("--figures", default=None)
    args = parser.parse_args()
    figure_dir = Path(args.figures) if args.figures else figures_dir(args.out)
    target = build(Path(args.out), figure_dir)
    print(f"Word: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
