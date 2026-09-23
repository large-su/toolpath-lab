"""交叉栅格刀路。

交叉栅格由两组相互交叉的平行扫描线组成。第一组沿 ``direction_deg``
加工，第二组相对第一组旋转 ``cross_angle_deg`` 后加工。每一组内部采用
往复连接，组间使用安全高度快速移动，避免在已加工区域上拖刀。

该策略复用基座已有的扫描线裁剪、运动段、统计和播放模型，因此可以
脱离界面独立调用，也会自动出现在 ``/api/catalog`` 和参数面板中。
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import direction_2d
from toolpath_lab.core.parameters import ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import scanline_intervals
from toolpath_lab.planning.registry import PLANNERS

_ALIGN_TOLERANCE = 0.05


def _pass_levels(values: np.ndarray, stepover: float, offset: float) -> np.ndarray:
    """在扫描法向上按切宽布置刀线，并补齐末刀。"""

    start = float(values.min()) + offset
    end = float(values.max()) - offset
    if end - start < -1e-6:
        raise PlanningError(
            f"刀具足迹半径 {offset:g} mm 已经超过区域在该方向上的宽度，"
            "请减小刀具直径或扩大区域"
        )
    count = int(np.floor((end - start) / stepover + 1e-9)) + 1
    levels = start + np.arange(count, dtype=np.float64) * stepover
    if end - float(levels[-1]) > _ALIGN_TOLERANCE * stepover:
        levels = np.append(levels, end)
    return levels


def _passes_for_direction(
    boundary: np.ndarray,
    *,
    direction_deg: float,
    stepover: float,
    offset: float,
) -> list[tuple[float, float, float, np.ndarray]]:
    """返回指定方向下的扫描区间，区间坐标位于该方向的局部坐标系。"""

    u_axis = direction_2d(direction_deg)
    v_axis = np.array([-u_axis[1], u_axis[0]], dtype=np.float64)
    frame = np.column_stack((u_axis, v_axis))
    planar = boundary @ frame
    passes: list[tuple[float, float, float, np.ndarray]] = []
    for level in _pass_levels(planar[:, 1], stepover, offset):
        for interval in scanline_intervals(planar, float(level)):
            start = interval.start + offset
            end = interval.end - offset
            if end - start > 1e-6:
                passes.append((start, end, float(level), frame))
    return passes


@PLANNERS.register
class CrosshatchPlanner(Planner):
    """按两个方向交叉加工的平行栅格刀路。"""

    id: ClassVar[str] = "crosshatch"
    label: ClassVar[str] = "交叉栅格刀路"
    description: ClassVar[str] = "按两个方向各加工一遍，提升平面覆盖均匀性"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec(
                "stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5,
                maximum=100.0, step=0.5, unit="mm", group="刀路",
                help="两条相邻扫描线的间距",
            ),
            spec(
                "direction_deg", "第一组方向", K.FLOAT, 0.0, minimum=0.0,
                maximum=180.0, step=5.0, unit="°", group="刀路",
                help="第一组扫描线的行进方向",
            ),
            spec(
                "cross_angle_deg", "交叉角度", K.FLOAT, 90.0, minimum=15.0,
                maximum=165.0, step=5.0, unit="°", group="刀路",
                help="第二组相对第一组的旋转角度",
            ),
            spec(
                "feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0,
                minimum=10.0, maximum=10000.0, step=50.0, unit="mm/min",
                group="刀路",
            ),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        direction = float(context.parameters["direction_deg"])
        cross_angle = float(context.parameters["cross_angle_deg"])
        if not 15.0 <= cross_angle <= 165.0:
            raise PlanningError("交叉角度必须在 15° 到 165° 之间")
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，"
                "两刀之间会留下未切除的残余"
            )

        boundary = context.boundary
        offset = context.tool.footprint_radius_mm
        families = (
            (direction, "A"),
            ((direction + cross_angle) % 180.0, "B"),
        )
        all_passes = [
            (name, _passes_for_direction(
                boundary,
                direction_deg=angle,
                stepover=stepover,
                offset=offset,
            ))
            for angle, name in families
        ]
        if any(not passes for _, passes in all_passes):
            raise PlanningError(
                "交叉栅格没有生成完整刀路：请检查刀具直径、区域尺寸与切宽"
            )

        moves: list[Move] = []
        previous: np.ndarray | None = None
        pass_index = 0
        for family_index, (family_name, passes) in enumerate(all_passes):
            for local_index, (start, end, level, frame) in enumerate(passes):
                reverse = local_index % 2 == 1
                planar_points = np.array(
                    [[end, level], [start, level]]
                    if reverse
                    else [[start, level], [end, level]],
                    dtype=np.float64,
                )
                points_xy = planar_points @ frame.T
                positions = context.to_positions(points_xy)
                if previous is None:
                    moves.append(context.approach_move_down(positions[0]))
                elif local_index == 0:
                    moves.append(context.rapid_between(previous, positions[0]))
                else:
                    moves.append(context.link_move(previous, positions[0]))
                moves.append(
                    context.cut_move(
                        points_xy,
                        pass_index=pass_index,
                        label=f"交叉组 {family_name} · 第 {local_index + 1} 刀",
                    )
                )
                previous = positions[-1]
                pass_index += 1
        assert previous is not None
        moves.append(context.retract_move_up(previous))
        counts = ", ".join(f"{name}组 {len(passes)} 刀" for name, passes in all_passes)
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=(
                f"交叉栅格：{counts}，切宽 {stepover:g} mm",
                f"第一组方向 {direction:g}°，交叉角度 {cross_angle:g}°",
                "两组之间抬刀到安全高度，组内采用往复连接",
            ),
        )
