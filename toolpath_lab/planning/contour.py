"""环切（等距轮廓）刀路。

从区域轮廓开始，每向内偏置一个切宽就得到一层环，偏置到结果退化为止：

1. 第一层把轮廓向内偏置"刀具在加工面上的足迹半径"，保证刀不切出区域；
2. 之后每层再向内偏置一个切宽；
3. 一层可能得到**多条环**：凹形状的细颈被偏置吃掉后，形状会分裂成互不相连的几块；
4. 每环按采样步长离散成闭合折线，相邻环绕行方向交替（顺铣/逆铣交替）；
5. 环之间的过渡分两种：套在里面的环用连接进给直接走进去；同一层分裂出来的兄弟环，或者
   无法判断内外关系时，抬刀到安全面再快移过去——在切削深度上横穿细颈会啃到材料。

偏置几何在 planning/geometry2d.py（凸角斜接、凹角圆弧接头、按自交点切分、"转角最小"接环），
那里也写了为什么不能像早期版本那样把断开的顶点按原顺序接起来。

已知限制：环与环之间只有直线连接，没有引入 / 引出圆弧。

**偏置的延长上限不是正确性开关**：凸角的斜接点离顶点 d·tan(转角/2)，看起来"尖角需要很长
延长"，但斜接点一旦落在平移线段之外，那个角两侧的边长就必然小于需要的延长，于是局部材料
宽度最大只有 2L·sin(内角/2) < 2d——比刀具还窄，整个角早被侵蚀掉了，那条边本来就不该出现。
所以 `geometry2d` 里给的是一个小数值余量；真正"什么都没剩下"的情形（细到放不下刀具的区域）
会走到下面的 PlanningError。

**环绕向与顺逆铣**：`offset_loops` 给出的环都是逆时针的。环切是从外往内走的，未加工的材料
始终在环的**内侧**（外面那一圈已经被上一环切掉了），所以

- **逆时针** = 接触点的刀刃运动方向与进给同向 = **顺铣**；
- **顺时针** = **逆铣**。

这是按"主轴 M03（从上往下看顺时针）+ 右手刀具"推出来的。如果机床用 M04，或者材料在刀路的
另一侧（例如加工的是外轮廓而不是这块区域），顺铣与逆铣就要对调——参数名按几何绕向标注，
约定写在 README 的「参数与固定值」里。栅格策略没有这个参数：一刀的两侧一侧顺、一侧逆，
没有单一答案，往复本身就是在交替。
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import MOTION_PARAMETERS, Planner, PlanningContext
from toolpath_lab.planning.geometry2d import offset_loops, point_in_polygon, resample_ring
from toolpath_lab.planning.registry import PLANNERS

#: 偏置后面积小于这个值的环直接丢掉（mm²）：细到没有加工意义的碎片不值得走一刀。
_MIN_RING_AREA_MM2 = 0.5

#: 环绕向 → notes 里的说明文案。
_DIRECTION_LABELS = {
    "alternate": "交替（顺铣/逆铣）",
    "climb": "全顺铣（逆时针）",
    "conventional": "全逆铣（顺时针）",
}


@PLANNERS.register
class ContourPlanner(Planner):
    """沿区域轮廓逐圈向内偏置的环切刀路。"""

    id: ClassVar[str] = "contour"
    label: ClassVar[str] = "环切"
    description: ClassVar[str] = "从轮廓逐圈向内偏置（等距轮廓），凹形状分裂出的环会分别加工"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路",
                 help="每环离散成折线的点距；越小越贴合曲线，刀点也越多"),
            spec("ring_direction", "环绕向", K.CHOICE, "alternate", group="刀路",
                 choices=(
                     Choice("alternate", "交替（顺铣/逆铣）"),
                     Choice("climb", "全顺铣（逆时针）"),
                     Choice("conventional", "全逆铣（顺时针）"),
                 ),
                 help="顺逆铣：按 M03 主轴 + 右手刀具，逆时针为顺铣；约定见 README「参数与固定值」"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    ) + MOTION_PARAMETERS

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        ring_direction = str(context.parameters["ring_direction"])
        boundary = context.boundary
        distance = context.tool.footprint_radius_mm

        layers: list[list[NDArray[np.float64]]] = []
        while True:
            loops = offset_loops(boundary, distance, min_area_mm2=_MIN_RING_AREA_MM2)
            if not loops:
                break
            layers.append(loops)
            distance += stepover
        if not layers:
            raise PlanningError(
                f"环切没有生成任何刀轨：刀具足迹半径 {context.tool.footprint_radius_mm:g} mm "
                "已经超过区域的内切半径，请减小刀具直径或扩大区域"
            )

        moves: list[Move] = []
        previous: NDArray[np.float64] | None = None
        previous_loop: NDArray[np.float64] | None = None
        index = 0
        for loops in layers:
            for loop in loops:
                sampled = resample_ring(loop, sample_step)
                closed = np.vstack([sampled, sampled[:1]])
                if self._should_reverse(ring_direction, index):
                    closed = closed[::-1]
                positions = context.to_positions(closed)
                if previous is None:
                    moves.append(context.approach_move_down(positions[0]))
                elif previous_loop is not None and self._is_nested(loop, previous_loop):
                    moves.append(context.link_move(previous, positions[0]))
                else:
                    moves.append(context.rapid_between(previous, positions[0]))
                moves.append(
                    Move(
                        MoveKind.CUT,
                        positions,
                        context.feed_mm_per_min,
                        pass_index=index,
                        label=f"第 {index + 1} 环",
                    )
                )
                previous = positions[-1]
                previous_loop = loop
                index += 1
        moves.append(context.retract_move_up(previous))

        ring_count = sum(len(loops) for loops in layers)
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"环切：共 {ring_count} 环（{len(layers)} 层），切宽 {stepover:g} mm，"
                f"采样步长 {sample_step:g} mm，环绕向 {_DIRECTION_LABELS[ring_direction]}",
                "同层分裂出的环之间抬刀快移，套在里面的环之间用连接进给",
                f"边界固定内缩一个刀具足迹半径（R{context.tool.footprint_radius_mm:g} mm），"
                f"安全高度 {context.safe_height_mm:g} mm、"
                f"快移 {context.rapid_feed_mm_per_min:g} mm/min",
            ),
        )

    @staticmethod
    def _should_reverse(ring_direction: str, index: int) -> bool:
        """这一环要不要反向走。

        offset_loops 给的环是逆时针的，也就是顺铣（材料在环内侧）；所以
        "全顺铣"保持原样、"全逆铣"全部反向、"交替"按顺序奇偶交替（原来唯一的行为）。
        """

        if ring_direction == "climb":
            return False
        if ring_direction == "conventional":
            return True
        return index % 2 == 1

    @staticmethod
    def _is_nested(inner: NDArray[np.float64], outer: NDArray[np.float64]) -> bool:
        """inner 是否落在 outer 内部（用来决定环间是连接进给还是抬刀快移）。"""

        return bool(point_in_polygon(inner[:1], outer)[0])
