# ToolpathLab · 刀路规划基座

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

一个**刻意做小**的刀路规划基座：给定一把刀具、一块规则区域，生成最基础的刀路，
并把它三维显示出来、按进给速度播放出来。目标是让人半天读完代码，然后动手加自己的东西。

```bash
git clone https://github.com/large-su/toolpath-lab.git
cd toolpath-lab
```

![界面](docs/images/screenshot.png)

## 它有什么（以及刻意没有什么）

| 有 | 说明 |
| --- | --- |
| 刀具 | 平底刀（直径、长度）。球头刀 / 圆鼻刀在列表里显示为"待拓展"，不可选 |
| 区域 | 方形（边长）与圆形（直径），加工面固定为 XY 平面 |
| 刀路 | **往复 Zigzag** 与 **单向 One-way** 两种栅格刀路 |
| 参数 | 切宽、走刀方向、进给速度（其余量写死为常量，见下） |
| 三维界面 | 独立桌面窗口（Electron）；左键旋转、中键缩放、右键平移；标准视图；实时阴影 / 白色背景 / 网格地面；按进给速度播放 |
| 导出 | 一个按钮：导出 NC（G-code） |

**刻意没有**：曲面与三维区域、导入模型/提取特征、凹多边形与各种预设形状、环切等其它策略、
仿真切削、后处理对话框、多格式导出。这些不是遗漏，而是留给基于它做练习的人——
见 [examples/plugins](examples/plugins) 里那份可以直接复制进主程序的环切示例。

**固定值**（想改成参数就去 `toolpath_lab/planning/base.py`）：

- 安全高度 5 mm，快移速度 5000 mm/min；
- 刀路相对区域轮廓**内缩一个刀具半径**（保证刀不切出区域）；
- 一刀只有两个点（加工面是平面），进刀/退刀用最简单的"下刀 → 走 → 抬刀"。

## 快速开始

**Windows：双击 `start.bat`**。脚本会自己找 Python（必要时建 `.venv` 装 numpy），
找 Node.js 并在首次运行时安装 Electron，然后打开独立窗口。没有 Node.js 时会退回浏览器打开。

**开发者方式**：

```bash
pip install -r requirements.txt        # 后端只依赖 numpy
npm install                            # 只装 Electron

npm start                              # 独立窗口（会自动拉起 Python 后端）
python -m toolpath_lab                 # 只用后端 + 浏览器：http://127.0.0.1:8770/
python examples/headless_plan.py       # 完全不用界面：算一条刀路并导出 NC
```

## 界面

左侧是参数（刀具 / 区域 / 刀路 / 显示 / 固定设置），右侧是三维视图、统计与播放条。

- **鼠标**：左键拖动旋转，中键滚轮缩放，右键拖动平移（与 ROMP 一致，右键菜单已屏蔽）。
- **视图工具条**（顶部居中）：最佳 / 前 / 后 / 左 / 右 / 上 / 下；再点一次当前方向会切到对面。
- **外观**（左上）：实时阴影、白色背景、网格地面。
- **播放条**（底部）：播放/暂停（空格也行）、回到起点、拖动进度。

![俯视图](docs/images/screenshot-top.png)

参数面板由后端 `/api/catalog` 的参数声明自动生成：**加一个区域形状或刀路策略只需要写 Python，
界面里会自动出现它的控件**，不用改任何 JavaScript。

## 目录结构

```
toolpath_lab/core/       领域层：参数声明、刀具、区域、刀路/运动段模型（不依赖任何其它层）
toolpath_lab/planning/   策略层：Planner 基类 + 注册表、扫描线几何、栅格刀路（单向/往复）
toolpath_lab/simulation/ 时间层：按进给速度把刀路变成时间轴（播放与工时）
toolpath_lab/export/     G-code 导出
toolpath_lab/server/     标准库 HTTP 服务：/api/catalog、/api/plan、/api/export/gcode
toolpath_lab/web/        原生 ES 模块前端 + 自带 three.js
electron/main.mjs        桌面壳：拉起 Python 后端并装进原生窗口
examples/                不用界面的示例、以及可复制进主程序的插件示例
tests/                   110 项 unittest
docs/                    架构与扩展文档
```

依赖只有一个方向：`core` 不认识任何其它层，`web` 只通过 HTTP 与后端交流。
详见 [docs/architecture.md](docs/architecture.md)。

## 接口

```bash
curl http://127.0.0.1:8770/api/catalog        # 能力目录：形状/策略 + 参数声明 + 默认值 + 固定值

curl -X POST http://127.0.0.1:8770/api/plan \
  -H "Content-Type: application/json" \
  -d '{"tool":{"diameter_mm":6,"length_mm":30},
       "region":{"shape":"circle","parameters":{"diameter_mm":80}},
       "planner":{"id":"raster","parameters":{"mode":"one_way","stepover_mm":6}}}'

curl -X POST http://127.0.0.1:8770/api/export/gcode -H "Content-Type: application/json" -d '{}' -o toolpath.nc
```

参数非法返回 `400`，几何上做不到（例如刀具比区域还大）返回 `422`。

## 怎么拓展

- **加一个刀路策略**：[examples/plugins/contour_planner.py](examples/plugins/contour_planner.py)
  是一份完整的环切实现，复制进 `toolpath_lab/planning/` 并在 `__init__.py` 里导入一行即可；
- **加一个区域形状**：写一个 `boundary()` 返回逆时针多边形的类，注册一下；
- **把固定值变成参数**：在 `planning/base.py` 加常量、在策略的 ParameterSet 里加一行；
- **接入机器人加工**：给 `Move` 增加刀轴字段、给区域加高度场，接口都已经留好位置。

完整说明见 [docs/extending.md](docs/extending.md)，动手前请读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 测试

```bash
python -m unittest discover -s tests     # 标准库即可，无需 pytest
```

## 与 ROMP 的关系

刀路概念（扫描线裁剪、单向/往复、按进给的时间轴）来自机器人加工规划项目 ROMP 的实践，
交互与观感（鼠标映射、标准视图、外观开关、深色工作室外观）也对齐 ROMP；
但本项目不共享代码，只保留最少的概念，方便作为一个独立的教学与练手基座。

## 许可

MIT，见 [LICENSE](LICENSE)。
