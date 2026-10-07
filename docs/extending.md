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

内置七种形状：方形、矩形、圆形、椭圆、U 形（凹）、哑铃形（细颈 + 两端方头）、三角形
（顶角可调尖），都在 `toolpath_lab/core/region.py`。再加一种就是照着它们写一个类——
下面以"跑道形"为例（`__post_init__` 要守住自己的不变式）：

```python
from dataclasses import dataclass
from typing import ClassVar
import numpy as np
from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.region import CURVE_SEGMENTS, REGION_SHAPES, RegionShape


@REGION_SHAPES.register
@dataclass(frozen=True, slots=True)
class StadiumRegion(RegionShape):
    """矩形两端各接一个半圆的跑道形。"""

    length_mm: float = 100.0
    width_mm: float = 40.0

    id: ClassVar[str] = "stadium"
    label: ClassVar[str] = "跑道形"
    description: ClassVar[str] = "矩形两端各接一个半圆"
    parameters: ClassVar[ParameterSet] = ParameterSet((
        spec("length_mm", "总长", K.FLOAT, 100.0, minimum=5.0, maximum=1000.0,
             step=5.0, unit="mm", group="区域"),
        spec("width_mm", "宽", K.FLOAT, 40.0, minimum=5.0, maximum=1000.0,
             step=5.0, unit="mm", group="区域"),
    ))

    def __post_init__(self) -> None:
        if self.length_mm <= 0 or self.width_mm <= 0:
            raise ParameterError("跑道形的长与宽都必须为正")

    def boundary(self):  # 逆时针、不重复首点
        ...
```

**形状契约**（`tests/test_region.py` 的 `ShapeContractTests` 会逐条检查）：

- 返回 `(N, 2)` 的 float64 多边形：**逆时针**、**不重复首点**、至少三个点、坐标有限；
- 包围盒左右 / 上下对称（三维取景与工件厚度都按包围盒算；材料重心不必在原点，凹形状就不在）；
- 曲线边界用共用的 `CURVE_SEGMENTS`（180）段折线逼近，别再引入新的魔数；
- 参数用 `spec(...)` 声明，界面控件自动生成，`build_region(id, params)` 自动校验范围；
  参数之间的耦合关系（例如"壁厚必须小于外宽的一半"）写在 `__post_init__` 里抛 `ParameterError`。

做到这些之后**刀路与三维显示都不需要改**：栅格刀路靠扫描线求交，工件直接按这条边界挤出
（`web/js/viewport.js`）。**凹形状**是这条承诺的试金石：U 形横穿两条臂的扫描线会得到两段独立刀轨
（`tests/test_planners.py` 的 `test_a_concave_region_puts_two_passes_on_the_same_level`），
哑铃形的细颈被环切偏置吃掉后一层会分裂成两条环（`tests/test_contour.py` 的 `MultiLoopTests`），
工件也会被挤出成真正的形状而不是方盒。

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

套路固定：**写纯函数 → 在 `export/__init__.py` 导出 → 在 `server/app.py` 的 `_route_api` 加一个分支**。
内置两个例子：[export/gcode.py](../toolpath_lab/export/gcode.py)（NC 程序）与
[export/csv.py](../toolpath_lab/export/csv.py)（点表）。签名照抄它们：

```python
def toolpath_to_xxx(toolpath: Toolpath, *, decimals: int = 3, **options) -> str:
    ...
```

约定：

- **纯函数**：输入 `Toolpath`，输出 `str`；不要碰 HTTP、文件系统或全局状态，这样它既能被接口用，
  也能被 `examples/` 里的脚本直接用；
- **不要再造一份刀路数据**：一切都从 `toolpath.moves`（类型 / 进给 / `pass_index` / 点）和
  `toolpath.notes` 里取，统计用 `toolpath.statistics()`；
- **编码尽量选安全的那一边**：csv.py 刻意只输出 ASCII（不带 BOM 的 UTF-8 也能被 Excel、pandas、
  `csv.reader` 直接读），gcode.py 用 UTF-8 输出中文注释——文本文件按各自生态的惯例挑；
- 路由里用 `text_response(..., content_type=..., filename=...)` 返回，文件名统一走
  `_export_filename(request, "后缀")`；
- 参数不合法/几何不可行仍然由 `PlanRequest.from_payload` 与规划层抛出，
  接口层不需要额外校验（400 / 422 自动生效）。

记得补测试：纯函数用数值断言（列名、行数 = 刀点数、坐标与 `move.points` 一致），
接口参考 `tests/test_api.py` 的 `ExportTests`（状态码、`Content-Type`、`Content-Disposition`、行数）。

## 5. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
