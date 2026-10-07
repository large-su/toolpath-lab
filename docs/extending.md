# 扩展指南

所有扩展点都是同一个套路：**声明参数 → 实现核心方法 → 注册**。注册之后，界面、接口、能力目录
都会自动包含它。

## 1. 新增一个刀路策略（推荐从这里开始）

仓库里有两个可以直接读的完整例子：内置的 [contour.py](../toolpath_lab/planning/contour.py)（环切，
自带它需要的等距偏置几何）与同源的模板 [examples/plugins/contour_planner.py](../examples/plugins/contour_planner.py)。
新增一个策略只要两步——放一个模块，再在 `planning/__init__.py` 里导入一行：

```python
# toolpath_lab/planning/__init__.py（导入顺序 = 界面上的排列顺序）
from toolpath_lab.planning import my_strategy as _my_strategy  # noqa: F401
```

重启程序，界面"刀路"分组与 `GET /api/catalog` 里就会出现它，`POST /api/plan` 也会接受
`{"planner": {"id": "my_strategy", ...}}`。

**id 必须唯一**：注册表遇到重复 id 会直接抛 `RegistryError`。示例模板的 id 是 `contour_demo`
（特意与内置的 `contour` 区分开），所以你可以直接把它复制进 `planning/` 启用，也可以改掉 id
再写成自己的策略。

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
| feed_mm_per_min | 当前切削进给 |
| safe_height_mm | 抬刀高度（读参数 safe_height_mm，没声明时用默认值） |
| rapid_feed_mm_per_min | 快移进给（读参数 rapid_feed_mm_per_min，没声明时用默认值） |
| to_positions(points_xy) | 平面点 (N, 2) → 工件坐标 (N, 3)，Z = 0 |
| cut_move / link_move | 切削段 / 连接段 |
| rapid_between | 抬刀 → 横移 → 下刀 |
| approach_move_down / retract_move_up | 首尾的下刀与抬刀 |
| warn(message) | 记录一条提醒（会显示在界面上） |

**别踩的坑**：

- Toolpath 至少要有一段运动、Move 至少两个点，否则会抛 ParameterError；
- 每段运动必须标明类型（切削 / 连接 / 快移）与进给，否则统计、播放与导出都会失真；
- 参数键在同一个 ParameterSet 里必须唯一。

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

## 3. 再加一个参数（示范：抬刀高度、快移速度、边界处理）

安全高度、快移速度、边界处理方式过去是 planning 里的常量，现在已经全部参数化，可以直接照抄这个
模式给新能力加参数：

1. **声明**：抬刀高度与快移速度是所有策略都要的，因此声明成一份共用的
   `MOTION_PARAMETERS`（planning/base.py），策略只要并进自己的 ParameterSet：

   ```python
   parameters: ClassVar[ParameterSet] = ParameterSet(
       (
           spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                step=0.5, unit="mm", group="刀路"),
       )
   ) + MOTION_PARAMETERS
   ```

   只有某一个策略才需要的参数就直接写在它自己的 set 里，
   [raster.py](../toolpath_lab/planning/raster.py) 的 `boundary_mode` / `stock_allowance_mm` 是例子；
   联动显隐用 `visible_if={"boundary_mode": "inset"}`（界面按它自动折叠）。

2. **读取**：值从 `context.parameters` 取，构造器一律读 `PlanningContext` 上的属性——
   `context.safe_height_mm`、`context.rapid_feed_mm_per_min`。策略没有声明这两个键时，
   context 会退回 `SAFE_HEIGHT_MM` / `RAPID_FEED_MM_PER_MIN` 默认值，第三方插件因此不会被绊住。

3. **两个注意事项**：
   - 参数化的值如果会影响"几何是否可行"，就让规划失败抛 `PlanningError`（HTTP 422），
     而不是静默生成一条错误的刀路；
   - 别把偏置量随手取负：`boundary_mode` 只有 `inset` / `none`，因为扫描线在轮廓**外**取不到
     区间，负偏置会让 v 方向的刀线被静默丢掉（u 方向切出轮廓、v 方向却留下不对称的未切带）。
     想少切一圈就用 `stock_allowance_mm`，它的几何是自洽的。

4. 记得同步 README 的「参数与固定值」表格、CHANGELOG 的「未发布」，并补测试（见 §5）。

## 4. 新增导出格式

在 export/ 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 export/__init__.py 里导出，
再在 server/app.py 的 `_route_api` 里加一个分支。

## 5. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
