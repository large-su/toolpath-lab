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

## 3. 新增一种加工面

加工面是一个**高度场**：曲面就是 `z = height(x, y)`，区域轮廓仍然定义在 XY 平面上，
所以栅格、曲面精加工等策略都只要"把平面点抬到面上"这一步就能贴上去。四种内置曲面
（平面 / 斜面 / 圆柱面 / 球冠面）都在 toolpath_lab/core/surface.py，新增一种只要一个类：

```python
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.surface import SURFACES, Surface


@SURFACES.register
@dataclass(frozen=True, slots=True)
class RippleSurface(Surface):
    """沿 X 方向起伏的正弦波纹面。"""

    amplitude_mm: float = 3.0
    wavelength_mm: float = 40.0

    id: ClassVar[str] = "ripple"
    label: ClassVar[str] = "波纹面"
    description: ClassVar[str] = "沿 X 方向的正弦起伏，用来看更陡的曲面上刀路怎么贴"
    parameters: ClassVar[ParameterSet] = ParameterSet((
        spec("amplitude_mm", "波幅", K.FLOAT, 3.0, minimum=0.5, maximum=20.0,
             step=0.5, unit="mm", group="曲面"),
        spec("wavelength_mm", "波长", K.FLOAT, 40.0, minimum=5.0, maximum=400.0,
             step=5.0, unit="mm", group="曲面"),
    ))

    def height(self, x: Any, y: Any):
        grid_x, _ = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        )
        return self.amplitude_mm * np.sin(2.0 * np.pi * grid_x / self.wavelength_mm)
```

三个要点：

- `height()` 必须**向量化**：标量或同形状数组都要能算，刀路采样与三维网格都是整批调用它；
- 曲面默认"处处有定义"；像圆柱面 / 球冠面那样只在某个半径内有定义时，覆盖 `covers()`
  与 `domain_radius_mm` —— 区域盖不住时框架会抛 `PlanningError`（HTTP 422）并给出中文原因；
- `is_planar` 声明"一条刀线只要两个端点"；曲面保持默认 `False`，策略就会按**曲面采样步长**取点。

界面、能力目录与三维毛坯都会自动包含新曲面：`Surface.sample_grid()` 采出来的网格就是前端
画被加工面的那一份数据，`describe()` 会出现在 `/api/catalog` 的 `surfaces` 段里。

## 4. 把固定值变成参数

planning/base.py 里现在是常量：

```python
SAFE_HEIGHT_MM = 5.0
RAPID_FEED_MM_PER_MIN = 5000.0
```

想在界面上可调，就在策略的 ParameterSet 里加一条 spec("safe_height_mm", ...)，把
`context.rapid_between(...)` 换成读参数即可。**边界处理方式**（现在固定为"内缩一个刀具半径"）
同理：把 `context.tool.footprint_radius_mm` 换成按参数取 0 / 半径 / 负半径。

## 5. 新增导出格式

在 export/ 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 export/__init__.py 里导出，
再在 server/app.py 的 `_route_api` 里加一个分支。

## 6. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
