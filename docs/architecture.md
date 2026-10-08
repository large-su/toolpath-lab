# 架构

## 分层与依赖方向

```
   electron/main.mjs           独立窗口（只负责拉起后端并装窗口）
        │  HTTP / JSON
      server ─────────┐
        │             │
   ┌────┴────┐     export
   │         │        │
planning ── simulation ┘
   │            │
   └──► core ◄──┘
        ▲
        │ 静态文件
      web（原生 ES 模块 + three.js）
```

依赖只有一个方向。`core` 不认识任何其它层，`planning` / `simulation` / `export` 只依赖 `core`，
`server` 组装全部，`web` 只通过 HTTP 说话，`electron` 只负责窗口。由此得到三个好处：

- 刀路算法可以脱离界面单独跑（`examples/headless_plan.py`）；
- 换传输层（CLI、gRPC、ROS 节点）不需要动算法；
- 把 `core` + `planning` 搬进别的项目不会拖入框架依赖。

## 各层职责

### core —— 领域层

| 模块 | 职责 |
| --- | --- |
| `parameters.py` | `ParameterSpec` / `ParameterSet`：声明式参数（类型、范围、默认值、单位、中文标签、显隐条件、选项的 disabled），同时驱动界面、校验与文档 |
| `tool.py` | 刀具：类型、直径、长度，以及由类型推出的**足迹半径**（刀路相对轮廓的偏置量） |
| `surface.py` | 加工面：一个高度场 `z = height(x, y)`（平面 / 斜面 / 圆柱面 / 球冠面），并负责"是否盖满整个区域"的判断 |
| `region.py` | 区域形状：方形与圆形，统一输出逆时针边界多边形 |
| `path.py` | `Move`（切削/连接/快移 + 进给）与 `Toolpath`（统计、载荷） |
| `registry.py` | 通用能力注册表（区域形状、策略共用） |
| `payload.py` | 请求字典 → 领域对象的拆解工具 |

### planning —— 策略层

- `base.py`：`PlanningContext`（刀具 + 区域 + 加工面 + 参数）与 `Planner` 基类；固定的安全高度与快移速度也在这里。
  `to_positions()` 把平面点逐点抬到加工面上，所以安全高度与刀路都是**相对曲面**的；
- `geometry2d.py`：`scanline_intervals`（直线与多边形求交、偶奇配对）与多边形规范化——
  栅格刀路只靠这一个几何操作就能支持任意形状；
- `raster.py`：往复与单向两种模式；扫描线的采点抽成 `sample_pass()`——平面只取两个端点，曲面按采样步长取点；
- `surface_finish.py`：曲面精加工。把切宽换成**残留高度**，用 `scallop_stepover_mm(ρ, h)` 反算切宽，
  再复用同一套扫描线贴着曲面走刀。

### simulation —— 时间层

`build_timeline` 把每段运动按自己的进给速度换算成时间（t = 弧长 / 进给），在弧长上重采样并限制
总采样数（默认 4000），同时保留每段边界，所以播放不会跨段插值。载荷里 `times` / `positions`
是逐采样数组，`kind_runs` / `move_runs` 是游程编码。

### export / server / web / electron

- `export/gcode.py`：G21 / G90 / G17 + G0 / G1 带 F 的最常见 ISO 子集；
- `server`：标准库 `ThreadingHTTPServer`。`schema.py` 是唯一的请求校验入口，`service.py` 组装响应，
  `catalog.py` 生成能力目录，`app.py` 只做路由与错误码映射（400 参数错误 / 422 几何不可行 / 404 / 405）；
  静态文件只从 `web/` 提供并做了路径穿越防护；
- `web`：`panel.js` 依据目录生成控件，`viewport.js` 负责 three.js 场景与相机，`playback.js` 是纯逻辑的
  时间插值器，`main.js` 负责串联。画布**绝对定位**在 `.viewport` 里，`.stage` 用 `min-height: 0` +
  `overflow: hidden` 与内容解耦：画布尺寸由容器算出，若画布又参与撑高容器就会自激放大
  （`devicePixelRatio > 1` 的屏幕上每轮放大 dpr 倍，很快就会白屏）；
- `electron/main.mjs`：挑一个空闲端口 → 拉起 `python -m toolpath_lab` → 轮询 `/api/health` →
  装进原生窗口；关窗时结束后端。前端是普通静态文件，所以不需要打包器。

## 关键算法

### 栅格刀路

1. 把区域轮廓旋转到"走刀坐标系"：u 沿走刀方向，v 垂直于它；
2. 在 v 方向按切宽布刀，两端各内缩一个刀具足迹半径（保证刀不会切出区域）；
3. 每条刀线用 `scanline_intervals` 求交，得到它在区域内部的区间（方形是整条，圆形是一条弦，
   凹形状可以是多段）；
4. 区间两端就是这一刀的起点和终点——加工面是平面时一刀只有两个点，曲面时沿刀线按采样步长加密；
5. 往复模式奇数刀反向、刀间直接连过去；单向模式每刀同向、刀间抬到安全面再回来；
   首尾补"下刀"和"抬刀"。

### 曲面贴合与残留高度

加工面是一个高度场：点 `(x, y)` 的 Z 由 `height(x, y)` 给出，刀路用 `to_positions()` 逐点抬到面上，
安全高度也相对曲面抬起（平面时 `height ≡ 0`，行为与 0.0.1 完全一致）。平面加工面只做一次
定义域检查，曲面则要求高度场的定义域盖住整个区域，盖不住就抛 `PlanningError`（HTTP 422）。

球头刀 / 圆鼻刀贴着曲面走刀时，相邻两条刀线之间留下的残留高度 `h` 与切宽 `ae` 满足
`ae = 2·sqrt(2ρh − h²)`（ρ 是刀尖圆弧半径）。曲面精加工反过来用它：界面给的是残留高度，
切宽由它算出——残留高度越小，刀线越密、表面越光。平底刀（ρ = 0）反算不出切宽，
于是退回"最大切宽"并给出一条提醒。


### 时间参数化

每段运动携带自己的进给速度，累计时间就是各段"弧长 / 进给"之和，因此快移段与切削段对"预计工时"
的贡献不同。播放时在时间轴上二分查找 + 线性插值；采样下标同时用于"已走轨迹"的绘制范围
（`setDrawRange`），不需要额外几何。

## 想动手改的时候

- 想加**参数**：在对应能力的 `ParameterSet` 里加一行 `spec(...)`，界面与校验自动跟上；
- 想加**形状**：写一个 `boundary()` 返回逆时针多边形；
- 想加**策略**：继承 `Planner` 并注册，见 extending.md；
- 想加**加工面**：继承 `Surface` 实现 `height()` 与定义域判断，见 extending.md；
- 想加**五轴 / 刀轴**：给 `Move` 加刀轴字段，再在策略里逐点给出姿态；
- 想加**导出格式**：在 `export/` 写一个纯函数，在 `server/app.py` 加一个分支。
