# 扩展指南

所有扩展点都是同一个套路：**声明参数 → 实现核心方法 → 注册**。注册之后，界面、接口、能力目录
都会自动包含它。

## 1. 新增一个刀路策略（推荐从这里开始）

最完整的例子就在仓库里：examples/plugins/contour_planner.py（环切，自带它需要的等距偏置几何）。
复制进主程序即可：

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/contour.py
```

```python
# toolpath_lab/planning/__init__.py
from toolpath_lab.planning import contour as _contour  # noqa: F401
```

最短的骨架长这样：

```python
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.registry import PLANNERS


@PLANNERS.register
class SpiralPlanner(Planner):
    id = "spiral"                  # 接口里的标识
    label = "螺旋(示例)"            # 界面上的名字
    description = "从外向内螺旋走刀"
    parameters = ParameterSet((
        spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
             step=0.5, unit="mm", group="刀路"),
        spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
             maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
    ))

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        ...  # 用 context 提供的几何与构造器拼出 Toolpath
```

**context 提供的全部东西**：

| 成员 | 说明 |
| --- | --- |
| boundary | 逆时针、无重复点的区域轮廓 (N, 2) |
| tool / region / surface / stock | 刀具、区域、加工面与毛坯对象 |
| parameters | 已经过校验的参数（含默认值） |
| feed_mm_per_min | 当前进给 |
| to_positions(points_xy) | 平面点 (N, 2) → 工件坐标 (N, 3)，Z 取自加工面 |
| sample_line(a, b, step_mm) | 把一条直线离散成 (N, 2)：曲面加工用它逐点跟随高度 |
| surface_extremes_mm | 加工面在区域内的 (最低, 最高) 高度（分层与安全平面都用它） |
| safe_z_mm | 快移平面：加工面最高点与毛坯顶面取高者再抬起 5 mm |
| cut_move / link_move | 切削段（沿加工面）/ 连接段 |
| level_move(xy, z) | 在给定高度的水平层上切削（分层粗加工） |
| rapid_between | 抬刀 → 横移 → 下刀（空行程，全快移） |
| rapid_over / plunge_to | 抬刀横移到正上方 / 以进给速度下刀（分层加工的入刀） |
| approach_move_down / retract_move_up | 首尾的下刀与抬刀 |
| warn(message) | 记录一条提醒（会显示在界面上） |

**别踩的坑**：

- Toolpath 至少要有一段运动、Move 至少两个点，否则会抛 ParameterError；
- 每段运动必须标明类型（切削 / 连接 / 快移）与进给，否则统计、播放与导出都会失真；
- 参数键在同一个 ParameterSet 里必须唯一；
- 想让自己的策略支持曲面，就把“取点”交给 `context.sample_line()`、把“抬 Z”交给
  `context.to_positions()`，不要自己拼 Z = 0。

## 2. 新增一个区域形状

在 toolpath_lab/core/region.py 里加一个类，或者单独放一个模块再导入：

```python
from dataclasses import dataclass
from typing import ClassVar
import numpy as np
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.region import REGION_SHAPES, RegionShape


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class EllipseRegion(RegionShape):
    semi_major_mm: float = 60.0
    semi_minor_mm: float = 40.0

    id: ClassVar[str] = "ellipse"
    label: ClassVar[str] = "椭圆"
    description: ClassVar[str] = "长半轴 / 短半轴定义的椭圆"
    parameters: ClassVar[ParameterSet] = ParameterSet((
        spec("semi_major_mm", "长半轴", K.FLOAT, 60.0, minimum=1.0, maximum=500.0,
             step=1.0, unit="mm", group="区域"),
        spec("semi_minor_mm", "短半轴", K.FLOAT, 40.0, minimum=1.0, maximum=500.0,
             step=1.0, unit="mm", group="区域"),
    ))

    def boundary(self):
        angles = np.linspace(0.0, 2.0 * np.pi, 180, endpoint=False)
        return np.column_stack((self.semi_major_mm * np.cos(angles),
                                self.semi_minor_mm * np.sin(angles)))
```

只要返回**逆时针、不重复首点**的多边形，栅格刀路与三维显示都会自动适配——连凹多边形都能直接
工作，因为裁剪用的是扫描线求交。

## 3. 新增一种加工面

加工面只需要回答一个问题：给定 XY，Z 是多少。在 `toolpath_lab/core/surface.py` 里加一个类并注册：

```python
@SURFACES.register
@dataclass(frozen=True, slots=True)
class DomeSurface(Surface):
    radius_mm: float = 40.0

    id = "dome"
    label = "球冠面"
    description = "半球面的一部分"
    parameters = ParameterSet((
        spec("radius_mm", "球面半径", K.FLOAT, 40.0, minimum=1.0, maximum=500.0,
             step=1.0, unit="mm", group="曲面"),
        spec("z_offset_mm", "Z 偏置", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
             step=0.5, unit="mm", group="曲面"),
    ))

    def heights(self, points_xy):
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        inner = np.clip(self.radius_mm ** 2 - np.sum(planar ** 2, axis=1), 0.0, None)
        return np.sqrt(inner) + self.z_offset_mm

    def sample_extremes(self, polygon):      # 安全平面：可选，不写就走逐点采样
        return (float(self.z_offset_mm), float(self.radius_mm + self.z_offset_mm))

    def note(self):                          # 写进刀路 notes 的一句中文
        return f"加工面：球冠面（R{self.radius_mm:g} mm），三轴联动"
```

要做的事：

1. `heights()` 必须能接受任意 (N, 2) 数组并返回 (N,)；
2. 返回有限值；如果某些位置没有面，就把它们归到最近的有效高度（参考 `ModelSurface`）；
3. 靠导入模型才能构造的加工面，把 `needs_model = True`，并在 `build_surface()` 里用 `_model=model` 传入；
4. 离散步长（`sample_step_mm`）在**策略**里，不在加工面里：一刀取多密是刀路的事。

刀路会自动跟着新加工面抬降，不需要改 `raster.py`，也不需要改 JavaScript。

## 4. 新增一种毛坯

毛坯只需要回答"材料从哪里到哪里"——一个轴对齐长方体。在 `toolpath_lab/core/stock.py` 里加一个类并注册：

```python
@STOCKS.register
@dataclass(frozen=True, slots=True)
class CylinderStock(Stock):
    margin_mm: float = 0.0

    id = "cylinder"
    label = "圆形棒料"
    description = "把毛坯做成圆柱（只用到包容盒，三维显示仍是长方体）"
    parameters = ParameterSet((
        spec("margin_mm", "余量", K.FLOAT, 0.0, minimum=0.0, maximum=100.0,
             step=0.5, unit="mm", group="毛坯"),
    ))

    def box(self):
        radius = 50.0 + self.margin_mm        # 你的规则
        return (-radius, radius, -radius, radius, 0.0, 100.0)

    def note(self):
        return "毛坯：圆形棒料 Ø100 × 100 mm"
```

顶面高度（`box()` 的第六个值）就是分层粗加工的起点，所以只要 `box()` 给对，
"毛坯 + 切深"这套分层逻辑就不用改。真正的三维形状可以再单独补渲染（现在是长方体）。

## 5. 把固定值变成参数

planning/base.py 里现在是常量：

```python
SAFE_HEIGHT_MM = 5.0
RAPID_FEED_MM_PER_MIN = 5000.0
```

想在界面上可调，就在策略的 ParameterSet 里加一条 spec("safe_height_mm", ...)，把
`context.rapid_between(...)` 换成读参数即可。**边界处理方式**（现在固定为"内缩一个刀具半径"）
同理：把 `context.tool.footprint_radius_mm` 换成按参数取 0 / 半径 / 负半径。

## 6. 新增一种刀具

刀具的三维外观不写在前端：`Tool.segments()` 返回若干段**回转轮廓**（每段是一串
`(半径, 高度)` 点，高度从刀尖 0 算起），前端用 `LatheGeometry` 把它们旋成实体，
`describe()` 会把它一并发布到 `/api/plan` 的 `tool.segments` 里。

球头刀就是这么加上去的，改的只有 `core/tool.py`，JavaScript 一行都没动：

1. **放开选项**：把 `TOOL_KINDS` 里对应选项的 `disabled=True` 去掉。参数面板是照这份声明
   渲染的，刷新页面下拉框里就已经可选了；
2. **补几何**：在 `Tool` 里给出这个类型自己的三个量——
   `corner_radius_mm`（刀尖圆角，决定 `tip_height_mm`）、
   `footprint_radius_mm`（刀路相对区域轮廓的偏置量，球头刀是 0）、
   `_cutting_profile()`（刀尖那一段的回转轮廓）。必要时再补一条 `__post_init__` 校验。

比如要放开圆鼻刀，只需让刀尖轮廓从"平底"变成"圆角 + 平底"：

```python
@property
def corner_radius_mm(self) -> float:
    if self.kind is ToolKind.BALL:
        return self.radius_mm
    if self.kind is ToolKind.BULL:
        return 1.0   # 圆鼻刀的刀尖圆角 Rc，将来做成参数即可
    return 0.0
```

`_cutting_profile()` 里平底刀画的是两条直线 `(0, 0) → (R, 0) → (R, top)`，
圆鼻刀把中间换成一段四分之一圆弧就行——三维显示会自动跟着变。

改完记得补测试：`tests/test_tool.py` 里平底刀与球头刀各有一组几何测试，照着加一组即可。

## 7. 新增导出格式

在 export/ 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 export/__init__.py 里导出，
再在 server/app.py 的 `_route_api` 里加一个分支。

## 8. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
  **加工面的 Z 一律通过 `context.to_positions()` 上抬**，策略里不要写死 Z = 0；
- 分层加工：水平层用 `level_move()`，层间用 `rapid_over()` + `plunge_to()`（进给下刀），
  不要在毛坯里用 `rapid_between()` 扎刀；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
