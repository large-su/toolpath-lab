# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。0.0.1 是第一个公开版本。

## [未发布]

### 能力

- **环切刀路**（`contour`）：把环切从示例插件转正为内置策略，从区域轮廓逐圈向内偏置，
  相邻环绕行方向交替（顺铣/逆铣交替），环间不抬刀直接过渡；参数为切宽、采样步长、进给速度。
  界面与 `POST /api/plan` 都会自动出现，不需要改前端代码。
  - 边界偏置几何（凸角斜接、凹角圆弧接头、按"到原始边界的距离"过滤自交顶点）随策略一起放进
    `toolpath_lab/planning/contour.py`；`offset_polygon` 现在只做向内偏置，负值直接报错，
    避免悄悄返回一条跑到区域外面的环。
  - 工具足迹半径超过区域内切半径时报"几何不可行"（HTTP 422），与栅格策略一致。
- `examples/plugins/contour_planner.py` 保留为参考模板（与内置版同源），id 改为 `contour_demo`，
  避免与内置策略重复注册；它的 `PlanningError` 与"只向内偏置"契约也与内置版保持一致。

### 工程

- 新增 `tests/test_contour.py`（24 项）：等距偏置的数值断言（方形内缩保持方形、圆形内缩后
  半径、超过内切半径退化、重采样等弧长）、环数与切削长度的精确值、闭环与 Z=0、方向交替、
  环间不抬刀、安全高度与快移进给、几何不可行。

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
