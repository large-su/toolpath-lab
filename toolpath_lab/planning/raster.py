"""栅格（平行扫描线）刀路。

两种模式：

- **往复 Zigzag**：奇数刀反向，相邻两刀在端头直接连过去，效率高；
- **单向 One-way**：每刀都朝同一个方向；刀与刀之间默认抬刀到安全面再回到起点，也可以改成
  沿加工面连接（`linking`），慢一些，但每一刀的切削状态一致（顺铣/逆铣方向固定）。

做法很简单，也是这个基座最值得读的一段代码：

1. 把刀路范围（`machining_boundary`，斜坡"只加工斜面段"时比工件轮廓窄）旋转到
   "走刀坐标系"：u 沿走刀方向，v 垂直于它；
2. 在 v 方向每隔一个切宽布一条刀线；
3. 每条刀线用扫描线求交，得到它在区域内部的区间（圆形是弦，方形是整条）；
4. 区间两端各内缩一个刀具足迹半径，得到这一刀的起点和终点；
5. 按模式决定方向与刀间连接，补上下刀和抬刀。

加工面不是水平面时还多做两件事（都放在 `PlanningContext` 里）：走刀方向摆成"由低往高"，
下刀改成从低处沿加工面切入。平面区域仍然是"一刀两个点、垂直下刀、刀间抬刀"。
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
from toolpath_lab.planning.base import (
    ENTRY_LEAD_IN_MM,
    LAYER_PARAMETERS,
    SURFACE_PARAMETERS,
    Planner,
    PlanningContext,
)
from toolpath_lab.planning.geometry2d import scanline_intervals
from toolpath_lab.planning.registry import PLANNERS

_MODE_LABELS = {"zigzag": "往复", "one_way": "单向"}

#: 末刀余量小于切宽的多少倍时补一刀，保证区域被切满。
_ALIGN_TOLERANCE = 0.05


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
            spec("linking", "刀间连接", K.CHOICE, "auto", group="刀路",
                 choices=(
                     Choice("auto", "自动（平面抬刀，斜面沿面连接）"),
                     Choice("retract", "抬刀到安全面"),
                     Choice("surface", "沿加工面连接（不抬刀）"),
                 ),
                 help="只对单向模式有效；沿面连接贴着加工面切过去，斜面上因此不必每次抬到安全面",
                 visible_if={"mode": "one_way"}),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两条刀线的间距"),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="刀路", help="扫描线的行进方向；切宽方向与之垂直"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        ) + SURFACE_PARAMETERS + LAYER_PARAMETERS
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        self._warn_if_stepover_too_large(context, stepover)

        # 加工面有起伏时改成"由低往高"：这样下刀的那一端就是低处，沿面切入不会撞上高处的材料。
        # 分层粗加工的每一层用同一个方向，粗精加工的方向因此保持一致。
        u_axis = direction_2d(float(context.parameters["direction_deg"]))
        flipped = context.entry_along_surface and context.surface_rise(u_axis) < 0.0

        # 先逐层把毛坯铣掉（每层是水平面，范围收窄到该层还有料的地方），
        # 最后再沿加工面走一遍——这一遍就是精加工，也是不分层时的全部刀路。
        levels = context.layer_levels()
        moves: list[Move] = []
        index = 0
        produced = 0
        for z in levels:
            level_moves, index = self._pass_moves(context.at_level(z), index, flip=flipped)
            if level_moves:
                produced += 1
            moves.extend(level_moves)
        if produced < len(levels):
            context.warn(
                f"有 {len(levels) - produced} 层在那个高度上剩下的料比刀还窄（放不下刀具），已跳过"
            )
        finish_moves, index = self._pass_moves(context, index, flip=flipped)
        if not finish_moves:
            raise PlanningError(
                "没有生成任何刀轨：请检查区域尺寸、刀具直径与切宽是否匹配"
            )
        moves.extend(finish_moves)

        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(context, index, produced, flipped),
        )

    # -- 内部步骤 ----------------------------------------------------------
    def _pass_moves(
        self, context: PlanningContext, first_index: int, *, flip: bool
    ) -> tuple[list[Move], int]:
        """在给定上下文上跑一遍完整的栅格走法（分层时是某一层，最后是沿加工面的一遍）。

        返回（这一段运动, 下一段要用的走刀序号）。这一层没有料（范围退化）时返回空。
        """

        mode = str(context.parameters["mode"])
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        offset = context.tool.footprint_radius_mm
        boundary = context.machining_boundary
        if boundary.shape[0] < 3:
            return [], first_index  # 这一层已经没有料了

        u_axis = direction_2d(float(context.parameters["direction_deg"]))
        if flip:
            u_axis = -u_axis
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
            return [], first_index

        moves: list[Move] = []
        first_line = self._pass_line(passes[0], frame, reverse=False)
        moves.extend(
            context.entry_moves(first_line[0], first_line[1], pass_index=first_index)
        )

        previous: np.ndarray | None = None
        for step, item in enumerate(passes):
            index = first_index + step
            reverse = mode == "zigzag" and step % 2 == 1
            world = self._pass_line(item, frame, reverse=reverse)
            positions = context.to_positions(world)
            if previous is not None:
                if mode == "zigzag":
                    moves.append(context.link_move(previous, positions[0]))
                elif context.links_on_surface:
                    moves.append(
                        context.surface_link(previous[:2], positions[0][:2], pass_index=index)
                    )
                else:
                    moves.append(context.rapid_between(previous, positions[0]))
            moves.append(context.cut_move(world, pass_index=index, label=f"第 {index + 1} 刀"))
            previous = positions[-1]

        if previous is not None:
            moves.append(context.retract_move_up(previous))
        return moves, first_index + len(passes)

    @staticmethod
    def _pass_line(
        item: tuple[float, float, float], frame: np.ndarray, *, reverse: bool
    ) -> np.ndarray:
        """一条刀线的两个端点（工件坐标，XY）。"""

        start, end, level = item
        planar = np.array(
            [[end, level], [start, level]] if reverse else [[start, level], [end, level]],
            dtype=np.float64,
        )
        return planar @ frame.T

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

    @staticmethod
    def _notes(
        context: PlanningContext,
        pass_count: int,
        layer_count: int,
        flipped: bool,
    ) -> tuple[str, ...]:
        mode = str(context.parameters["mode"])
        stepover = float(context.parameters["stepover_mm"])
        direction = float(context.parameters["direction_deg"])
        surface: list[str] = []
        if context.entry_along_surface:
            surface.append(f"由低往高走刀，下刀沿加工面切入 {ENTRY_LEAD_IN_MM:g} mm")
        if mode == "one_way":
            surface.append(
                "刀间沿加工面连接（不抬刀）" if context.links_on_surface
                else "刀间抬刀到安全面"
            )
        if flipped:
            surface.append(f"斜面自动反向：实际走刀方向 {direction + 180.0:g}°")
        notes = [
            f"{_MODE_LABELS[mode]}走刀，共 {pass_count} 刀，"
            f"切宽 {stepover:g} mm，走刀方向 {direction:g}°",
            f"边界内缩一个刀具足迹半径（本刀 {context.tool.footprint_radius_mm:g} mm），"
            "安全高度 5 mm、快移 5000 mm/min 为固定值",
        ]
        if layer_count and context.layer_depth_mm > 0:
            _, high = context.surface_z_range
            notes.append(
                f"分层粗加工：每层 {context.layer_depth_mm:g} mm、共 {layer_count} 层"
                f"（毛坯顶面在 {high + context.stock_margin_mm:g} mm），"
                "每层只切该高度上还有料的范围，最后沿加工面精加工一刀"
            )
        steps = context.region.surface_step_levels()
        if steps and context.stock_margin_mm > 0:
            notes.append(
                f"平台高度 {steps[0]:g} mm 上单独走一遍：那一层覆盖整个区域，"
                "把平台之上的毛坯清掉（沿加工面的那一遍只走斜面段，到不了平台）"
            )
        if surface:
            notes.append("；".join(surface))
        return tuple(notes)
