# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.0.2] - 2026-10-05

在 0.0.1 基座上完成两项功能开发：新增刀路规划模式、完善刀具建模形态。

### 新增刀路规划模式（策略 1 种 → 3 种）

- **螺旋 Spiral**（`toolpath_lab/planning/spiral.py`，新增）：从区域中心连续向外螺旋，
  一刀走完、空行程少。射线法预计算各角度上边界允许的最大半径（720 项查找表），
  沿弧长采样，外圈自动贴合区域形状——方形得到圆角方形螺旋，圆形为阿基米德螺旋。
- **环切 Contour**（`toolpath_lab/planning/contour.py`，由 `examples/plugins/contour_planner.py`
  启用）：沿轮廓逐圈向内等距偏置，相邻环反向减少空行程。
- 原有栅格刀路（往复 / 单向）保留作对照。

### 完善刀具建模形态（刀具 1 种 → 3 种）

- 开放球头刀、圆鼻刀（去掉参数目录里的 disabled 标记，界面可选）。
- 新增**圆角半径 Rc** 参数：仅在类型为圆鼻时显示（`visible_if`），参与足迹半径计算。
- 足迹半径公式三条分支全部生效：平底 = R，球头 = 0，圆鼻 = R − Rc。
- `Tool` 数据模型增加 `corner_radius_mm` 字段：球头自动取 Rc = R，圆鼻超限自动钳制，平底强制归零。
- 三维显示按刀具类型渲染刀尖形状（`web/js/viewport.js`）：平底 = 平端圆柱，
  球头 = 圆柱 + 球冠，圆鼻 = 平底 + 圆角环 + 刀杆。

### 工程

- 测试从 110 项扩充到 130 项，覆盖新策略的几何布局与新刀具的参数/足迹/接口行为，全部通过。
- README、架构与扩展文档同步更新。

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
