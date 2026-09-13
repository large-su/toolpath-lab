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

## 4. 新增导出格式

在 export/ 写一个纯函数 `toolpath_to_xxx(toolpath, **options) -> str`，在 export/__init__.py 里导出，
再在 server/app.py 的 `_route_api` 里加一个分支。

## 5. 新增 CAM 加工类型（钻孔、等高铣、螺纹铣……）

比新增刀路策略多一步"登记"，但界面上的加工类型下拉框、工序树的类型标签、参数面板都是自动的。

**第一步**：在 `toolpath_lab/core/operation.py` 的 `OperationKind` 里加枚举值，并补上中文标签：

```python
class OperationKind(str, Enum):
    ...
    DRILL = "drill"

OPERATION_KIND_LABELS[OperationKind.DRILL.value] = "钻孔"
```

**第二步**：在 `toolpath_lab/cam/` 下写规划函数。它拿到的是
`MillingContext`（刀具、起始高度、目标高度、已校验的参数、加工区域）：

```python
# toolpath_lab/cam/drill.py
class DrillPlanner:
    id = "drill"
    label = "钻孔"
    parameters = cam_parameters() + ParameterSet((
        spec("peck_mm", "每次钻深", K.FLOAT, 3.0, unit="mm", group="钻孔"),
    ))

    def plan(self, context: MillingContext) -> Toolpath:
        builder = MoveBuilder(context)
        # 用加工区域里的孔位（context.region.islands 就是岛屿/孔）逐个下钻
        ...
        return builder.finish(planner=self.id, label=self.label, notes=(...))
```

**第三步**：在 `toolpath_lab/cam/service.py` 的 `execute_operation` 里加一个分支，
并在 `planning_catalog()` 的 `operations` 列表里加一条（界面上的下拉框就出来了）。

加工区域的可用成员：

| 成员 | 说明 |
| --- | --- |
| outline / islands | 外轮廓与岛屿（世界 XY，(N, 2)） |
| top_z / floor_z | 这一层的起始高度与目标高度 |
| inside | 刀心可行区域的布尔掩码（已按刀具半径 + 余量偏置） |
| distance | 每个格点到轮廓的距离（毫米），等距与防撞判断都靠它 |
| offset_mask(mm) / offset_outline_polygons(mm) | 等距区域与它的闭合边界环 |
| scanline_levels / scanline_intervals | 按走刀方向布刀线与取区间 |

## 6. 新增毛坯类型

继承 `Stock`、声明参数、注册到 `STOCK_TYPES`（`toolpath_lab/core/stock.py`）。
必须实现 `volume_mm3()` 与 `build_mesh()`（界面预览与仿真都要用），
`bounds` 决定刀路深度与仿真栅格范围。

## 7. 新增三维模型格式

在 `toolpath_lab/step/` 旁边写一个 reader，返回同一个 `TessellatedModel`
（positions / indices / normals / face_of_triangle / faces / loops），然后让
`PartModel` 指向它即可——毛坯、特征、刀路、仿真全都不用改。
`FaceRecord.loops` 是型腔铣的区域来源，务必把边界环填上。

## 8. 约定与检查清单

- 单位：毫米、秒、度；角度只在 API 边界出现，核心内部用弧度；
- 坐标：右手系、Z 轴向上、XY 是加工平面；数组一律 float64；
- 错误：参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422），
  文件格式错误抛 StepFormatError（400）、体积超限抛 StepSizeError（413）；
- 用户可见文案用中文（放在 label / help），代码注释与文档字符串用中文；
- 每个新能力都要补测试，`python -m unittest discover -s tests` 必须全绿；
- 动了前端就顺手跑一次 `electron/smoke.mjs`：它会真的把界面加载起来，检查控制台错误与关键 DOM。
