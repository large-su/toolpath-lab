"""五轴曲面刀路。

该策略在现有栅格刀路的 XYZ 接触点上增加刀轴方向：先取曲面法向，再沿
走刀方向和横向切向量施加可调前倾/侧倾角。这样可以在不改变三轴区域裁剪
逻辑的前提下生成连续的 5-axis tool-axis 数据，供播放和 A/B 轴 G-code 导出。
"""

from __future__ import annotations

from math import radians, tan
from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import unit
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.crosshatch import _passes_for_direction
from toolpath_lab.planning.registry import PLANNERS


def _surface_axes(
    context: PlanningContext,
    points_xy: np.ndarray,
    *,
    lead_deg: float,
    side_tilt_deg: float,
) -> np.ndarray:
    """计算每个接触点的刀轴，正方向为刀尖指向刀柄。"""

    points = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    normals = context.surface.normal_at(points)
    heights = context.surface.height_at(points)
    tangents = np.empty_like(normals)
    if len(points) == 1:
        tangents[:] = np.array([1.0, 0.0, 0.0])
    else:
        for index in range(len(points)):
            if index == 0:
                delta = points[1] - points[0]
                dz = heights[1] - heights[0]
            elif index == len(points) - 1:
                delta = points[-1] - points[-2]
                dz = heights[-1] - heights[-2]
            else:
                delta = points[index + 1] - points[index - 1]
                dz = heights[index + 1] - heights[index - 1]
            tangent = np.array([delta[0], delta[1], dz], dtype=np.float64)
            tangent = tangent - normals[index] * float(np.dot(tangent, normals[index]))
            tangents[index] = unit(tangent if np.linalg.norm(tangent) > 1e-9 else [1, 0, 0])
    sides = np.cross(normals, tangents)
    sides = sides / np.maximum(np.linalg.norm(sides, axis=1)[:, None], 1e-12)
    lead = tan(radians(lead_deg))
    side_tilt = tan(radians(side_tilt_deg))
    axes = normals + lead * tangents + side_tilt * sides
    return axes / np.maximum(np.linalg.norm(axes, axis=1)[:, None], 1e-12)


@PLANNERS.register
class FiveAxisPlanner(Planner):
    """带前倾/侧倾刀轴姿态的单向栅格五轴策略。"""

    id: ClassVar[str] = "five_axis"
    label: ClassVar[str] = "五轴曲面刀路"
    description: ClassVar[str] = "沿曲面法向并叠加前倾/侧倾角，输出连续刀轴姿态"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="五轴刀路"),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="五轴刀路"),
            spec("lead_deg", "前倾角", K.FLOAT, 12.0, minimum=-45.0, maximum=45.0,
                 step=1.0, unit="°", group="五轴刀路",
                 help="沿走刀方向倾斜刀轴，正值朝走刀方向前倾"),
            spec("side_tilt_deg", "侧倾角", K.FLOAT, 0.0, minimum=-30.0, maximum=30.0,
                 step=1.0, unit="°", group="五轴刀路",
                 help="沿扫描线法向侧倾刀轴"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 500.0,
                 minimum=10.0, maximum=10000.0, step=50.0, unit="mm/min", group="五轴刀路"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(float(context.parameters["stepover_mm"]), "切宽 stepover_mm")
        direction = float(context.parameters["direction_deg"])
        lead_deg = float(context.parameters["lead_deg"])
        side_tilt_deg = float(context.parameters["side_tilt_deg"])
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，两刀之间会留下未切除的残余"
            )

        passes = _passes_for_direction(
            context.boundary,
            direction_deg=direction,
            stepover=stepover,
            offset=context.tool.footprint_radius_mm,
        )
        if not passes:
            raise PlanningError("五轴刀路没有生成有效扫描线，请检查区域和刀具尺寸")

        moves: list[Move] = []
        previous: np.ndarray | None = None
        previous_axis: np.ndarray | None = None
        for index, (start, end, level, frame) in enumerate(passes):
            reverse = index % 2 == 1
            planar = np.array(
                [[end, level], [start, level]] if reverse else [[start, level], [end, level]],
                dtype=np.float64,
            )
            points_xy = planar @ frame.T
            axes = _surface_axes(
                context, points_xy, lead_deg=lead_deg, side_tilt_deg=side_tilt_deg
            )
            positions = context.to_positions(points_xy)
            if previous is None:
                moves.append(context.approach_move_down(positions[0], axes[0]))
            else:
                moves.append(context.link_move(previous, positions[0], previous_axis, axes[0]))
            moves.append(context.cut_move(
                points_xy,
                pass_index=index,
                label=f"五轴第 {index + 1} 刀",
                tool_axes=axes,
            ))
            previous = positions[-1]
            previous_axis = axes[-1]
        assert previous is not None and previous_axis is not None
        moves.append(context.retract_move_up(previous, previous_axis))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"五轴栅格：{len(passes)} 刀，切宽 {stepover:g} mm，走刀方向 {direction:g}°",
                f"刀轴沿曲面法向，前倾 {lead_deg:g}°，侧倾 {side_tilt_deg:g}°",
                "刀轴姿态以 A=方位角、B=倾角形式导出；请在仿真或机床后处理器中校验轴限位",
            ),
        )
