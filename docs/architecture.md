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
| `region.py` | 区域形状：方形与圆形，统一输出逆时针边界多边形 |
| `path.py` | `Move`（切削/连接/快移 + 进给 + 可选刀轴姿态）与 `Toolpath`（统计、载荷） |
| `registry.py` | 通用能力注册表（区域形状、策略共用） |
| `payload.py` | 请求字典 → 领域对象的拆解工具 |

### planning —— 策略层

- `base.py`：`PlanningContext`（刀具 + 区域 + 曲面 + 参数）与 `Planner` 基类；固定的安全高度与快移速度也在这里；
- `geometry2d.py`：`scanline_intervals`（直线与多边形求交、偶奇配对）与多边形规范化——
  栅格刀路只靠这一个几何操作就能支持任意形状；
- `raster.py`：往复与单向两种模式。
- `five_axis.py`：在加密后的每个扫描点上计算曲面法向、前倾/侧倾刀轴，并输出连续姿态与局部刀具接触间隙。
  可选的距离加权姿态平滑限制原刀轴偏差，换行在安全高度转向；运动段携带仿真角速度上限。
- `five_axis_adaptive.py`：球头刀教学组合，用自适应策略的名义采样刀点与步距，再复用 `orient_surface_passes` 赋予五轴姿态；原有两种策略保持独立。
- `roughing.py`：可选地在原策略前插入竖直刀轴的分层粗加工，通过曲面邻域保护包络保留精加工余量，并提供各层与精加工的阶段索引。

### simulation —— 时间层

`build_timeline` 把每段运动按自己的进给速度换算成时间（t = 弧长 / 进给），在弧长上重采样。
默认采样目标 4000 为软预算；每段边界、快移拐点及粗加工保护包络刀点必须保留，必要时可超出预算，避免跨段或压缩后穿过毛坯。启用五轴平滑的段按平移/转向所需时间的较大值计时，保留刀轴刀点及原地转向，播放与检测用相同大圆插值。载荷里 `times` / `positions`
是逐采样数组，`kind_runs` / `move_runs` 是游程编码。

`stock.py` 提供 `StockSpec` 与 `StockState`。它用规则 XY 高度场表达毛坯顶部，按刀具圆形足迹
降低局部高度，并支持从任意时间轴采样点重置后重放。前端只有在勾选“材料切除仿真”时才创建
对应状态和网格；未勾选时仍沿用原来的轻量刀路播放。

### export / server / web / electron

- `export/gcode.py`：G21 / G90 / G17 + G0 / G1 带 F 的最常见 ISO 子集；
- `server`：标准库 `ThreadingHTTPServer`。`schema.py` 是唯一的请求校验入口，`service.py` 组装响应，
  `catalog.py` 生成能力目录，`app.py` 只做路由与错误码映射（400 参数错误 / 422 几何不可行 / 404 / 405）；
  静态文件只从 `web/` 提供并做了路径穿越防护；
- `web`：`panel.js` 依据目录生成控件，`viewport.js` 负责 three.js 场景与相机，`playback.js` 是纯逻辑的
  时间插值器，`main.js` 负责串联；
- `electron/main.mjs`：挑一个空闲端口 → 拉起 `python -m toolpath_lab` → 轮询 `/api/health` →
  装进原生窗口；关窗时结束后端。前端是普通静态文件，所以不需要打包器。

## 关键算法

### 栅格刀路

1. 把区域轮廓旋转到"走刀坐标系"：u 沿走刀方向，v 垂直于它；
2. 在 v 方向按切宽布刀，两端各内缩一个刀具足迹半径（保证刀不会切出区域）；
3. 每条刀线用 `scanline_intervals` 求交，得到它在区域内部的区间（方形是整条，圆形是一条弦，
   凹形状可以是多段）；
4. 区间两端就是这一刀的起点和终点——一刀两个点，因为加工面是平面；
5. 往复模式奇数刀反向、刀间直接连过去；单向模式每刀同向、刀间抬到安全面再回来；
   首尾补"下刀"和"抬刀"。

### 五轴姿态

五轴策略先从曲面求单位法向，再由走刀方向构造切向量和侧向量：
`axis = normalize(normal + tan(lead) × tangent + tan(side_tilt) × side)`。
每个 `Move` 保存与 XYZ 点数量相同的 `tool_axes`，时间轴对姿态做归一化插值，前端将刀具局部 +Z 旋转到该方向。
G-code 导出以 A（方位角）和 B（相对 +Z 的倾角）表达，真实机床的 RTCP 和旋转轴约定由后处理器负责。

### 时间参数化

每段运动携带自己的进给速度，累计时间就是各段"弧长 / 进给"之和，因此快移段与切削段对"预计工时"
的贡献不同。播放时在时间轴上二分查找 + 线性插值；采样下标同时用于"已走轨迹"的绘制范围
（`setDrawRange`），不需要额外几何。

## 想动手改的时候

- 想加**参数**：在对应能力的 `ParameterSet` 里加一行 `spec(...)`，界面与校验自动跟上；
- 想加**形状**：写一个 `boundary()` 返回逆时针多边形；
- 想加**策略**：继承 `Planner` 并注册，见 extending.md；
- 想加**曲面 / 三维区域**：给区域加高度场和 `normal_at()`，策略即可逐点采样；
- 想加**更精确的材料切除仿真**：替换 `simulation/stock.py` 的高度场为体素或三角网格扫掠体，保持 `StockSpec` 元数据和 Timeline 驱动接口不变；
- 想加**导出格式**：在 `export/` 写一个纯函数，在 `server/app.py` 加一个分支。
