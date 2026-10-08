<p align="center">
  <img src="toolpath_lab/web/icon.png" width="128" alt="ToolpathLab">
</p>

# ToolpathLab · 刀路规划基座

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

ToolpathLab 是一个刀路规划基座：给定一把刀具、一块加工区域与一个加工面，生成栅格刀路，
在三维窗口中显示工件（或导入的模型）、刀路与刀具，并按进给速度播放整个加工过程。

后端是纯 Python（只依赖 numpy），前端是原生 ES 模块加 three.js，桌面窗口由 Electron 提供。
刀具、区域与加工面都用参数描述，参数面板根据后端的参数声明自动生成。

![界面](docs/images/screenshot.png)

## 功能

- **刀具**：平底刀与球头刀，可设置直径与长度。刀具在加工面上的足迹半径决定刀路相对区域轮廓的偏置量——
  平底刀内缩一个半径，球头刀只有刀尖接触加工面（内缩量为 0），刀路说明里还会给出相邻两刀之间的
  理论残留高度。刀具实体由 `tool.py` 发布的回转轮廓旋成，所以球头刀的半球刀尖不需要额外画。
- **区域**：方形（边长）、圆形（直径），以及**导入模型的投影轮廓**（凸包 / 包围盒，可外扩或内缩）。
- **加工面**：刀路的 Z 不再固定为 0，而是取自加工面 `z = f(x, y)`：
  - **平面**（默认，等价于以前的行为）；
  - **斜面**：按倾角线性抬升，用来看刀路在斜面上的抬降；
  - **波浪面**：正弦起伏的曲面；
  - **导入模型**：用 STL 的 Z-map 高度场，把平面栅格刀路变成三轴曲面刀路
    （可取"最高面"加工凸台/外表面，或"最低面"加工凹腔底面）。
  非平面加工面下每条刀线按"离散步长"取点（三轴联动，刀轴恒为 +Z），
  安全高度自动抬到加工面最高点之上 5 mm。
- **导入模型**：把 `.stl`（二进制或 ASCII）上传到后端内存模型库，界面上直接出现模型选择框；
  导入后区域自动切到"模型轮廓"、加工面自动切到"导入模型"、毛坯自动切到"模型包容体"，
  三维视图里直接画出模型实体。
- **毛坯**：模型的最小六面体包容体（轴对齐包围盒），可 XY 外扩、顶面抬高；
  三维视图里画成半透明长方体，安全平面自动抬到毛坯顶面之上。这是分层粗加工的起点。
- **切深（分层粗加工）**：`切深 ap > 0` 时，刀路从毛坯顶面按 `Z = 顶面 − k·ap` 一层层往下切，
  每层只切该层还有材料的地方（加工面低于该层），层与层之间抬刀横移、以进给速度下刀；
  最后可沿加工面补一刀精加工。切深为 0（默认）时不分层，行为与以前完全一致。
- **刀路**：栅格刀路的两种模式
  - **往复 Zigzag**：奇数刀反向，相邻两刀在端头直接连过去；
  - **单向 One-way**：每刀同向，刀与刀之间抬刀到安全面再回到起点。
- **参数**：切宽、走刀方向角、进给速度、离散步长、切深、是否精加工。安全高度、快移速度、
  边界处理方式等为固定值，见[配置常量](#配置常量)。
- **三维视图**：工件或导入模型、**毛坯**、区域轮廓、刀路（切削 / 连接 / 快移分色）、刀具实体、
  已走轨迹、实时阴影。
- **播放**：按每段运动自己的进给速度做时间参数化，支持播放 / 暂停、拖动进度，并给出切削长度与预计工时。
- **导出**：NC 程序（G-code，G21 / G90 / G17 加 G0 / G1 带 F）。
- **HTTP 接口**：能力目录、规划、导出、模型管理，便于脚本调用与集成。

## 界面

左侧是参数面板（刀具 / 模型 / 毛坯 / 区域 / 加工面 / 刀路 / 显示 / 固定设置），右侧是三维视图、统计与播放条。

| 鼠标操作 | 功能 |
| --- | --- |
| 左键拖动 | 旋转视角 |
| 中键滚轮 | 缩放 |
| 右键拖动 | 平移 |

- **视图工具条**（顶部居中）：最佳 / 前 / 后 / 左 / 右 / 上 / 下；再次点击当前方向会切换到对面。
- **外观开关**（左上角）：实时阴影、白色背景、网格地面。
- **播放条**（底部）：播放 / 暂停（空格键同样有效）、回到起点、拖动进度、当前时间。
- **统计面板**（右上角）：区域尺寸、加工面、刀轨条数、刀点数量、切削长度、预计工时。

![俯视图](docs/images/screenshot-top.png)

参数面板由后端 `/api/catalog` 返回的参数声明生成：新增区域形状、加工面或刀路策略后，
界面上会自动出现对应的控件，不需要修改前端代码。

## 使用

### 导入模型

点击参数面板里的**模型 · 导入 STL…**，选一个 `.stl` 文件即可（二进制与 ASCII 都支持）。
导入后：

1. 区域自动切到**模型轮廓**（默认取投影凸包，也可以换成矩形包围盒，并能整体外扩 / 内缩）；
2. 加工面自动切到**导入模型**（默认取模型最高面，也可以取最低面加工凹腔底面，并可调整 Z-map 分辨率）；
3. 三维视图里直接显示模型实体，刀路叠在零件的真实表面上。

模型保存在后端进程的内存里（最多 8 个，超出按先进先出淘汰），重启后需要重新导入。

### 分层粗加工（毛坯 + 切深）

导入模型之后，把参数面板里 **毛坯 · 类型** 选成**模型包容体**（模型的最小六面体包容体，
可以 XY 外扩、顶面抬高来留加工余量），再到 **刀路** 里把 **切深 ap** 设成大于 0，
刀路就会从毛坯顶面开始一层层往下切：

- 每层是 `Z = 毛坯顶面 − k·ap` 的水平刀路，只在该层还有材料的地方（加工面低于该层）切削；
- 同一层里两段刀路之间如果立着材料（凸起），会自动抬刀绕过去；
- 层与层之间抬刀横移，并且以进给速度垂直下刀（不是快移扎刀）；
- `最后精加工` 打开时，还会沿加工面再走一刀，把留下的余量切干净；关掉就只留粗加工分层。

切深留 0（默认）表示不分层，此时毛坯只参与显示与安全平面计算，刀路和以前一样沿加工面走。

```bash
# 上传：把 STL 内容做 base64 后 POST，返回的 model.id 供 /api/plan 引用
curl -X POST http://127.0.0.1:8770/api/models \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"part.stl\",\"data_base64\":\"$(base64 -w0 part.stl)\"}"

curl -X POST http://127.0.0.1:8770/api/plan \
  -H "Content-Type: application/json" \
  -d '{"model":{"id":"m-1a2b3c4d"},
       "region":{"shape":"model","parameters":{"outline":"hull","margin_mm":1}},
       "surface":{"kind":"model","parameters":{"pick":"top","resolution_mm":0.5}},
       "stock":{"kind":"model","parameters":{"margin_top_mm":2}},
       "planner":{"parameters":{"stepover_mm":2,"sample_step_mm":0.5,"depth_per_pass_mm":1}}}'
```

### 脚本调用

```bash
python examples/headless_plan.py
```

该示例不使用界面，直接生成一条刀路、打印统计信息并导出 NC 文件，
可以当作把 ToolpathLab 当作库使用的起点：

```python
from toolpath_lab.core.region import build_region
from toolpath_lab.core.surface import WaveSurface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan

outcome = run_plan(
    planner_id="raster",
    tool=Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0),
    region=build_region("square", {"side_mm": 80.0}),
    parameters={"mode": "zigzag", "stepover_mm": 6.0, "feed_mm_per_min": 800.0},
    surface=WaveSurface(amplitude_mm=5.0, wavelength_mm=40.0),   # 省略即平面
)
print(outcome.toolpath.statistics())
```

### HTTP 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查与版本号 |
| `GET /api/catalog` | 能力目录：区域形状、加工面、刀路策略、参数声明、默认值、已导入的模型与固定值 |
| `POST /api/plan` | 生成刀路，返回刀路运动段、统计与播放时间轴 |
| `POST /api/export/gcode` | 导出 NC 程序 |
| `GET /api/models` | 已导入的模型列表 |
| `POST /api/models` | 上传模型（`{"name": "...", "data_base64": "..."}`） |
| `GET /api/models/{id}` | 模型详情，含三维显示用的三角形坐标 |
| `DELETE /api/models/{id}` | 删除模型 |

```bash
curl http://127.0.0.1:8770/api/catalog

curl -X POST http://127.0.0.1:8770/api/plan \
  -H "Content-Type: application/json" \
  -d '{"tool":{"diameter_mm":6,"length_mm":30},
       "region":{"shape":"circle","parameters":{"diameter_mm":80}},
       "surface":{"kind":"slope","parameters":{"tilt_deg":15}},
       "planner":{"id":"raster","parameters":{"mode":"one_way","stepover_mm":6}}}'

curl -X POST http://127.0.0.1:8770/api/export/gcode \
  -H "Content-Type: application/json" -d '{}' -o toolpath.nc
```

参数非法返回 `400`；参数合法但几何上无法加工（例如刀具直径大于区域尺寸）返回 `422`，
响应体中的 `error` 字段给出具体原因。

## 项目结构

```
toolpath_lab/
  core/        领域层：参数声明、刀具、区域、加工面、毛坯、导入模型（STL / Z-map）、刀路与运动段模型
  planning/    策略层：Planner 基类与注册表、平面多边形几何、栅格刀路
  simulation/  时间层：按进给速度把刀路参数化为时间轴
  export/      G-code 导出
  server/      标准库 HTTP 服务：接口路由、请求校验、能力目录、模型库、静态文件
  web/         前端：原生 ES 模块 + three.js（随仓库提供，无打包步骤）
electron/      桌面壳：拉起 Python 后端并承载窗口
examples/      命令行示例与示例插件
tests/         单元测试
docs/          架构与扩展文档
```

依赖方向是单向的：`core` 不依赖其它层，`planning` / `simulation` / `export` 只依赖 `core`，
`server` 负责组装，`web` 只通过 HTTP 与后端通信，`electron` 只负责窗口。
因此刀路算法可以脱离界面单独运行。详见 [docs/architecture.md](docs/architecture.md)。

## 配置常量

以下数值定义在代码中，不在界面上暴露：

| 常量 | 值 | 位置 |
| --- | --- | --- |
| 安全高度 | 加工面最高点与毛坯顶面取高者之上 5 mm（都没有时即 Z = 5 mm） | `toolpath_lab/planning/base.py` |
| 快移速度 | 5000 mm/min | `toolpath_lab/planning/base.py` |
| 边界处理 | 刀路相对区域轮廓内缩一个刀具足迹半径 | `toolpath_lab/planning/raster.py` |
| 每刀采样 | 平面两个端点；曲面按"离散步长"取点 | `toolpath_lab/planning/raster.py` |
| 分层数上限 | 200 层（再多就报参数错误，避免生成海量运动段） | `toolpath_lab/planning/raster.py` |
| Z-map 节点上限 | 1 000 000（超出自动放宽分辨率） | `toolpath_lab/core/mesh.py` |
| 模型数量上限 | 8（先进先出） | `toolpath_lab/core/mesh.py` |
| 三维显示三角形上限 | 40 000（超出等间隔抽样显示） | `toolpath_lab/core/mesh.py` |

把它们改成可在界面上调整的参数，做法见 [docs/extending.md](docs/extending.md)。

## 扩展

- **新增刀路策略**：继承 `Planner`，声明参数并实现 `plan()`，然后注册。
  [examples/plugins/contour_planner.py](examples/plugins/contour_planner.py) 是一个可直接使用的
  环切（等距轮廓）实现，复制到 `toolpath_lab/planning/` 并在 `__init__.py` 中导入一行即可启用。
- **新增区域形状**：实现一个返回逆时针边界多边形的 `boundary()`，栅格刀路与三维显示会自动适配。
- **新增加工面**：实现一个返回高度的 `heights(points_xy)`，栅格刀路就会沿它抬降。
- **新增毛坯**：实现一个返回六面体范围的 `box()`（例如圆柱毛坯 / 铸件毛坯），分层粗加工直接可用。
- **新增导出格式**：在 `export/` 中写一个纯函数，并在 HTTP 路由中加一个分支。
- 完整说明见 [docs/extending.md](docs/extending.md)，开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

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

## 测试

```bash
python -m unittest discover -s tests
```

覆盖几何裁剪、刀路模式与安全高度、曲面离散与安全平面、STL 解析与 Z-map、时间参数化、
G-code 导出、HTTP 接口（含模型上传）与静态资源。

## 设计说明

刀路模型采用机械加工中常见的平行扫描线形式，时间轴按各段运动的进给速度累加，
三维交互沿用通用的三维 CAD 操作习惯（左键旋转、中键缩放、右键平移）。

## 许可

[MIT](LICENSE)
