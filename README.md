<p align="center">
  <img src="toolpath_lab/web/icon.png" width="128" alt="ToolpathLab">
</p>

# ToolpathLab · 刀路规划基座

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

ToolpathLab 有两块能力，共用同一个三维视口与后端：

- **实验台**：给定一把刀具和一块规则形状的加工区域，生成栅格刀路，按进给速度播放整个加工过程；
- **CAM 加工**：导入 STEP 零件 → 创建毛坯 → 工序树 → 自动编程（平面铣 / 型腔铣）→
  **毛坯切除仿真** → 导出 NC 程序。

后端是 Python，底层几何计算交给成熟库（OCP / pyclipper / opencamlib / trimesh / ezdxf），
前端是原生 ES 模块加 three.js，桌面窗口由 Electron 提供。
刀具、区域、毛坯、加工参数都用声明描述，参数面板根据后端的参数声明自动生成。

![界面](docs/images/screenshot.png)

## 功能

### CAM 加工（导入模型 → 出程序）

- **导入 STEP/IGES**：由 OpenCascade（OCP）读取 BRep，支持平面、圆柱、圆锥、球、环面、B 样条曲面，
  自动修复与归一化后离散成三角网格并按面渲染；支持面拾取（点一下就能选中加工面）。
  损坏文件、格式错误、超大文件分别返回 400 / 413 / 422。
- **毛坯**：矩形块或圆柱，按零件包容盒在 X/Y/Z 方向外扩；实时预览、可切换、可重置。
- **刀具库**：对标 UG/NX 的刀具管理。可新建刀具、选刀具类型（平底刀 / 球头刀 / 圆鼻刀 /
  锥度铣刀 / 钻头 / 丝锥 / 铰刀 / 镗刀）、填各类刀具参数、自定义刀具名称与备注；
  支持复制、删除、恢复出厂刀具。刀库是**全局**的（`tools.json`），与工程无关。
  工序直接引用库里的刀，刀路计算、仿真与 NC 头部注释读的都是同一把刀；
  在库里改了直径，重新生成刀路立刻生效，界面上也不再让人手改那几个尺寸。
- **工序树**：序号 / 名称 / 加工类型 / 参数 / 状态，支持排序、改名、复制、启用禁用、整体导出；
  每生成一步程序就自动同步节点。
- **自动编程**：平面铣（分层往复 / 单向 + 精修轮廓）、型腔铣（环切 / 平行扫描，自动避让岛屿，
  **底面支持水平面、斜面与曲面**）、轮廓铣，以及**三维曲面**的平行行切（opencamlib 落刀，
  可转走刀方向、往复/单向）与等高铣（OCP 分层剖切 + pyclipper 偏置，外轮廓与内腔一起出）。
  参数可配置并存成模板；曲面加工可以只加工选中的面，也可以不选面直接加工整个零件。
  - **多加工面的切削顺序（对标 UG/NX）**：选多个面时按几何高度统一排层，
    **层优先**（默认——各面在同一高度合并加工、逐层下切）或**深度优先**
    （一个面从上到下切完再换下一个），不再受选面顺序牵制。
  - **层内连续切削**：一层里的环间、行间、壁精修转移优先在**层内平移**衔接，
    一层切完才斜降到下一层——型腔从入口**一次下刀**逐层切到底，不再每段空程
    都抬到安全面再插下来；穿岛、跨区域等出界情况仍会安全抬刀，单向（one-way）
    仍保持每刀抬刀的语义。
  - **几何障碍判断（逐层连通）**：选中面的孔 / 内环**并入同一加工区域**，区域连通
    改按"本层高度处的几何遮挡"逐层判定：孔上方无障碍就与周边平面连通、合并到同一层
    加工；层内没有凸台时整层按一个连续平面出刀；孔上或面上有凸台时，低于其顶面的层
    自动绕开并留出刀具半径的避让，层高越过凸台顶后恢复连通。零件上没有高出加工底面的
    几何时判据不裁，刀路与旧版逐字节一致。
  - **斜面 / 曲面型腔**：底面高度由**高度场**逐点给出（平面走解析平面方程、曲面用三角面片
    光栅化），刀轴 Z 逐点取 `max(层高, 底面高度 + 防过切抬升)`。
    抬升量只与底面坡度与刀具形状有关：水平面时为 0（与老行为完全一致）、
    平底刀在坡度 θ 上抬 `r·tanθ`、球头刀约一半。层高落到某处底面之下时，
    那一块会从"本层可切区域"里裁掉，刀不会平着切过去。
    已知的几何限制：平底刀在**斜面与竖直侧壁的交角**处必然留一小块三角残料
    （球头/圆鼻刀才能贴进那个角），刀路备注里会写出来。
- **毛坯切除仿真**：Z-Map 高度图材料去除，刀具沿刀路运动时毛坯被真实削掉，
  支持播放 / 暂停 / 单步 / 重置 / 回放 / 变速，结束后给出剩余体积与零件体积偏差。
- **工程**：模型 + 毛坯 + 参数 + 工序树存成工程文件，重启后恢复。

### 实验台（刀路基座）

- **刀具**：平底刀，可设置直径与长度。刀具在加工面上的足迹半径决定刀路相对区域轮廓的偏置量。
  实验台这条链路只认平底刀；要比这更丰富的刀具几何，用 CAM 的**刀具库**。
- **区域**：方形（边长）与圆形（直径），以原点为中心，加工面为 XY 平面。
- **刀路**：栅格刀路的两种模式
  - **往复 Zigzag**：奇数刀反向，相邻两刀在端头直接连过去；
  - **单向 One-way**：每刀同向，刀与刀之间抬刀到安全面再回到起点。
- **参数**：切宽、走刀方向角、进给速度。安全高度、快移速度、边界处理方式等为固定值，见[配置常量](#配置常量)。
- **三维视图**：工件实体、区域轮廓、刀路（切削 / 连接 / 快移分色）、刀具实体、已走轨迹、实时阴影。
- **播放**：按每段运动自己的进给速度做时间参数化，支持播放 / 暂停、拖动进度，并给出切削长度与预计工时。
- **导出**：NC 程序（G-code，G21 / G90 / G17 加 G0 / G1 带 F）。
- **HTTP 接口**：能力目录、规划、导出三个接口，便于脚本调用与集成。

## 界面

左侧是参数面板（CAM 模式下还有工序树），右侧是三维视图、统计与播放条。

| 鼠标操作 | 功能 |
| --- | --- |
| 左键拖动 | 旋转视角 |
| 中键滚轮 | 缩放 |
| 右键拖动 | 平移 |
| 左键单击（CAM 拾取模式下） | 选中加工面（Shift 多选） |

- **模式切换**（顶栏中部）：实验台 / CAM 加工。
- **视图工具条**（顶部居中）：最佳 / 前 / 后 / 左 / 右 / 上 / 下；再次点击当前方向会切换到对面。
- **外观开关**（左上角）：实时阴影、白色背景、网格地面。
- **拾取工具条**（CAM 模式，左上角）：开启面拾取、清除选择，并显示已选面。
- **播放条**（底部）：播放 / 暂停（空格键同样有效）、回到起点、单步、拖动进度、速度、当前时间。
- **统计面板**（右上角）：实验台显示区域尺寸 / 刀轨 / 刀点 / 切削长度 / 工时；
  CAM 显示工序、刀轨、切削长度与（仿真后）切除率与体积。

参数面板由后端 `/api/catalog` 返回的参数声明生成：新增区域形状、刀路策略、毛坯类型或加工类型后，
界面上会自动出现对应的控件，不需要修改前端代码。

## CAM 使用流程

1. 顶栏 **导入模型** → 选一个 `.step` / `.stp`（仓库里有示例 `examples/sample_plate.step`）；
2. 打开左上角 **拾取面**，在三维视图里点击要加工的面（加工面必须朝上，Shift 可多选）；
3. 在左侧 **毛坯** 一栏选类型、调偏移，点 **生成毛坯**；
4. 在左侧 **刀具** 一栏点 **刀具库**，新建一把刀（选类型、填参数、起名字），
   在工序的 **刀具** 下拉框里选中它；
5. 在 **工序** 一栏选加工类型、调加工参数，点顶栏 **生成刀路**（或工序树里的 **新增工序**）；
6. 需要多道工序时在工序树里继续新增，可排序、改名、禁用；
7. 点顶栏 **切削仿真** 看毛坯被逐层切除的动画（空格播放 / 暂停，进度条可拖动，可变速）；
8. 点 **导出 NC** 出程序（单道工序或全部启用工序）。

> 刀具库放在顶栏与左侧面板两处入口；对话框里左栏是刀具列表、右栏是参数表单，
> 选中刀具时三维视图里会同步显示它的形状。曲面工序（平行行切 / 等高铣）暂时仍用
> 面板上的刀具类型与直径，还没有接刀具库。

## 环境要求

- Python 3.10 及以上，numpy（`pip install -r requirements.txt`）；
- 桌面窗口需要 Node.js 18 及以上与 Electron（`npm install` 自动获取，约 200 MB）；
- 没有 Node.js 时仍可使用浏览器方式运行，功能一致。

## 安装与启动

**Windows**：双击 `start.bat`。脚本会查找可用的 Python（必要时创建 `.venv` 并安装 numpy）、
确认 Electron 是否就绪（首次会执行 `npm install`），然后打开桌面窗口。

**手动启动**：

```bash
git clone https://github.com/large-su/toolpath-lab.git
cd toolpath-lab

pip install -r requirements.txt
npm install

npm start                 # 桌面窗口（自动拉起 Python 后端）
python -m toolpath_lab    # 只用后端 + 浏览器：http://127.0.0.1:8770/
```

命令行参数：`--host`、`--port`、`--no-browser`。

## 使用

### 脚本调用

```bash
python examples/headless_plan.py
```

该示例不使用界面，直接生成一条刀路、打印统计信息并导出 NC 文件，
可以当作把 ToolpathLab 当作库使用的起点：

```python
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan

outcome = run_plan(
    planner_id="raster",
    tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
    region=build_region("square", {"side_mm": 80.0}),
    parameters={"mode": "zigzag", "stepover_mm": 6.0, "feed_mm_per_min": 800.0},
)
print(outcome.toolpath.statistics())
```

CAM 链路同样可以脱离界面调用：

```python
from toolpath_lab.cam.service import CAMOperationRequest, execute_operation
from toolpath_lab.core.part import build_part
from toolpath_lab.core.stock import build_stock
from toolpath_lab.simulation import simulate_toolpath
from toolpath_lab.brep import import_model

model = import_model("examples/sample_plate.step")
part = build_part(model, model_id="demo")
stock = build_stock("rectangular", part, {"offset_x_mm": 2, "offset_y_mm": 2, "offset_z_mm": 2})

# 选一个朝上的平面面作为加工面
face = next(f for f in part.features if f["horizontal"])

request = CAMOperationRequest.from_payload(
    {"kind": "pocket_mill", "faces": [face["face_id"]],
     "parameters": {"tool_diameter_mm": 10, "stepover_ratio": 0.5, "cut_depth_mm": 2},
     "stock": stock},
    part,
)
result = execute_operation(request)
print(result.toolpath.statistics())

simulation = simulate_toolpath(result.toolpath, stock, tool_radius=5.0)
print(simulation.summary())
```

### HTTP 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查与版本号 |
| `GET /api/catalog` | 能力目录：区域形状、刀路策略、毛坯类型、加工类型、参数声明、默认值与固定值 |
| `POST /api/plan` | 生成刀路，返回刀路运动段、统计与播放时间轴 |
| `POST /api/export/gcode` | 导出 NC 程序（实验台） |
| `POST /api/import/step` | 导入 STEP（`multipart/form-data` 文件，或 JSON 里的 `content` 文本/base64） |
| `GET /api/model` | 当前工程的模型（含网格与面拓扑） |
| `GET /api/model/features` | 可加工的平面面清单（法向、面积、是否朝上） |
| `GET/POST /api/stock` | 读取 / 设置毛坯（含预览网格） |
| `GET/POST /api/parameters` | 全局加工参数与后处理参数 |
| `GET/POST /api/operations` | 工序树；`POST` 新增并立即生成刀路 |
| `POST /api/operations/<id>` | 改名 / 改参数 / 改面 / 启用禁用（改参数会把状态标回"待生成"） |
| `POST /api/operations/<id>/{generate,duplicate,move}` | 生成 / 复制 / 调整顺序 |
| `DELETE /api/operations/<id>` | 删除工序 |
| `POST /api/operations/generate` | 按顺序生成全部启用工序 |
| `POST/DELETE /api/templates[/<id>]` | 参数模板 |
| `GET/POST /api/tools` | 刀具库：列表 + 刀具类型目录 / 新建刀具 |
| `GET/POST/DELETE /api/tools/<id>` | 读取（含被哪些工序引用）/ 修改 / 删除一把刀 |
| `POST /api/tools/<id>/duplicate`、`POST /api/tools/restore` | 复制一把刀 / 补回出厂刀具 |
| `POST /api/simulate` | 毛坯切除仿真（可按工序、按请求规划、或整条工序链） |
| `POST /api/export/nc` | 导出 NC 程序（单道工序或全部启用工序） |
| `GET /api/projects`、`POST /api/projects/open`、`DELETE /api/projects/<id>` | 工程列表 / 打开 / 删除 |

```bash
curl http://127.0.0.1:8770/api/catalog

curl -X POST http://127.0.0.1:8770/api/import/step \
  -F "file=@examples/sample_plate.step"

curl -X POST http://127.0.0.1:8770/api/operations \
  -H "Content-Type: application/json" \
  -d '{"kind":"pocket_mill","faces":[197],
       "parameters":{"tool_diameter_mm":10,"stepover_ratio":0.5,"cut_depth_mm":2}}'

curl -X POST http://127.0.0.1:8770/api/tools \
  -H "Content-Type: application/json" \
  -d '{"name":"D10R1 圆鼻刀","kind":"bull_nose_mill",
       "values":{"diameter_mm":10,"corner_radius_mm":1,"flute_length_mm":25,"length_mm":60}}'

# 工序引用刀具库里的刀：刀路、仿真与 NC 都按这把刀的几何算
curl -X POST http://127.0.0.1:8770/api/operations \
  -H "Content-Type: application/json" \
  -d '{"kind":"pocket_mill","faces":[197],
       "parameters":{"tool_id":"tool-flat-d10","stepover_ratio":0.5,"cut_depth_mm":2}}'

curl -X POST http://127.0.0.1:8770/api/simulate \
  -H "Content-Type: application/json" -d '{"cell_mm":0.6}' -o simulation.json

curl -X POST http://127.0.0.1:8770/api/plan \
  -H "Content-Type: application/json" \
  -d '{"tool":{"diameter_mm":6,"length_mm":30},
       "region":{"shape":"circle","parameters":{"diameter_mm":80}},
       "planner":{"id":"raster","parameters":{"mode":"one_way","stepover_mm":6}}}'
```

参数非法返回 `400`；参数合法但几何上无法加工（例如刀具直径大于区域尺寸）返回 `422`，
响应体中的 `error` 字段给出具体原因。

## 项目结构

```
toolpath_lab/
  core/        领域层：参数声明、刀具、区域、刀路、零件、毛坯、工序、离散模型
  brep/        BRep 层（OCP）：STEP/IGES 读取、拓扑修复、Z 层剖切、BRep → 三角网格
  contour2d/   2D 轮廓层（pyclipper）：多边形布尔、偏置（刀具半径补偿）、环切刀路
  surfacing/   三维曲面层（opencamlib + OCP/pyclipper）：平行行切、等高铣
  cam/         加工层：加工区域（栅格 + 距离）、平面铣 / 型腔铣 / 轮廓铣、工序编排
  planning/    策略层：Planner 基类与注册表、平面多边形几何、栅格刀路
  simulation/  时间层与仿真：按进给速度做时间轴、毛坯切除（Z-Map）
  export/      G-code / CAM 程序导出
  storage/     持久化：工程仓库（JSON + npz）与刀具库（tools.json）
  server/      标准库 HTTP 服务：接口路由、请求校验、能力目录、multipart、静态文件
  web/         前端：原生 ES 模块 + three.js（随仓库提供，无打包步骤）
electron/      桌面壳：拉起 Python 后端并承载窗口（含 smoke.mjs 无头自检）
examples/      命令行示例、示例插件与示例 STEP 模型
tests/         单元测试
docs/          架构与扩展文档
```

依赖方向是单向的：`core` 不依赖其它层；`brep` / `contour2d` / `surfacing` / `planning` 只依赖
`core` 与各自的第三方库；`cam` / `simulation` / `export` / `storage` 依赖 `core` 及需要的几何层；
`server` 负责组装，`web` 只通过 HTTP 与后端通信，`electron` 只负责窗口。
因此刀路算法可以脱离界面单独运行。详见 [docs/architecture.md](docs/architecture.md)。

库的职责边界（不交叉）：**OCP** 只处理 BRep（读取、拓扑、剖切、离散），不做刀路计算；
**pyclipper** 只处理 2D 轮廓（布尔、偏置、环切）；**opencamlib** 只接收三角面片网格，生成三轴
曲面刀路；**trimesh** 负责网格预处理与预览；**ezdxf** 负责 DXF 二维轮廓读取。

## 配置常量

以下数值定义在代码中，不在界面上暴露：

| 常量 | 值 | 位置 |
| --- | --- | --- |
| 安全高度 | 5 mm | `toolpath_lab/planning/base.py` |
| 快移速度 | 5000 mm/min | `toolpath_lab/planning/base.py` |
| 边界处理 | 刀路相对区域轮廓内缩一个刀具足迹半径 | `toolpath_lab/planning/raster.py` |
| 每刀采样 | 两个端点（加工面为平面） | `toolpath_lab/planning/raster.py` |
| CAM 栅格间距 | 0.4 mm（可按工序改） | `toolpath_lab/cam/boundary.py` |
| 仿真格距 | 0.5 mm / 最多 180 帧 | `toolpath_lab/simulation/cut_sim.py` |
| STEP 体积上限 | 32 MB | `toolpath_lab/brep/model.py` |

把它们改成可在界面上调整的参数，做法见 [docs/extending.md](docs/extending.md)。

## 扩展

- **新增刀路策略**：继承 `Planner`，声明参数并实现 `plan()`，然后注册。
  [examples/plugins/contour_planner.py](examples/plugins/contour_planner.py) 是一个可直接使用的
  环切（等距轮廓）实现，复制到 `toolpath_lab/planning/` 并在 `__init__.py` 中导入一行即可启用。
- **新增区域形状**：实现一个返回逆时针边界多边形的 `boundary()`，栅格刀路与三维显示会自动适配。
- **新增加工类型**（例如钻孔、插铣）：在 `cam/` 下写一个 `plan_xxx(context)`，
  在 `cam/service.py` 的 `execute_operation` 里加一个分支，再在
  `toolpath_lab/core/operation.py` 的 `OperationKind` 里加一个枚举值——界面上的加工类型下拉框
  与工序树的类型标签会自动出现。**三维曲面类**的加工类型要在 `SURFACE_KINDS` 里登记，
  这样才不需要拾取面、并会去 `surfacing/` 取几何。
- **新增毛坯类型**：继承 `Stock`、声明参数、注册到 `STOCK_TYPES`。
- **新增刀具类型**（例如螺纹铣刀）：在 `core/tool.py` 的 `TOOL_TYPES` 加一项、给它的参数补
  `visible_if`、在 `TOOL_KIND_BY_TYPE` 里说明它在刀路里按哪种几何算——界面上的类型下拉框与
  参数表单会自动出现，详见 [docs/extending.md](docs/extending.md) 第 8 节。
- **新增模型格式**：在 `toolpath_lab/brep/` 旁边写一个 reader，返回 `BrepModel` 再走
  `to_tessellated_model()`，或者直接返回同一个 `TessellatedModel`，
  下游（毛坯、特征、刀路、仿真）完全不用改。
- **新增导出格式**：在 `export/` 中写一个纯函数，并在 HTTP 路由中加一个分支。
- 完整说明见 [docs/extending.md](docs/extending.md)，开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 测试

```bash
python -m unittest discover -s tests
```

468 项测试，覆盖几何裁剪、刀路模式与安全高度、时间参数化、G-code 导出、HTTP 接口与静态资源、
BRep 读取与离散、Z 层剖切、2D 轮廓布尔与偏置、三维曲面刀路、加工区域与刀路正确性、
毛坯切除仿真、刀具库（类型目录 / 参数归一 / 几何换算 / JSON 持久化 / 与工序打通）、
工程持久化与 CAM 接口，
以及**持久连接复用**（同一条 TCP 连接上连续发请求，浏览器就是这么用的）。

前端自检（会真的拉起一个窗口加载界面，检查控制台错误与关键 DOM）：

```bash
node_modules/electron/dist/electron.exe electron/smoke.mjs   # Windows
node_modules/.bin/electron electron/smoke.mjs               # Linux / macOS
```

## 设计说明

刀路模型采用机械加工中常见的平行扫描线形式，时间轴按各段运动的进给速度累加，
三维交互沿用通用的三维 CAD 操作习惯（左键旋转、中键缩放、右键平移）。
CAM 部分刻意把"加工区域"表达成栅格 + 到轮廓的精确距离：外轮廓、岛屿、复杂型腔因此
共用同一套代码，等距轮廓与刀路都从它导出；毛坯切除仿真则用 Z-Map（每个 XY 位置记录剩余高度），
这正是 2.5 轴铣削最自然的数据结构。

## 许可

[MIT](LICENSE)
