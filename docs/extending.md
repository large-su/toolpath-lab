# 扩展指南

所有扩展点都是同一个套路：**声明参数 → 实现核心方法 → 注册**。注册之后，界面、接口、能力目录
都会自动包含它。

## 1. 新增一个刀路策略（推荐从这里开始）

最完整的参考就在主程序里：`toolpath_lab/planning/follow_periphery.py`（跟随周边）——它声明了一个
选项型参数和三个数值参数，用等距偏置生成每一环，再把环拼成 `Toolpath`。

仓库里另有一份可直接复制的示例插件（环切），复制进主程序即可启用：

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/contour.py
```

```python
# toolpath_lab/planning/__init__.py
from toolpath_lab.planning import contour as _contour  # noqa: F401
```

导入顺序就是界面上策略的排列顺序（内置的两个是 raster、follow_periphery）。

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

**planning.geometry2d 里现成的几何**（都是纯函数，可以直接复用）：

| 函数 | 说明 |
| --- | --- |
| `scanline_intervals(polygon, level)` | 直线与多边形求交、偶奇配对 → 内部区间（栅格刀路用） |
| `offset_polygon(polygon, distance)` | 等距偏置，向内为正、向外为负；凸角斜接、凹角补圆弧（跟随周边用） |
| `resample_ring(polygon, step_mm)` | 闭合环按等弧长重采样成刀点 |
| `ensure_ccw` / `signed_area` / `bounding_box` | 规范化、有向面积、外接矩形 |

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

加工面不是平面时，再补下面这些（`core/region.py` 里的斜坡就是完整例子）：

| 方法 | 作用 |
| --- | --- |
| `height_at(points_xy)` | 每个 (x, y) 处的 Z；刀路、G-code、时间轴都读它 |
| `surface_breaks(start, end)` | 分片平面的折角位置（斜坡在"斜面转平顶"的折痕处返回一点），折线在那里补点后才真正贴合加工面 |
| `machining_boundary(footprint_mm)` | 刀路可取范围；默认就是轮廓。加工面在区域**内部**转折时（斜坡只加工斜面段）收窄到要加工的那一段，并把分界处的边外扩一个足迹，好让内缩之后刀路正好停在那里 |
| `surface_patches()` | 顶面分片（每片共面且凸），前端据此拼工件实体；默认就是轮廓本身一片 |

有了这些，**策略一行都不用改**：`PlanningContext.to_positions()` 会替你补点、取 Z，
斜面上还会自动由低往高走刀、沿加工面切入与连接，横移的安全面也会抬到该段路径最高点之上 5 mm。

## 3. 把固定值变成参数

planning/base.py 里现在是常量：

```python
SAFE_HEIGHT_MM = 5.0
RAPID_FEED_MM_PER_MIN = 5000.0
```

想在界面上可调，就在策略的 ParameterSet 里加一条 spec("safe_height_mm", ...)，把
`context.rapid_between(...)` 换成读参数即可。**边界处理方式**（现在固定为"内缩一个刀具半径"）
同理：把 `context.tool.footprint_radius_mm` 换成按参数取 0 / 半径 / 负半径。

## 4. 新增导出格式

在 export/ 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 export/__init__.py 里导出，
再在 server/app.py 的 `_route_api` 里加一个分支。

## 5. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
