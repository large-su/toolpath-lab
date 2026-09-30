# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。0.0.1 是第一个公开版本。

## [未发布]

### 能力

- **斜坡区域**：XY 投影为方形（默认 80 × 80），以 **+X 方向最外侧边为低边**、沿 −X 方向抬起，
  斜度可调（0°–80°），最高升到 80 mm 之后转为平顶；加工面以下保留与其它区域相同的基体高度。
- **加工面抽象**：区域用 `height_at()` 描述每个 (x, y) 处的 Z、用 `surface_breaks()` 描述折痕，
  `to_positions()` 据此抬高刀路并在折痕处补点，抬刀/横移的安全面改为"最高点之上 5 mm"——
  平面与斜面因此走同一条代码路径（栅格、跟随周边、G-code、时间轴都不需要区分）。
  三维视图按后端下发的顶面分片拼出斜面工件实体。
- **球头刀**：参数目录里放开球头刀（足迹半径 0——只有刀尖接触，刀路可以贴到轮廓上），
  三维视图按"半球刀头 + 圆柱刃部"绘制；圆鼻刀仍标记为"待拓展"。
- **跟随周边（新策略）**：沿区域轮廓等距环切，一圈比一圈靠里、直到区域切满；参数为走刀顺序
  （向内 / 向外）、绕向（逆时针 / 顺时针，以俯视方向为准）、切宽、采样步长、进给速度。
  每一环绕向一致，环间沿同一条缝径向过渡一个切宽、不抬刀。
- **几何**：等距偏置与等弧长重采样从示例插件沉淀到 `planning/geometry2d.py`
  （`offset_polygon` / `resample_ring` / `inward_normals` / `distance_to_boundary`），
  内置策略与示例插件共用同一份实现。

### 修复

- **HTTP 服务**：没有读走请求体的分支（未知接口、方法不允许、超大请求体）现在会收尾清理请求体。
  此前残留字节会被当成"下一个请求"解析，而在 Windows 上带着未读数据关闭套接字会发 RST，
  客户端偶发读到 `ConnectionAbortedError` 而不是刚发出去的响应。
- **示例**：按 README 与 CONTRIBUTING 的命令 `python examples/headless_plan.py` 直接运行会
  `ModuleNotFoundError`（脚本目录进了 `sys.path`、仓库根目录没进）。现在脚本自己把仓库根目录
  加进 `sys.path`，未安装本包、未设 `PYTHONPATH` 也能跑；新增 `tests/test_examples.py` 守住这条命令。

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
