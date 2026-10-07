# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。0.0.1 是第一个公开版本。

## [未发布]

### 能力

- **覆盖率分析**：新增 `planning/coverage.py` 与 `/api/plan` 响应里的 `coverage` 字段——
  用网格（默认 0.5 mm，区域大时自动放大）比对"刀具扫过的面积"与区域面积，给出覆盖率、
  未切除面积、未切除连通块的处数 / 面积 / 位置。统计面板新增"覆盖率"与"未切除"两行；
  漏切超过 2% 时随响应给出提醒（切宽大于刀具直径、环切步距太大剩中心都会命中）。
  - 只统计真正去材料的运动（切削与连接，快移不算）；刀具按足迹半径当圆盘。
  - 实测：方形 80 + D6 + 切宽 6 → 99.1%（残留都在边界附近：圆刀切不到尖角，刀线端头之间
    也有扇形缺口）；切宽改 12 → 57.9%，6 条 6 × 74 的条带；环切切宽 12 → 59.2%，
    最大一块 1443 mm² 正在中心；用 100 方形量一条 80 的刀路 → 未切 3656 mm²（解析值 3600）。
- **环切支持"每层多环"**：凹形状的细颈被偏置吃掉后，形状会分裂成互不相连的几块，现在每块单独
  加工，而不是像以前那样把断开的顶点按原顺序接起来。
  - 旧实现在这种形状上会生成**错误刀路**：环的顶点都满足偏置量，但其中一条边横穿细颈
    （实测 d=12 的哑铃形，边中点离轮廓只有 3 mm，而要求是 12 mm）。新实现的关键不变式是
    "整条边都不越界"，`tests/test_geometry2d.py` 会沿每条边密集取样验证。
  - 过渡分两种：套在里面的环用连接进给直接走过去；同层分裂出的兄弟环抬刀到安全面再快移——
    在切削深度上横穿细颈会啃到材料。
  - 偏置几何从环切策略搬到 `planning/geometry2d.py`（按扩展指南"需要时再抄进主程序"）。
    做法：每条边向内平移并**延长到斜接点**（延长量按顶点转角精确算：`d·tan(转角/2)`）、
    每个凹角补一段圆弧并离散成弦；在所有交点处切分（交点坐标两边共用，斜接点因此成为共享节点）；
    只保留"在多边形内部、且到边界距离 ≈ 偏置量"的子段；最后按"转角最小"接成闭环。
    方形 / 矩形的偏置面积现在精确等于解析值（74²、80×40 …），U 形还多出凹角圆弧的鼓包。
- **新增哑铃形区域**（`dumbbell`，总长 / 方头边长 / 细颈宽，默认 160 / 60 / 20）：细颈被偏置
  吃掉后一层分裂成两条环，用来验证上面的能力；界面、`/api/catalog` 与 `POST /api/plan`
  都自动包含它，刀路与前端一行特判都没加。
- **U 形区域（凹多边形）**：新增 `u_shape`（外宽 / 外高 / 壁厚，默认 100 / 80 / 25）。
  它验证了两件事：一条横穿两条臂的扫描线会切出**两段独立刀轨**（同一水平层出现两刀），
  而工件会被挤出成真正的 U 形而不是方盒——刀路与前端都没有为它加任何特判。
  壁厚 >= 外宽的一半时 `__post_init__` 直接报 `ParameterError`（那样两条臂会贴在一起）。
- **界面新增「导出 CSV」按钮**：`api.js` 的下载逻辑从写死 gcode 改成 `downloadExport(kind, payload)`
  （文件名仍由后端的 `Content-Disposition` 决定，缺失时按格式回退），顶栏因此有两个导出按钮。
- **CSV 点表导出**：新增 `POST /api/export/csv` 与 `export/csv.py`（纯函数 `toolpath_to_csv`），
  一个刀点一行：`move_index,pass_index,kind,feed_mm_per_min,point_index,x_mm,y_mm,z_mm`，
  非切削段的 `pass_index` 为 -1。刻意保持纯 ASCII（不带 BOM），Excel、pandas 与 `csv.reader`
  都能直接读。导出路由里重复的时间戳/文件名顺手收进 `_export_filename()`。
- **区域形状新增矩形与椭圆**：`rectangle`（宽 W × 高 H，默认 100 × 60）与 `ellipse`
  （长半轴 a / 短半轴 b，默认 60 / 40，长半轴沿 X）。界面"区域 → 形状"、`/api/catalog`
  与 `POST /api/plan` 都会自动包含它们，刀路代码一行没改。
  - 圆与椭圆共用 `CURVE_SEGMENTS`（原 `CIRCLE_SEGMENTS`）作为折线逼近段数。
  - 三维工件不再区分形状：`viewport.js` 从"方形/圆形特判"改成**按区域边界多边形挤出**
    （`ExtrudeGeometry`，上表面落在 Z = 0），因此任何新形状——包括凹多边形——都能正确显示。
  - 统计面板的"区域"一行改为按该形状自己声明的参数生成（"宽 W 100 mm · 高 H 60 mm"），
    同样不再特判形状。
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

- `Choice(disabled=True)` 的取值现在会被参数层拒绝（`ParameterError` → HTTP 400）：
  「界面上不可选、接口却能传」是自相矛盾的状态，而球头刀 / 圆鼻刀在没有刀尖圆角参数与三维
  刀型显示之前，按平底刀算出来的结果是错的。领域层不受影响——`Tool(ToolKind.BALL, ...)` 直接
  构造仍然可用（`test_tool.py` 里两条测试分别钉住这两件事）。
- 三维视图里快移段按**真实 Z** 绘制（原先所有刀路都被拍平到 Z = 0.05，安全高度改了也看不出来）：
  切削 / 连接段与已走轨迹仍然只抬高到 0.05 mm 防 z-fighting，抬刀段保留真实高度。
- 播放到结尾时播放键图标不复位：`Playback.update()` 在自然播放结束时没有通知状态变化，
  界面因此停在"暂停"图标上；现在结束时也会 `_notify()` 一次。
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
  没有声明这两个参数的 context 退回默认值、边界处理与边界余量取值下的精确刀轨数与切削长度、
  非法取值报 ParameterError、notes 跟随参数。
- 新增 `tests/test_contour.py`（24 项）：等距偏置的数值断言（方形内缩保持方形、圆形内缩后
  半径、超过内切半径退化、重采样等弧长）、环数与切削长度的精确值、闭环与 Z=0、方向交替、
  环间不抬刀、安全高度与快移进给、几何不可行。
- `tests/test_region.py` 扩到六形状：矩形 / 椭圆 / U 形 / 哑铃形的边界数值（面积、包围盒、
  点都落在椭圆上、半轴互换只是旋转 90°、U 形与哑铃形的扫描线区间数）、
  新增 `ShapeContractTests`（每个形状都必须逆时针、不重复首点、包围盒对称），
  `tests/test_planners.py` 新增 `EveryShapeTests`（六种形状 × 两个策略都能规划）与
  "凹形状同一层出两刀"的断言。
- `tests/test_geometry2d.py` 新增偏置测试：方形 / 矩形的偏置面积精确值、圆形偏置后的半径、
  超过内切半径返回空、负偏置报错、最小面积过滤、**沿每条边密集取样验证不越界**、
  环的简单性与逆时针、细颈分裂成两条环、分裂后的面积与解析值一致、U 形凹角圆弧鼓包，
  以及 `distance_to_boundary` / `point_in_polygon` / `resample_ring` 的数值断言。
- `tests/test_contour.py` 新增 `MultiLoopTests`：哑铃形一层分裂成两条环、每条环只绕一个方头、
  兄弟环之间抬刀快移而嵌套环之间连接进给、U 形仍然每层一条环。
- 新增 `tests/test_coverage.py`（14 项）：先拿能算解析值的例子校准——一次走刀覆盖整个窄区域、
  切宽大于刀具直径留 6 条条带、用大区域量小刀路差 3600 mm²、圆刀只在边界附近留残、环切步距
  大时留下中心那一块；另有网格细化 / 上限、覆盖率与未切除之和等于区域面积、提醒阈值、
  快移不算去材料而连接算。
- `tests/test_timeline_export.py` 新增 `CsvTests`（列名、行数 = 刀点数、逐点坐标与 `move.points`
  一致、非切削段 pass_index、纯 ASCII、decimals 选项），`tests/test_api.py` 的 `ExportTests`
  新增 CSV 下载与"CSV 行数与 /api/plan 的刀点数一致"，`PlanTests` 新增"待拓展"刀具类型返回 400。

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
