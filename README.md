<p align="center">
  <img src="toolpath_lab/web/icon.png" width="128" alt="ToolpathLab">
</p>

# ToolpathLab · 刀路规划基座

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

ToolpathLab 是一个刀路规划基座：给定一把刀具和一块规则形状的加工区域，生成刀路（栅格或跟随周边），
在三维窗口中显示工件、刀路与刀具，并按进给速度播放整个加工过程。

后端是纯 Python（只依赖 numpy），前端是原生 ES 模块加 three.js，桌面窗口由 Electron 提供。
刀具与区域都用参数描述，参数面板根据后端的参数声明自动生成。

![界面](docs/images/screenshot.png)

## 功能

- **刀具**：平底刀、球头刀与圆鼻刀，可设置直径与长度；圆鼻刀另有刀尖圆角 `Rc` 参数（界面按刀具类型自动显隐）。
  三者的**足迹半径**是同一句话——**半径 − 刀尖圆角半径**：平底刀 R、球头刀 0、圆鼻刀 R − Rc；
  `Rc = 0` 就退化成平底刀，`Rc = R` 就退化成球头刀。足迹半径决定刀路相对区域轮廓的偏置量。

![三种刀尖：平底刀 R、球头刀点接触、圆鼻刀 R − Rc](docs/images/tool-tips.png)
- **区域**：以原点为中心的三种形状
  - **方形**（边长）与**圆形**（直径）：加工面是水平面，**高度可设**（相对基准面 Z = 0，默认 0，
    可正可负）。刀路 Z、安全面与 G-code 都跟着它走；三维里工件实体跟着高度摆放、地面网格留在
    基准面，所以"抬高"一眼就能看出来；
  - **斜坡**：XY 投影仍是方形（默认 80 × 80），以 **+X 方向最外侧边为低边**、沿 −X 方向抬起，
    斜度可调（最大 80°），**Z 上限也可设**（斜面沿 Z 轴能升到的最大高度，默认 80 mm、范围 1–1000）：
    升到上限就转成平顶（上限越小、斜度越大，平顶越宽；上限给得比整块坡度还高就没有平顶）。
    **默认只加工斜面段**——刀路正好停在折痕上、不走上平顶（平顶留给别的工序）；想连平顶一起加工，
    把区域参数"加工平顶"打开。（斜坡暂时不提供加工面高度。）
  - 三个区域都能设**部件厚度**（加工面以下那块基体有多厚，默认 20 mm）：它是纯几何量，
    只影响工件实体与显示，**不参与刀路计算**——把料调厚调薄，刀路一模一样。
- **刀路**：两种策略
  - **栅格刀路**：平行扫描线，两种模式——**往复 Zigzag**（奇数刀反向，相邻两刀在端头直接连过去）
    与**单向 One-way**（每刀同向，刀与刀之间抬刀到安全面再回到起点，也可以改成沿加工面连接）；
  - **跟随周边**：沿区域轮廓等距环切，一圈比一圈靠里，直到区域切满；**走刀顺序**可选向内
    （由外向内，第一刀沿轮廓）或向外（由内向外，下刀在料心），**绕向**可选逆时针或顺时针
    （俯视方向为准，每一环同向绕行）。
- **斜面上的走刀**（加工面不是水平面时自动生效，也可以在"下刀方式"里关掉）：
  **由低往高**走刀，下刀从料外**沿加工面切入**（不垂直扎进斜面），单向走刀时刀与刀**沿加工面连接**、
  不再每次抬到安全面。
- **参数**：切宽、走刀方向角、走刀模式、下刀方式、刀间连接、走刀顺序、进给速度；区域侧另有
  边长 / 直径、加工面高度、部件厚度、斜度、Z 上限与加工平顶。安全高度、快移速度、沿面切入长度、边界处理方式等为固定值，
  见[配置常量](#配置常量)。
- **三维视图**：工件实体、区域轮廓、刀路（切削 / 连接 / 快移分色）、刀具实体、已走轨迹、实时阴影，
  以及可以随时生成／收起的**毛坯**（见下）。
- **播放**：按每段运动自己的进给速度做时间参数化，支持播放 / 暂停、拖动进度，并给出切削长度与预计工时。
- **导出**：NC 程序（G-code，G21 / G90 / G17 加 G0 / G1 带 F）。
- **HTTP 接口**：能力目录、规划、导出三个接口，便于脚本调用与集成。

## 界面

左侧是参数面板，右侧是三维视图、统计与播放条。

| 鼠标操作 | 功能 |
| --- | --- |
| 左键拖动 | 旋转视角 |
| 中键滚轮 | 缩放 |
| 右键拖动 | 平移 |

- **顶栏按钮**：「生成刀路」「生成毛坯」「导出 NC」，毛坯按钮旁边是**顶部余量**输入框。
  **生成毛坯**会按当前区域生成毛坯：**方形与斜坡是长方体、圆形是竖直圆柱**，**竖直面贴紧区域**
  （XY 不留余量）、底面与工件底面齐平，只有**顶面留「顶部余量」**（默认 2 mm，可改 0–50）——
  改余量会立刻重画毛坯，不必重新规划。毛坯用半透明紫色（棱线更亮）与工件、刀路明显区分；
  区域参数改了就跟着重新生成，再点一次收起（按钮文字在"生成毛坯 / 隐藏毛坯"之间切换）。
- **视图工具条**（顶部居中）：最佳 / 前 / 后 / 左 / 右 / 上 / 下；再次点击当前方向会切换到对面。
- **外观开关**（左上角）：实时阴影、白色背景、网格地面。
- **播放条**（底部）：播放 / 暂停（空格键同样有效）、回到起点、拖动进度、当前时间。
- **统计面板**（右上角）：区域尺寸、刀轨条数、刀点数量、切削长度、预计工时。

![俯视图](docs/images/screenshot-top.png)

参数面板由后端 `/api/catalog` 返回的参数声明生成：新增区域形状或刀路策略后，
界面上会自动出现对应的控件，不需要修改前端代码。

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

脚本自己会把仓库根目录加进 `sys.path`，所以没装本包、也没设 `PYTHONPATH` 时照样能运行
（在任意目录运行也可以）。该示例不使用界面，直接生成一条刀路、打印统计信息并导出 NC 文件，
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

### HTTP 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查与版本号 |
| `GET /api/catalog` | 能力目录：区域形状、刀路策略、参数声明、默认值与固定值 |
| `POST /api/plan` | 生成刀路，返回刀路运动段、统计与播放时间轴 |
| `POST /api/export/gcode` | 导出 NC 程序 |

```bash
curl http://127.0.0.1:8770/api/catalog

curl -X POST http://127.0.0.1:8770/api/plan \
  -H "Content-Type: application/json" \
  -d '{"tool":{"diameter_mm":6,"length_mm":30},
       "region":{"shape":"circle","parameters":{"diameter_mm":80}},
       "planner":{"id":"raster","parameters":{"mode":"one_way","stepover_mm":6}}}'

curl -X POST http://127.0.0.1:8770/api/export/gcode \
  -H "Content-Type: application/json" -d '{}' -o toolpath.nc
```

参数非法返回 `400`；参数合法但几何上无法加工（例如刀具直径大于区域尺寸）返回 `422`，
响应体中的 `error` 字段给出具体原因。

## 项目结构

```
toolpath_lab/
  core/        领域层：参数声明、刀具、区域、刀路与运动段模型
  planning/    策略层：Planner 基类与注册表、平面多边形几何、栅格与跟随周边刀路
  simulation/  时间层：按进给速度把刀路参数化为时间轴
  export/      G-code 导出
  server/      标准库 HTTP 服务：接口路由、请求校验、能力目录、静态文件
  web/         前端：原生 ES 模块 + three.js（随仓库提供，无打包步骤）
electron/      桌面壳：拉起 Python 后端并承载窗口
examples/      命令行示例与示例插件
tests/         单元测试
tools/         前端几何自检（node tools/check_frontend_geometry.mjs）
docs/          架构与扩展文档
```

依赖方向是单向的：`core` 不依赖其它层，`planning` / `simulation` / `export` 只依赖 `core`，
`server` 负责组装，`web` 只通过 HTTP 与后端通信，`electron` 只负责窗口。
因此刀路算法可以脱离界面单独运行。详见 [docs/architecture.md](docs/architecture.md)。

## 配置常量

以下数值定义在代码中，不在界面上暴露：

| 常量 | 值 | 位置 |
| --- | --- | --- |
| 安全高度 | 5 mm（相对该段快移经过的加工面最高点） | `toolpath_lab/planning/base.py` |
| 快移速度 | 5000 mm/min | `toolpath_lab/planning/base.py` |
| 沿面切入长度 | 5 mm（斜面下刀从料外沿加工面切进来） | `toolpath_lab/planning/base.py` |
| 边界处理 | 刀路相对**加工范围**内缩一个刀具足迹半径（斜坡只加工斜面段时，分界处先外扩一个足迹） | `planning/raster.py`、`planning/follow_periphery.py` |
| 每刀采样 | 平面区域两个端点；斜面上只在平顶折痕处补一个点 | `toolpath_lab/planning/base.py` |
| 环间连接 | 所有环同向绕行，环间沿同一条缝径向过渡一个切宽、不抬刀 | `toolpath_lab/planning/follow_periphery.py` |
| 斜坡 Z 上限默认值 | 80 mm（区域参数，可改 1–1000；斜面升到它就转平顶） | `toolpath_lab/core/region.py` |
| 部件厚度默认值 | 20 mm（区域参数，可改 1–500）；载荷没带这个字段时按跨度的 9% 估算、限幅 4–24 mm | `toolpath_lab/core/region.py`、`toolpath_lab/web/js/viewport.js` |
| 毛坯顶部余量默认值 | 2 mm（顶栏输入框可改 0–50；竖直面贴紧区域、底面与工件齐平） | `toolpath_lab/web/js/viewport.js` |

把它们改成可在界面上调整的参数，做法见 [docs/extending.md](docs/extending.md)。

## 扩展

- **新增刀路策略**：继承 `Planner`，声明参数并实现 `plan()`，然后注册。
  [toolpath_lab/planning/follow_periphery.py](toolpath_lab/planning/follow_periphery.py)（跟随周边）是
  一个完整的内置实现，可以直接照抄结构；
  [examples/plugins/contour_planner.py](examples/plugins/contour_planner.py) 是同样思路的示例插件，
  复制到 `toolpath_lab/planning/` 并在 `__init__.py` 中导入一行即可启用。
- **新增区域形状**：实现一个返回逆时针边界多边形的 `boundary()`，栅格刀路、跟随周边与三维显示会自动适配。
  加工面不是平面时再实现 `height_at()`（每个 (x, y) 处的 Z）与 `surface_breaks()`（折痕处补点），
  策略本身不用改——斜坡就是这么接进来的。
- **新增导出格式**：在 `export/` 中写一个纯函数，并在 HTTP 路由中加一个分支。
- 完整说明见 [docs/extending.md](docs/extending.md)，开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 测试

```bash
python -m unittest discover -s tests        # 后端与库
node tools/check_frontend_geometry.mjs      # 前端几何（刀路抬升保留真实 Z、刀尖位置、工件法向）
```

覆盖几何裁剪与等距偏置、两种策略的刀路（平面与斜面）与安全高度、时间参数化、G-code 导出、HTTP 接口与静态资源；
前端几何自检补上 Python 测试看不到的那一层（渲染前把三维点变成几何的那几步）。

## 设计说明

加工面由区域自己描述：平面形状的 `height_at()` 恒为 0，斜坡返回被截断的斜面高度，刀路因此
在平面与斜面上走同一条代码路径。刀路模型覆盖机械加工中最常见的两类走法——平行扫描线（栅格）与
沿轮廓逐圈偏置（跟随周边）；时间轴按各段运动的进给速度累加，三维交互沿用通用的三维 CAD 操作习惯
（左键旋转、中键缩放、右键平移）。

## 许可

[MIT](LICENSE)
