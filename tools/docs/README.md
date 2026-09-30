# 开发记录文档工具链

把《ToolpathLab 功能开发记录》Word 与配套 PPT 变成**可重复生成**的东西：内容写在
`content.py` 里，配图由脚本画（分层剖面用的是**真实规划结果**），两份文档一次生成。

```
tools/docs/
  content.py     唯一内容源 —— 以后完善/增加内容只改这个文件
  figures.py     生成四张数据配图（分层剖面 / 毛坯 / 代码分层 / 测试增长）
  paths.py       图路径解析（generated / images / assets 三种写法）
  render_docx.py 渲染 Word
  render_pptx.py 渲染 PPT（结构页固定，功能页按内容自动多一页）
  build.py       入口：配图 → 两份文档 → 结构检查（可 --copy 到指定目录）
  assets/        文档自带的图（例如已撤回功能的存档图）
```

产物写在 `build/docs/`（**不进版本库**，`build/` 已在 `.gitignore` 里）。

## 日常操作

```bash
python tools/docs/build.py                     # 全套重新生成
python tools/docs/build.py --copy D:\交付        # 顺手复制两份文档过去
python tools/docs/build.py --figures-only      # 只重画配图
python tools/docs/build.py --skip-check        # 跳过结构检查（离线环境）
```

依赖：`python-docx`、`python-pptx`、`Pillow`（DSH 捆绑运行时已自带；分层配图还需要仓库自己的
`toolpath_lab`，所以要在仓库根目录下跑）。

## 加一条新功能（Word 多一节、PPT 多一页）

1. 在 `content.py` 的 `FEATURES` 里加一项，照着文件开头的模板或已有条目写：

   ```python
   dict(
       key="new-feature",                       # 唯一键
       kind="feature",                          # feature / fix / retracted
       doc_heading="3.11 一句话标题",             # Word 章号；写 None 就只在 PPT 出现
       need="需求原文或要点",
       how="实现要点",
       proof="验证：从仓库或接口跑出来的真实数字",
       figure=("generated/fig-xxx.png", "图 N　图注"),   # 可选
       deck=dict(                               # 可选；不给就没有 PPT 页
           heading="四、功能：PPT 页标题",
           sub="副标题",
           picture=("images/xxx.png", "图注"),     # 可选
           bullets=[("要点", True), "普通要点"],    # True = 加粗
           cards=[dict(heading="卡片", lines=["…"], colour="teal")],
           table="region-params",                # 可选：引用 TABLES 里的表
           note="页脚一句（可选）",
       ),
   ),
   ```

2. 要新配图：
   - **数据图**（从刀路算出来的）→ 在 `figures.py` 里加一个绘制函数并注册进 `BUILDERS`，
     内容里写 `generated/fig-xxx.png`；
   - **截图或手工图** → 放进仓库 `docs/images/`，内容里写 `images/xxx.png`；
   - **只在文档里用的图** → 放进 `tools/docs/assets/`，内容里写 `assets/xxx.png`。
3. 想让 PPT 出现这一页，把 `key` 加进 `DECK_ORDER`（页序即数组顺序）。
4. 跑 `python tools/docs/build.py`，产物在 `build/docs/`。

## 数字要跟着仓库走

| 内容源字段 | 从哪来 |
| --- | --- |
| `META["commits"]` / `COMMITS` / `COMMITS_WITH_TESTS` | `git log --oneline` |
| `META["size"]` / `STACK_ROWS` 规模列 | `git ls-files` 与行数统计 |
| `META["tests"]` / `STAGES` 用例数列 / `CHART_VALUES` | `python -m unittest discover -s tests` 的最后一行 |
| 各 `FEATURES[*]["proof"]` 里的刀路数字 | 命令行或 HTTP 接口实跑的结果，别手写 |

## 提交前检查清单

- [ ] `python -m unittest discover -s tests` 全绿，用例数已同步到 `content.py`
- [ ] `node tools/check_frontend_geometry.mjs` 通过
- [ ] `python tools/docs/build.py` 无报错，结构检查 pass
- [ ] 渲染抽查：Word 表格不越界、图片与图注成对；PPT 文字不溢出卡片
- [ ] 新功能在 CHANGELOG / README 里也有对应条目（文档与仓库说明不脱节）

## 检查与渲染（可选）

结构检查用 DSH 捆绑的脚本；也可以用捆绑 LibreOffice Kit 转 PDF 后渲成图做版式抽查：

```bash
python <check_office.py> build/docs/xxx.docx --out build/docs/xxx.checks.json
node <libreoffice-kit/cli.js> convert --input build/docs/xxx.docx --output build/docs/xxx.pdf
node <libreoffice-kit/cli.js> render --input build/docs/xxx.pdf --output-dir build/docs/preview --dpi 96
```

注意：Kit 的 **docx 直接栅格化**路径会按更宽的版心排版再裁到 A4（页面右缘看起来被切）；
判定版式请走 **docx → PDF → PNG** 这条路，PDF 的页边距与设计一致。
