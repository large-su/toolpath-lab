"""曲面精加工（等残余高度）。

平面精加工里切宽是直接给的；到了曲面精加工，工艺上更常用的说法是"残留高度"——
相邻两条刀线之间没切干净的那层料有多高。两者由刀尖圆弧半径 ρ 联系在一起：

    h = ρ − sqrt(ρ² − (ae/2)²)      ⟺      ae = 2·sqrt(2ρh − h²)

所以这里把用户参数从"切宽"换成"残留高度"，切宽由刀具几何反算，再由"最大切宽"
兜底。球头刀 ρ = R、圆鼻刀 ρ = Rc；平底刀没有圆弧，反算不出有限切宽，此时退回
最大切宽并给出提醒。

策略本身不重新实现走刀：它把反算出来的切宽交给 RasterPlanner._plan_raster()，
于是两种策略的刀轨形态、安全高度、统计口径完全一致。
"""

from __future__ import annotations

from math import sqrt
from typing import ClassVar

from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.tool import ToolKind
from toolpath_lab.planning.base import PlanningContext
from toolpath_lab.planning.raster import RasterPlanner
from toolpath_lab.planning.registry import PLANNERS


def scallop_stepover_mm(cutter_radius_mm: float, scallop_mm: float) -> float:
    """由刀尖圆弧半径与允许残留高度反算切宽；反算不出（平底刀）时返回 0。"""

    radius = max(float(cutter_radius_mm), 0.0)
    height = max(float(scallop_mm), 0.0)
    inner = 2.0 * radius * height - height * height
    if inner <= 0.0:
        return 0.0
    return 2.0 * sqrt(inner)


@PLANNERS.register
class SurfaceFinishPlanner(RasterPlanner):
    """按残留高度控制切宽的曲面平行精加工。"""

    id: ClassVar[str] = "surface_finish"
    label: ClassVar[str] = "曲面精加工"
    description: ClassVar[str] = (
        "由残留高度反算切宽的平行精加工：球头刀/圆鼻刀贴着曲面走刀，适合最后一道工序"
    )
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("scallop_mm", "残留高度 h", K.FLOAT, 0.02, minimum=0.001, maximum=1.0,
                 step=0.001, unit="mm", group="精加工",
                 help="相邻两刀之间允许残留的高度，越小越密；切宽由它与刀尖圆弧半径反算"),
            spec("max_stepover_mm", "最大切宽", K.FLOAT, 3.0, minimum=0.1, maximum=50.0,
                 step=0.1, unit="mm", group="精加工",
                 help="切宽上限：防止残留高度设得很小时刀路过密"),
            spec("mode", "走刀模式", K.CHOICE, "zigzag", group="刀路", choices=(
                Choice("zigzag", "往复 Zigzag"),
                Choice("one_way", "单向 One-way"),
            )),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="刀路", help="扫描线的行进方向；切宽方向与之垂直"),
            spec("sample_step_mm", "曲面采样步长", K.FLOAT, 0.5, minimum=0.2,
                 maximum=20.0, step=0.2, unit="mm", group="刀路",
                 help="沿刀线每隔这么长取一个点贴到曲面上；平面只用两个端点"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        scallop = self.require_positive(
            float(context.parameters["scallop_mm"]), "残留高度 scallop_mm"
        )
        cap = self.require_positive(
            float(context.parameters["max_stepover_mm"]), "最大切宽 max_stepover_mm"
        )
        return self._plan_raster(
            context,
            mode=str(context.parameters["mode"]),
            stepover=self._stepover_mm(context, scallop, cap),
            direction_deg=float(context.parameters["direction_deg"]),
            sample_step=float(context.parameters.get("sample_step_mm") or 0.0),
        )

    @staticmethod
    def _stepover_mm(context: PlanningContext, scallop: float, cap: float) -> float:
        """残留高度 → 切宽，并保证结果永远是一个可以走刀的正数。"""

        tool = context.tool
        if tool.kind is ToolKind.FLAT:
            context.warn(
                f"平底刀没有刀尖圆弧，算不出 {scallop:g} mm 残留高度对应的切宽，"
                f"已按最大切宽 {cap:g} mm 走刀；想按残留高度控制切宽请改用球头刀或圆鼻刀"
            )
            return cap
        derived = scallop_stepover_mm(tool.corner_radius_mm, scallop)
        if derived <= 0.0:
            context.warn(
                f"残留高度 {scallop:g} mm 相对刀尖圆弧半径 "
                f"{tool.corner_radius_mm:g} mm 太大，切宽已按上限 {cap:g} mm 走刀"
            )
            return cap
        if derived > cap + 1e-9:
            context.warn(
                f"按残留高度 {scallop:g} mm 反算的切宽 {derived:.3f} mm 超过上限 "
                f"{cap:g} mm，已按上限走刀（此时实际残留高度小于设定值）"
            )
            return cap
        return max(derived, 1e-3)

    def _notes(
        self, context: PlanningContext, mode: str, stepover: float, pass_count: int
    ) -> tuple[str, ...]:
        scallop = float(context.parameters["scallop_mm"])
        notes = list(super()._notes(context, mode, stepover, pass_count))
        notes.append(
            f"切宽由残留高度 {scallop:g} mm 与刀尖圆弧半径 "
            f"{context.tool.corner_radius_mm:g} mm 反算为 {stepover:.3f} mm"
        )
        return tuple(notes)
