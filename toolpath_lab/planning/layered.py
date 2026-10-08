"""分层开粗（多层切削）刀路策略。

把栅格或环切的平面刀路沿深度方向逐层复制，每层下刀后完整走一遍、层间抬刀横移，
最后统一抬刀——这是型腔/槽类零件粗加工的标准流程（对应 UG 的 Cavity Mill 分层）。

实现要点：

1. 先用底层策略（栅格往复 / 环切）在 Z = 0 平面生成单层刀路；
2. 层数 = ceil(总深 / 每层切深)，每层把单层刀路平移到对应负深度；
3. 首层从安全高度直接下刀；后续每层用"抬刀-横移-下刀"从上一层终点连到本层起点；
4. 切削段的 pass_index 按层错开，统计与 G-code 输出无需任何适配。

单层刀路里的快速段（approach/retract）被丢弃，分层自己重建下刀与抬刀。
"""

from __future__ import annotations

from math import ceil
from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.planning.base import (
    RAPID_FEED_MM_PER_MIN,
    SAFE_HEIGHT_MM,
    Planner,
    PlanningContext,
)
from toolpath_lab.planning.contour import ContourPlanner
from toolpath_lab.planning.raster import RasterPlanner
from toolpath_lab.planning.registry import PLANNERS


@PLANNERS.register
class LayeredPlanner(Planner):
    """把栅格/环切刀路沿深度分层的粗加工策略。"""

    id: ClassVar[str] = "layered"
    label: ClassVar[str] = "分层开粗"
    description: ClassVar[str] = "沿深度方向逐层下刀切削（栅格或环切），型腔/槽类粗加工的标准流程"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("strategy", "底层策略", K.CHOICE, "raster", group="刀路", choices=(
                Choice("raster", "栅格往复"),
                Choice("contour", "环切"),
            )),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="每层内相邻两条刀线的间距"),
            spec("depth_mm", "总切深 ap", K.FLOAT, 6.0, minimum=0.5, maximum=60.0,
                 step=0.5, unit="mm", group="刀路", help="从顶面到最深处的总切削深度"),
            spec("layer_depth_mm", "每层切深", K.FLOAT, 2.0, minimum=0.1, maximum=60.0,
                 step=0.5, unit="mm", group="刀路", help="每层下刀的深度；层数 = 总深 ÷ 每层切深，向上取整"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        strategy = str(context.parameters["strategy"])
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        depth = self.require_positive(
            float(context.parameters["depth_mm"]), "总切深 depth_mm"
        )
        layer = self.require_positive(
            float(context.parameters["layer_depth_mm"]), "每层切深 layer_depth_mm"
        )
        feed = self.require_positive(
            float(context.parameters["feed_mm_per_min"]), "进给速度 feed_mm_per_min"
        )

        # -- 1. 底层单层刀路（Z = 0 平面） ------------------------------------
        base_params = {"feed_mm_per_min": feed, "stepover_mm": stepover}
        if strategy == "contour":
            base_params["sample_step_mm"] = 1.0
            base_planner: Planner = ContourPlanner()
            base_label = "环切"
        else:
            base_params["mode"] = "zigzag"
            base_params["direction_deg"] = 0.0
            base_planner = RasterPlanner()
            base_label = "栅格往复"

        child = PlanningContext(
            tool=context.tool,
            region=context.region,
            parameters={**context.parameters, **base_params},
            warnings=context.warnings,
        )
        base_toolpath = base_planner.plan(child)

        planar_moves = [m for m in base_toolpath.moves if m.kind is not MoveKind.RAPID]
        if not planar_moves:
            raise PlanningError("分层开粗：底层刀路没有可用的切削段")

        # -- 2. 深度分层 ------------------------------------------------------
        layer_count = int(ceil(depth / layer))
        z_levels = [-layer * index for index in range(1, layer_count + 1)]
        z_levels[-1] = -depth  # 最后一层精确到总深
        base_passes = len({m.pass_index for m in planar_moves if m.pass_index >= 0}) or 1

        first_xy = planar_moves[0].points[0][:2]
        moves: list[Move] = []
        for index, z in enumerate(z_levels):
            layer_moves: list[Move] = []
            for move in planar_moves:
                points = np.column_stack(
                    (
                        move.points[:, 0],
                        move.points[:, 1],
                        np.full(move.points.shape[0], z, dtype=np.float64),
                    )
                )
                pass_index = move.pass_index
                if pass_index >= 0:
                    pass_index += index * base_passes
                layer_moves.append(
                    Move(
                        move.kind,
                        points,
                        move.feed_mm_per_min,
                        pass_index=pass_index,
                        label=move.label,
                    )
                )

            first = np.array([first_xy[0], first_xy[1], z], dtype=np.float64)
            if index == 0:
                start = np.array(
                    [first_xy[0], first_xy[1], SAFE_HEIGHT_MM], dtype=np.float64
                )
                moves.append(
                    Move(MoveKind.RAPID, np.vstack([start, first]),
                         RAPID_FEED_MM_PER_MIN, label="下刀")
                )
            else:
                previous_last = moves[-1].points[-1]
                moves.append(
                    retract_move(previous_last, first, SAFE_HEIGHT_MM, RAPID_FEED_MM_PER_MIN)
                )
            moves.extend(layer_moves)

        final = moves[-1].points[-1]
        up = np.array([final[0], final[1], SAFE_HEIGHT_MM], dtype=np.float64)
        moves.append(Move(MoveKind.RAPID, np.vstack([final, up]),
                          RAPID_FEED_MM_PER_MIN, label="抬刀"))

        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"分层开粗：{base_label}走刀，共 {layer_count} 层"
                f"（每层 {layer:g} mm，总深 {depth:g} mm），切宽 {stepover:g} mm",
                "每层从安全高度下刀后切削，层间抬刀至安全高度再横移，"
                "适合型腔/槽类零件的粗加工",
            ),
        )
