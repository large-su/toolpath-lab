# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。0.0.1 是第一个公开版本。

## [未发布]

### 能力

- **固定值参数化**：安全高度、快移速度、边界处理方式不再写死在代码里。
  - `planning/base.py` 新增共用的 `MOTION_PARAMETERS`（`safe_height_mm` 默认 5 mm、
    `rapid_feed_mm_per_min` 默认 5000 mm/min），由各策略并进自己的 `ParameterSet`；
    `PlanningContext` 从参数读值，策略没声明这两个键时退回原来的默认常量。
  - 栅格策略新增 `boundary_mode`（`inset` 内缩一个刀具半径 / `none` 贴轮廓）与
    `stock_allowance_mm`（边界余量，默认 0，只在 `inset` 下显示）。刀路 notes 与响应
    `warnings` 会记录这两项。
  - 未提供"外扩（负偏置）"：扫描线在轮廓外取不到区间，负偏置会让 v 方向刀线被静默丢掉，
    实测 u 方向切出轮廓、v 方向留下不对称的未切带。要少切一圈请用边界余量。
  - `GET /api/catalog` 删除 `fixed` 段（那两项已变成参数），默认值仍在
    `planners.defaults` 里；界面上原来的"固定设置"面板随之删除。
  - 参数不合法仍返回 400；`PlanningError` 仍是 422。
- **环切刀路**（`contour`）：把环切从示例插件转正为内置策略，从区域轮廓逐圈向内偏置，
  相邻环绕行方向交替（顺铣/逆铣交替），环间不抬刀直接过渡；参数为切宽、采样步长、进给速度
  以及共用的动作参数。界面与 `POST /api/plan` 都会自动出现，不需要改前端代码。
  - 边界偏置几何（凸角斜接、凹角圆弧接头、按"到原始边界的距离"过滤自交顶点）随策略一起放进
    `toolpath_lab/planning/contour.py`；`offset_polygon` 现在只做向内偏置，负值直接报错，
    避免悄悄返回一条跑到区域外面的环。
  - 工具足迹半径超过区域内切半径时报"几何不可行"（HTTP 422），与栅格策略一致。
- `examples/plugins/contour_planner.py` 保留为参考模板（与内置版同源），id 改为 `contour_demo`，
  避免与内置策略重复注册；它的 `PlanningError` 与"只向内偏置"契约也与内置版保持一致，
  并示范了如何把 `MOTION_PARAMETERS` 并进自己的参数集。

### 修复

- `python examples/headless_plan.py` 现在按文档写的那样可以直接运行：脚本先把仓库根补进
  `sys.path`（此前 `sys.path[0]` 是 `examples/`，裸跑会 `ModuleNotFoundError`，
  只有设了 `PYTHONPATH` 或 `pip install -e .` 才行）。这也是 CONTRIBUTING 提交前自检的第 2 条。
- 清理已经过时的文档引用：`toolpath_lab/__init__.py` 里提到的 surface / 高度场 / JSON / CSV
  导出、`core/errors.py` 与 `core/registry.py` 里的 surface、`core/payload.py` 请求示例中的
  `surface` 分组与不存在的 `rectangle` 形状、`core/parameters.py` 指向的 `docs/parameters.md`
  ——这些都是简化版本里已经删掉的能力，文档不该继续承诺。

### 工程

- 新增 `tests/test_motion_parameters.py`（20 项）：参数声明与默认值、两个策略都吃
  `safe_height_mm` / `rapid_feed_mm_per_min`、0 高度不抬刀、快移速度只影响时间不影响长度、
  没有声明这两个参数的 context 退回默认值、边界处理三种取值下的精确刀轨数与切削长度、
  非法取值报 ParameterError、notes 跟随参数。
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
