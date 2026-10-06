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

## 3. 把固定值变成参数

planning/base.py 里现在是常量：

```python
SAFE_HEIGHT_MM = 5.0
RAPID_FEED_MM_PER_MIN = 5000.0
```

想在界面上可调，就在策略的 ParameterSet 里加一条 spec("safe_height_mm", ...)，把
`context.rapid_between(...)` 换成读参数即可。**边界处理方式**（现在固定为"内缩一个刀具半径"）
同理：把 `context.tool.footprint_radius_mm` 换成按参数取 0 / 半径 / 负半径。

注意：材料切除仿真按刀路真实的 Z 削料，所以改安全高度不会改变切削结果，改**切削深度**
（也就是刀路的 Z）才会。

## 4. 新增导出格式

在 export/ 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 export/__init__.py 里导出，
再在 server/app.py 的 `_route_api` 里加一个分支。

## 5. 让材料切除仿真认得新的刀具

材料切除仿真（simulation/material.py）按 `core/tool.py` 的**足迹半径**推出圆角半径
（`rc = R - footprint_radius`），所以启用球头刀/圆鼻刀时不需要改仿真：

| 刀具 | 足迹半径 | 仿真里的 rc | 刀底形状 | 现在可选 |
| --- | --- | --- | --- | --- |
| 平底刀 | R | 0 | 平底圆盘（ae ≤ D 时底面绝对平） | ✅ |
| 球头刀 | 0 | R | 标准球面，相邻刀路间留扇贝形残留 | ✅ |
| 圆鼻刀 | R − Rc | Rc | 平底 + 圆角环（Rc 越大越接近球面） | ✅ |

三种形态共用同一条刀底公式，所以它们的**关系是连续的**：Rc = 0 就是平底刀，
Rc = R 就是球头刀，中间是圆鼻刀。新增刀型时这一点是重要的回归依据——
边缘抬起量应当满足"平底刀 < 圆鼻刀 < 球头刀"。

新增**其它**刀型（锥度刀、成形刀）时，把 `carve()` 里那段"离刀心 d 处刀底抬升多少"改成
新刀型的解析式即可；其余流程（网格、采样、帧、载荷）都不用动。具体还要动三处：

1. `core/tool.py`：加进 `ToolKind` 与 `TOOL_KINDS`，给出 `footprint_radius_mm` 与
   `effective_corner_radius_mm`（圆鼻刀就是这么加的，可作模板）；
2. `web/js/viewport.js` 的 `setTool()`：补上刀底形状。圆鼻刀用 `LatheGeometry` 把
   "平盘 → 圆角圆弧 → 圆柱面"的母线绕刀轴旋转成实体，可作模板；
3. `tests/`：加一条剖面测试，把解析解与仿真结果对齐（见 `BullToolTests`）。

注意毛坯尺寸按 `tool.radius_mm` 定界，刀体伸出半径之外的刀型要相应调整。

**网格够不够细**不用在这里操心：`_grid_step()` 会按相邻刀轨的间距自动加密到每条刀轨
至少 `CELLS_PER_PASS` 格，用户给的 `resolution_mm` 只是上限。

## 6. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用英文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿。
