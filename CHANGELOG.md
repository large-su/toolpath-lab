# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。0.0.1 是第一个公开版本。

## [未发布]

### 新增

- **刀路**：
  - **环切 contour**：沿轮廓逐圈等距向内偏置，一圈一刀，每刀独立成闭合刀轨；
    刀间可抬刀返回或直接连接，旋向可选。适合精修轮廓或留出台阶。
    凹形状每层只保留面积最大的一块环，窄颈区域会被跳过（已在刀路备注里注明）。
- **区域**：圆角矩形（宽 W / 高 H / 圆角半径 R）与椭圆（长半轴 a / 短半轴 b）。
  圆角矩形取 R = 短边一半即跑道形，W == H 且 R == W/2 退化为圆形；椭圆 a == b 时为圆形。
- **导出格式**：刀点表 CSV（逐点一行，可 `export.exclude_rapid` 只取切削轨迹）
  与刀路 JSON 快照（`POST /api/export/csv`、`/api/export/json`），
  界面「导出」按钮新增格式选择。
- **工艺设置**：安全高度、快移速度、边界处理方式从固定常量改成可调参数
  （`planning/setup.py`，请求里的 `setup` 分组）。边界处理有四种模式：
  内缩一个刀具半径（默认）、不偏置、外扩留边、自定义偏置量。
  `/api/catalog` 里的 `fixed` 分组保留为兼容字段。
- **刀具**：球头刀与圆鼻刀从"待拓展"改为可用，三维视图按刀尖类型成型
  （平面 / 半球 / 带圆角的平面，`viewport.js` 的 `toolProfileGeometry`）。

### 修复

### 重构

- 等距偏置几何（`offset_polygon` / `resample_ring`）从示例插件提升到
  `planning/geometry2d.py`，新增 `collect_rings` 统一"逐圈偏置"的循环，
  并区分"材料用尽"与"圆角自交提前结束"——后者会 warn() 提醒用户中心没切到。
  螺旋铣与环切共用这套几何，示例插件不再自带副本。

### 已知限制

- 球头刀与圆鼻刀目前只影响**边界偏置量**与三维外形；刀路仍是平面加工的一刀两个点，
  没有建模残留高度（球头刀的扇贝形痕）与刀轴姿态。

### 修复

- **HTTP/1.1 连接错乱（接口测试偶发失败）**：POST 到未匹配的路由时，服务端直接回 404
  而**不读请求体**。保持连接下残留的字节会被下一个请求当成开头解析，整条连接就此错乱，
  客户端表现为 `ConnectionAbortedError` / `ConnectionResetError`
  （WinError 10053 / 10054）。现在请求体在路由之前一定被读完（缓存后复用），
  超限时也会先抽干再回 400，避免客户端写数据时收到 RST。
- **毛坯不再永远是方块**：三维视图里的工件实体改为由 `region.boundary` 多边形拉伸而成。
  此前 `_workpiece()` 只对 `circle` 特判、其余一律退化成 `BoxGeometry`，且只取 X 方向跨度，
  因此圆角矩形、椭圆这类非方形区域的毛坯与刀路对不上。
- **切宽范围在三个策略间不一致**：栅格的 `stepover_mm` 上限 100 mm，螺旋与环切却是 50 mm，
  切策略时同一个值会突然 400。现在三者统一为 0.5–100 mm。
- **圆鼻刀足迹半径算错**：`footprint_radius_mm` 用 `R − Rc`，但 `corner_radius_mm`
  属性对 bull 恒返回 0，公式退化成 `R`，圆鼻刀一直等价于平底刀。
- **`examples/headless_plan.py` 无法直接运行**：`sys.path[0]` 是 `examples/` 而非项目根，
  `import toolpath_lab` 失败。现在脚本会自己把项目根补进 `sys.path`。
- 清理死代码：删掉未使用的 `csv_header_comment` 与 `describe_contour`；
  `EXPORT_FORMATS` 之前只写在文档里没人用，现在由 `_export` 真正校验，
  登记了格式却忘了写分支会明确报错而不是静默失败。

## [0.0.1] - 2026-09-10

第一个公开版本：刀路规划基座，包含刀具与规则区域建模、往复/单向栅格刀路、桌面三维可视化与按进给播放。

### 能力

- **刀具**：平底刀（直径、长度）。球头刀 / 圆鼻刀在参数目录里标记为"待拓展"、界面上不可选，
  足迹半径的公式三条分支都已写好。
- **区域**：方形（边长）与圆形（直径），加工面固定为 XY 平面。
- **刀路**：
  - 栅格刀路：平行扫描线，**往复 Zigzag** 与**单向 One-way** 两种模式；
  - 螺旋铣：沿轮廓逐圈向内收缩的连续刀路，全程只下一次刀；支持逆/顺时针旋向、
    中心留残料圆，进给速度可随半径线性变化以保持径向切深恒定。
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
