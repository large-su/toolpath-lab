# 扩展指南

所有扩展点都是同一个套路：**声明参数 → 实现核心方法 → 注册**。注册之后，界面、接口、能力目录
都会自动包含它。

## 1. 新增一个刀路策略（推荐从这里开始）

主程序里已经有三个策略可以照着写：`raster.py`（栅格，只用扫描线求交）、
`spiral.py`（螺旋，连续一圈刀）与 `contour.py`（环切，一圈一刀）。
如果想要一个**未注册的模板**，仓库里还有 examples/plugins/contour_planner.py
（`SketchPlanner`）——它刻意没有 `@PLANNERS.register`，复制进主程序也不会撞 id：

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/my_planner.py
```

```python
# toolpath_lab/planning/__init__.py
from toolpath_lab.planning import my_planner as _my_planner  # noqa: F401
```

等距偏置几何（`offset_polygon` / `resample_ring` / `collect_rings`）现在正式放在
`toolpath_lab/planning/geometry2d.py` 里，`spiral.py` 与 `contour.py` 共用，
写新策略时不用再抄一份。

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
| tool / region | 刀具与区域对象 |
| parameters | 已经过校验的参数（含默认值） |
| feed_mm_per_min | 当前进给 |
| to_positions(points_xy) | 平面点 (N, 2) → 工件坐标 (N, 3)，Z = 0 |
| cut_move / link_move | 切削段 / 连接段 |
| rapid_between | 抬刀 → 横移 → 下刀 |
| approach_move_down / retract_move_up | 首尾的下刀与抬刀 |
| warn(message) | 记录一条提醒（会显示在界面上） |

**别踩的坑**：

- Toolpath 至少要有一段运动、Move 至少两个点，否则会抛 ParameterError；
- 每段运动必须标明类型（切削 / 连接 / 快移）与进给，否则统计、播放与导出都会失真；
- 参数键在同一个 ParameterSet 里必须唯一；
- `visible_if` 是**写在单个 spec 上的**，不是写在 ParameterSet 上（`ParameterSet` 不接受这个
  关键字）。它表达"当某个参数等于某值时才显示"，例如 `spec(..., visible_if={"ramp_feed": "true"})`；
- 想让界面上的进给随半径变化，就不能只用 `context.feed_mm_per_min`——那是单一值。
  按圈插值进给需要自己构造 `Move(MoveKind.CUT, points, feed, ...)`，见 `spiral.py`；
- 几何算不出来时要区分"材料用尽"和"算法失败"：前者正常收尾，后者要 `context.warn()`。
  `spiral.py` 用轮廓内切半径（`_inradius`）来区分：偏置量超过内切半径是材料用尽，
  还没超过就返回 None（例如圆角处自交）说明中心没切干净，必须提醒用户。

## 2. 新增一个区域形状

在 toolpath_lab/core/region.py 里加一个类，然后登记进 `core/__init__.py` 的导入与 `__all__`。
仓库里已经有一个走完整个流程的例子：RoundedRectRegion（`region.py`）与它对应的
`tests/test_region.py::RoundedRectRegionTests`，可以对照着看。

本工程里已经有的四个形状：

- 方形 `square`（一个边长）；
- 圆形 `circle`（一个直径）；
- 椭圆 `ellipse`（长半轴 / 短半轴）；
- 圆角矩形 `rounded_rect`（宽 / 高 / 圆角半径，圆角半径取短边一半即跑道形）。

形状本身可以写在 region.py 里，也可以单独放一个模块再导入：

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
工作，因为裁剪用的是扫描线求交。`PlanningContext.boundary` 会再调用一次 `ensure_ccw`，
所以相邻重复点（例如圆角半径取到极限时首尾相接）会被顺手清掉，不必自己去重。

三条容易踩的坑：

- **参数范围要自己兜住几何约束**。`spec` 只能表达"上下限"，像"圆角半径不超过短边一半"这种
  相互依赖的条件要在 `__post_init__` 里检查并抛 `ParameterError`（接口会返回 400）；
- **写完形状记得登记 `core/__init__.py`**，否则 `from toolpath_lab.core import RoundedRectRegion`
  会失败——类的定义位置和它是否被导出是两件事；
- **不要按"极角"反解参数来采样椭圆**。由 `cos φ / a` 反解 `t` 时，φ = 90° 与 270° 会得到同一个
  `t`，上下半平面的点重合，面积能偏一个数量级。用标准参数式 `x = a·cos t, y = b·sin t` 即可。

## 3. 把固定值变成参数

本工程里已经有一个走完流程的例子：**工艺设置**（`planning/setup.py`）。
它把安全高度、快移速度、边界处理方式从常量改成了一份独立的 `ParameterSet`，
通过 `PlanningContext.setup` 传给所有策略。要加新的机床/工艺参数，照这个模式做：

1. 在 `setup_parameters()` 里加一条 `spec(...)`，写清范围、单位与中文标签；
2. 在 `PlanningContext` 上加一个属性读它（留空退回默认值，旧代码与旧测试不受影响）；
3. 策略里改用 `context.<属性>` 而不是模块常量。

`catalog.py` 会自动导出这份声明，界面自动生成控件——不用碰前端。

```python
# planning/setup.py
spec("safe_height_mm", "安全高度", K.FLOAT, 5.0, minimum=0.1, maximum=100.0,
     step=0.5, unit="mm", group="设置"),

# planning/base.py —— 留空退回模块常量，所以直接构造 context 的旧测试不用改
@property
def safe_height_mm(self) -> float:
    return float(self.setup.get("safe_height_mm", SAFE_HEIGHT_MM))
```

**边界处理**（刀路相对轮廓的偏置量）同理：策略里把
`context.tool.footprint_radius_mm` 换成 `context.boundary_offset_mm`，
由 `setup.boundary_offset()` 按模式解析成 0 / 半径 / 负半径 / 自定义值。

## 4. 新增导出格式

在 `export/` 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 `export/__init__.py`
里导出并登记进 `EXPORT_FORMATS`，再在 `server/app.py` 的 `_route_api` 与 `_export` 里各加一个分支。
已有的三个格式（`gcode.py` 程序、`csv_points.py` 逐点表、`json_toolpath.py` 快照）都是纯函数，
可以照抄。前端则在 `main.js` 的 `EXPORT_OPTIONS` 里加一项。

## 5. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
