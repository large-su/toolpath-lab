# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。0.0.1 是第一个公开版本。

## [未发布] - 2026-10-04 · 分支 feat/removal-sim-and-spiral

课程作业的功能开发训练（作者：陈栎霏 U202411196）。详见 `docs/development.md`。

### 新增

- **材料切除仿真（Z-map）**：`simulation/removal.py`。按刀尖形状与轴向切深逐段扫掠，
  输出覆盖率、残余面积 / 体积、残余高度、区域外切出面积（过切）与材料去除率。
- **螺旋刀路策略** `spiral`：圆形区域为解析阿基米德螺线（单段切削、零抬刀、等切宽），
  其他形状为等距环 + 切向连接；参数含切宽、旋向、采样步长与「外圈清边」。
  开发中发现并修正了"单条螺线最外环带漏切"的问题（覆盖率 81.3% → 100%），见开发说明 4.2 节。
- **刀路质量评价** `evaluation/`：`evaluate_strategies` 把覆盖率（质量 0.5）、
  相对工时（效率 0.3）、切削行程占比（行程 0.2）合成可排序的评分表；
  `suggest_stepover` 由刀具与切深给出"能切净"的切宽建议。
- **接口** `POST /api/evaluate`：一次请求完成多策略规划 + 仿真 + 评分排序。
- **示例**：`examples/evaluate_strategies.py`（命令行对比）、
  `examples/render_development_figures.py`（生成 5 张汇报演示图，输出到 figures/）。

### 变更

- **刀具**：球头刀与圆鼻刀正式启用（不再是"待拓展"）；新增 `corner_radius_mm` 参数
  （按类型归一化，仅圆鼻刀可见）；新增 `cutting_footprint_radius_mm(ap)` 与 `profile_mm()`。
- **区域**：新增 `rounded_rect`（圆角矩形，圆角取到短边一半即为跑道形）。
- **几何**：偏置多边形、等弧长重采样、点在多边形内判定从示例插件提升为
  `planning/geometry2d.py` 的公共工具。
- 既有测试中 5 处"能力范围断言"随扩展更新（详见开发说明第 7 节）。

### 工程

- 测试：112 → **142 项**（新增 30 项：刀具形态、圆角矩形、螺旋两分支、切除仿真、评价排序、/api/evaluate）。

## [0.0.1] - 2026-09-10

第一个公开版本：刀路规划基座，包含刀具与规则区域建模、往复/单向栅格刀路、桌面三维可视化与按进给播放。

### 能力

- **刀具**：平底刀（直径、长度）。球头刀 / 圆鼻刀在参数目录里标记为"待拓展"、界面上不可选，
  足迹半径的公式三条分支都已写好。
- **区域**：方形（边长）与圆形（直径），加工面固定为 XY 平面。
- **刀路**：栅格刀路的**往复 Zigzag** 与**单向 One-way** 两种模式。
- **参数**：切宽、走刀方向、进给速度。安全高度 5 mm、快移 5000 mm/min、边界内缩一个刀具半径、
  一刀两个点等为固定常量（见 planning/base.py）。
- **桌面窗口**（Electron）：左键旋转 / 中键缩放 / 右键平移、顶部标准视图工具条、
  实时阴影 / 白色背景 / 网格地面、按进给速度播放与拖动进度。
- **导出**：NC（G-code，G21/G90/G17 + G0/G1 带 F 的最常见 ISO 子集）。
- **接口**：GET /api/catalog、POST /api/plan、POST /api/export/gcode。
- **示例**：examples/headless_plan.py（不用界面直接算刀路）、
  examples/plugins/contour_planner.py（环切示例插件，复制进主程序即可启用）。

### 工程

- 依赖方向单一：core ← planning / simulation / export ← server ← web / electron；
- 没有 Web 框架：后端只用 Python 标准库 + numpy；前端是原生 ES 模块 + 自带 three.js，无打包器；
- 参数声明（ParameterSpec）同时驱动界面生成、请求校验与文档；
- 110 项 unittest，覆盖几何、策略、时间轴、导出、HTTP 接口与静态资源；
- 一键启动：start.bat（Windows）/ start.sh（Linux、macOS），会自动准备 Python 环境与 Electron。
