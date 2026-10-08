"""栅格（平行扫描线）刀路。

两种模式：

- **往复 Zigzag**：奇数刀反向，相邻两刀在端头直接连过去，效率高；
- **单向 One-way**：每刀都朝同一个方向，刀与刀之间抬刀到安全面再回到起点，
  慢一些，但每一刀的切削状态一致（顺铣/逆铣方向固定）。

做法很简单，也是这个基座最值得读的一段代码：

1. 把区域轮廓旋转到"走刀坐标系"：u 沿走刀方向，v 垂直于它；
2. 在 v 方向每隔一个切宽布一条刀线；
3. 每条刀线用扫描线求交，得到它在区域内部的区间（圆形是弦，方形是整条）；
4. 区间两端各内缩一个刀具足迹半径，得到这一刀的起点和终点；
5. 按模式决定方向与刀间连接，补上下刀和抬刀。

加工面是平面时，一刀只要两个端点。换成曲面（PlanningContext.surface）之后，
每条刀线按 sample_step_mm 离散，再交给 to_positions() 逐点抬到曲面上——策略
本身既不知道曲面的形状，也不知道自己在生成"三维"刀路。
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import direction_2d
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import scanline_intervals
from toolpath_lab.planning.registry import PLANNERS

_MODE_LABELS = {"zigzag": "往复", "one_way": "单向"}

#: 末刀余量小于切宽的多少倍时补一刀，保证区域被切满。
_ALIGN_TOLERANCE = 0.05

#: 曲面加工时沿刀线的默认采样步长（mm）。
DEFAULT_SAMPLE_STEP_MM = 1.0


def sample_pass(
    level: float, start: float, end: float, *, reverse: bool, step: float, planar: bool
) -> np.ndarray:
    """一条刀线的平面点集：平面只要两个端点，曲面按步长离散。

    离散是"让刀贴着曲面走"的唯一动作，因此放在这里而不是策略里——任何扫描线
    策略都可以直接调用它。
    """

    if reverse:
        start, end = end, start
    if planar or step <= 0.0:
        return np.array([[start, level], [end, level]], dtype=np.float64)
    count = max(2, int(np.ceil(abs(end - start) / step)) + 1)
    along = np.linspace(start, end, count, dtype=np.float64)
    return np.column_stack((along, np.full(count, float(level), dtype=np.float64)))


@PLANNERS.register
class RasterPlanner(Planner):
    """平行扫描线，往复或单向。"""

    id: ClassVar[str] = "raster"
    label: ClassVar[str] = "栅格刀路"
    description: ClassVar[str] = "平行扫描线：往复（之字形）或单向（每刀抬刀返回）"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("mode", "走刀模式", K.CHOICE, "zigzag", group="刀路", choices=(
                Choice("zigzag", "往复 Zigzag"),
                Choice("one_way", "单向 One-way"),
            )),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两条刀线的间距"),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="刀路", help="扫描线的行进方向；切宽方向与之垂直"),
            spec("sample_step_mm", "曲面采样步长", K.FLOAT, DEFAULT_SAMPLE_STEP_MM,
                 minimum=0.2, maximum=20.0, step=0.2, unit="mm", group="刀路",
                 help="加工曲面时沿刀线每隔这么长取一个点贴到曲面上；平面只用两个端点"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        return self._plan_raster(
            context,
            mode=str(context.parameters["mode"]),
            stepover=self.require_positive(
                float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
            ),
            direction_deg=float(context.parameters["direction_deg"]),
            sample_step=float(context.parameters.get("sample_step_mm") or 0.0),
        )

    # -- 主体；"曲面精加工"只是换一种方式算出切宽，然后复用这一段 -----------
    def _plan_raster(
        self,
        context: PlanningContext,
        *,
        mode: str,
        stepover: float,
        direction_deg: float,
        sample_step: float,
    ) -> Toolpath:
        offset = context.tool.footprint_radius_mm
        self._warn_if_stepover_too_large(context, stepover)

        boundary = context.boundary
        u_axis = direction_2d(direction_deg)
        v_axis = np.array([-u_axis[1], u_axis[0]], dtype=np.float64)
        frame = np.column_stack((u_axis, v_axis))
        planar = boundary @ frame

        levels = self._pass_levels(planar[:, 1], stepover, offset)
        passes: list[tuple[float, float, float]] = []
        for level in levels:
            for interval in scanline_intervals(planar, float(level)):
                start = interval.start + offset
                end = interval.end - offset
                if end - start <= 1e-6:
                    continue
                passes.append((start, end, float(level)))
        if not passes:
            raise PlanningError(
                "没有生成任何刀轨：请检查区域尺寸、刀具直径与切宽是否匹配"
            )

        flat = bool(context.surface.is_planar)
        moves: list[Move] = []
        first = sample_pass(passes[0][2], passes[0][0], passes[0][1],
                            reverse=False, step=sample_step, planar=flat)
        moves.append(context.approach_move_down(context.to_positions(first @ frame.T)[0]))

        previous: np.ndarray | None = None
        for index, (start, end, level) in enumerate(passes):
            reverse = mode == "zigzag" and index % 2 == 1
            planar_points = sample_pass(level, start, end, reverse=reverse,
                                        step=sample_step, planar=flat)
            positions = context.to_positions(planar_points @ frame.T)
            if previous is not None:
                moves.append(
                    context.link_move(previous, positions[0])
                    if mode == "zigzag"
                    else context.rapid_between(previous, positions[0])
                )
            moves.append(context.cut_move(planar_points @ frame.T, pass_index=index,
                                          label=f"第 {index + 1} 刀"))
            previous = positions[-1]

        moves.append(context.retract_move_up(previous))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(context, mode, stepover, len(passes)),
        )

    # -- 内部步骤 ----------------------------------------------------------
    @staticmethod
    def _pass_levels(
        v_values: np.ndarray, stepover: float, offset: float
    ) -> np.ndarray:
        """按切宽在 v 方向布刀，两端各内缩一个刀具足迹半径。"""

        v_start = float(v_values.min()) + offset
        v_end = float(v_values.max()) - offset
        if v_end - v_start < -1e-6:
            raise PlanningError(
                f"刀具足迹半径 {offset:g} mm 已经超过区域在该方向上的宽度，"
                "请减小刀具直径或扩大区域"
            )
        count = int(np.floor((v_end - v_start) / stepover + 1e-9)) + 1
        levels = v_start + np.arange(count, dtype=np.float64) * stepover
        if (v_end - float(levels[-1])) > _ALIGN_TOLERANCE * stepover:
            levels = np.append(levels, v_end)
        return levels

    @staticmethod
    def _warn_if_stepover_too_large(context: PlanningContext, stepover: float) -> None:
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，"
                "两刀之间会留下未切除的残余"
            )

    def _notes(
        self, context: PlanningContext, mode: str, stepover: float, pass_count: int
    ) -> tuple[str, ...]:
        direction = float(context.parameters["direction_deg"])
        notes = [
            f"{_MODE_LABELS[mode]}走刀，共 {pass_count} 刀，"
            f"切宽 {stepover:g} mm，走刀方向 {direction:g}°",
            f"边界内缩一个刀具半径（本刀 R{context.tool.footprint_radius_mm:g} mm），"
            "安全高度 5 mm、快移 5000 mm/min 为固定值",
        ]
        if not context.surface.is_planar:
            notes.append(
                f"加工面为{context.surface.label}：每条刀线按曲面采样后逐点贴合"
            )
        return tuple(notes)
