"""示例插件：怎么写一个新策略。

**环切策略已经转正为主程序内置功能**（`toolpath_lab/planning/contour.py`，界面上的
"环切"）。本文件保留下来作为模板，但刻意**不注册**，所以直接复制到 `planning/`
也不会和内置版本撞 id。

要看一份真正在跑的代码，请读 `toolpath_lab/planning/contour.py`；
想看"从零写一个策略要处理哪些事"，就读下面的 `SketchPlanner`——
它比内置版多做了旋向选择和抬刀/连接两种刀间衔接，可以照着改成自己的策略。

偏置几何（`offset_polygon` / `resample_ring` / `collect_rings`）现在正式放在
`toolpath_lab/planning/geometry2d.py`，螺旋铣与环切共用，所以本文件不再自带一份副本。

## 写一个策略要处理的坑

1. Toolpath 至少一段运动、每段至少两个点，否则构造时就抛 ParameterError；
2. 每段必须标明类型（cut / link / rapid）与进给，否则统计、播放、导出都会失真；
3. 几何算不动时要区分"材料用尽"和"算法失败"，后者要 warn() 提醒用户
   （见 `collect_rings` 返回的第二个值）；
4. 参数问题抛 ParameterError（HTTP 400），几何不可行抛 PlanningError（HTTP 422）；
5. 写完在 `toolpath_lab/planning/__init__.py` 里导入一行，否则界面看不到。
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import (
    collect_rings,
    ensure_ccw,
    resample_ring,
    signed_area,
)
from toolpath_lab.planning.registry import PLANNERS


class SketchPlanner(Planner):
    """一个未注册的模板策略：沿轮廓逐圈向内偏置，一圈一刀。

    与内置 `contour` 的差别只有两处：刀间衔接方式可以选、旋向可以选。
    复制这个类、改掉 id / label / parameters，再在 `planning/__init__.py` 里导入，
    就是一个可用的新策略。
    """

    id: ClassVar[str] = "sketch_contour"
    label: ClassVar[str] = "环切(模板)"
    description: ClassVar[str] = "未注册的策略模板：改掉 id 就能启用"

    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=50.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两环的间距"),
            spec("sample_step_mm", "采样步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路"),
            spec("link", "刀间连接", K.CHOICE, "rapid", group="刀路", choices=(
                Choice("rapid", "抬刀返回 Rapid"),
                Choice("link", "直接连接 Link"),
            )),
            spec("direction", "旋向", K.CHOICE, "ccw", group="刀路", choices=(
                Choice("ccw", "逆时针 CCW"),
                Choice("cw", "顺时针 CW"),
            )),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        sample_step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "采样步长 sample_step_mm"
        )
        reverse = str(context.parameters["direction"]) == "cw"
        rapid_between = str(context.parameters["link"]) == "rapid"

        # collect_rings 第二个返回值表示"是不是几何自交导致提前结束"——
        # 是的话中心还有材料没切，必须提醒用户，不能当成切完了。
        rings, exhausted_at_corner = collect_rings(
            context.boundary, context.tool.footprint_radius_mm, stepover
        )
        if not rings:
            # 几何不可行用 PlanningError，接口会返回 422 而不是 400。
            raise PlanningError("环切未生成任何刀轨：刀具足迹半径相对区域尺寸过大")
        if exhausted_at_corner:
            context.warn("中心材料没有被切到：圆角处自交，可减小切宽后重试")

        moves: list[Move] = []
        previous: np.ndarray | None = None
        for index, ring in enumerate(rings):
            sampled = resample_ring(ring, sample_step)
            closed = np.vstack([sampled, sampled[:1]])   # 闭合：首点等于末点
            if reverse:
                # 闭合环反序遍历得到的仍是同一条环，只是绕向相反。
                closed = closed[::-1]
            positions = context.to_positions(closed)

            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            elif rapid_between:
                moves.append(context.rapid_between(previous, positions[0]))
            else:
                moves.append(context.link_move(previous, positions[0]))

            moves.append(
                Move(MoveKind.CUT, positions, context.feed_mm_per_min,
                     pass_index=index, label=f"第 {index + 1} 环")
            )
            previous = positions[-1]
        moves.append(context.retract_move_up(previous))

        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(f"环切模板：共 {len(rings)} 环，切宽 {stepover:g} mm",),
        )


def self_check() -> dict[str, Any]:
    """不注册也能跑一遍，确认模板是好的。

    直接实例化 SketchPlanner 就能用——注册表只影响"界面上能不能看到"。
    """

    from toolpath_lab.core.region import build_region
    from toolpath_lab.core.tool import Tool, ToolKind
    from toolpath_lab.planning.base import PlanningContext as _Ctx

    planner = SketchPlanner()
    tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
    region = build_region("circle", {"diameter_mm": 80.0})
    values = planner.parameters.coerce({})
    toolpath = planner.plan(_Ctx(tool=tool, region=region, parameters=values))
    boundary = ensure_ccw(region.boundary())
    return {
        "id": planner.id,
        "label": planner.label,
        "registered": "sketch_contour" in PLANNERS,
        "parameters": [item.key for item in planner.parameters],
        "region_area_mm2": round(abs(signed_area(boundary)), 1),
        "rings": toolpath.pass_count,
    }