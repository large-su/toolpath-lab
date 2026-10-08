"""自适应等残留高度刀路。

这是一种适合教学演示的曲面刀路复现：先用球头刀的弓高关系得到目标残留
高度对应的基准步距，再沿相邻刀路方向估计曲面二阶变化，在曲率较大的区域
自动缩小步距。平坦区域使用基准步距，起伏区域使用更密的刀线。

它保留了现有栅格策略的区域裁剪、自由曲面采样和安全抬刀逻辑，因此可以和
栅格、交叉栅格直接比较。该实现是论文方法的工程化简化，不是完整 CAM 中的
精确刀具扫掠碰撞求解器。
"""

from __future__ import annotations

from math import sqrt
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

_ALIGN_TOLERANCE = 0.05


@PLANNERS.register
class AdaptiveScallopPlanner(Planner):
    """根据目标残留高度和局部曲率自适应调整横向步距。"""

    id: ClassVar[str] = "adaptive_scallop"
    label: ClassVar[str] = "自适应等残留高度"
    description: ClassVar[str] = "按目标残留高度与曲面曲率自动调整刀路步距"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec(
                "mode", "走刀模式", K.CHOICE, "zigzag", group="刀路",
                choices=(
                    Choice("zigzag", "往复 Zigzag"),
                    Choice("one_way", "单向 One-way"),
                ),
            ),
            spec(
                "target_scallop_mm", "目标残留高度", K.FLOAT, 0.2,
                minimum=0.02, maximum=2.0, step=0.02, unit="mm", group="刀路",
                help="相邻刀路之间允许的最大理论残留高度，需小于等效刀尖圆弧半径",
            ),
            spec(
                "min_stepover_mm", "最小步距", K.FLOAT, 0.8,
                minimum=0.2, maximum=20.0, step=0.1, unit="mm", group="刀路",
                help="高曲率区域的步距下限",
            ),
            spec(
                "max_stepover_mm", "最大步距", K.FLOAT, 6.0,
                minimum=0.5, maximum=40.0, step=0.5, unit="mm", group="刀路",
                help="平坦区域的步距上限",
            ),
            spec(
                "direction_deg", "走刀方向", K.FLOAT, 0.0,
                minimum=0.0, maximum=180.0, step=5.0, unit="°", group="刀路",
                help="扫描线的行进方向；自适应步距沿其垂直方向计算",
            ),
            spec(
                "feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0,
                minimum=10.0, maximum=10000.0, step=50.0, unit="mm/min",
                group="刀路",
            ),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        mode = str(context.parameters["mode"])
        target = self.require_positive(
            float(context.parameters["target_scallop_mm"]), "目标残留高度"
        )
        minimum = self.require_positive(
            float(context.parameters["min_stepover_mm"]), "最小步距"
        )
        maximum = self.require_positive(
            float(context.parameters["max_stepover_mm"]), "最大步距"
        )
        if minimum > maximum:
            raise PlanningError("最小步距不能大于最大步距")

        equivalent_radius = self._equivalent_radius(context)
        if target >= equivalent_radius:
            raise PlanningError(
                f"目标残留高度 {target:g} mm 必须小于等效刀尖圆弧半径 "
                f"{equivalent_radius:g} mm"
            )
        nominal = self._nominal_stepover(equivalent_radius, target)
        nominal = float(np.clip(nominal, minimum, maximum))

        boundary = context.boundary
        direction = float(context.parameters["direction_deg"])
        u_axis = direction_2d(direction)
        v_axis = np.array([-u_axis[1], u_axis[0]], dtype=np.float64)
        frame = np.column_stack((u_axis, v_axis))
        planar_boundary = boundary @ frame
        offset = context.tool.footprint_radius_mm
        levels, used_steps = self._adaptive_levels(
            context,
            planar_boundary,
            frame,
            v_axis,
            nominal,
            minimum,
            maximum,
            equivalent_radius,
            offset,
        )

        passes: list[tuple[float, float, float, float]] = []
        for level_index, level in enumerate(levels):
            # 凹区域的一层可能有多条独立刀线，着色步距必须与每条实际刀线对齐。
            level_step = used_steps[min(level_index, len(used_steps) - 1)] if used_steps else nominal
            for interval in scanline_intervals(planar_boundary, float(level)):
                start = interval.start + offset
                end = interval.end - offset
                if end - start > 1e-6:
                    passes.append((start, end, float(level), float(level_step)))
        if not passes:
            raise PlanningError(
                "自适应等残留高度没有生成刀路：请检查区域尺寸、刀具直径与步距"
            )

        moves: list[Move] = []
        previous: np.ndarray | None = None
        for index, (start, end, level, _) in enumerate(passes):
            reverse = mode == "zigzag" and index % 2 == 1
            local_points = np.array(
                [[end, level], [start, level]]
                if reverse else [[start, level], [end, level]],
                dtype=np.float64,
            )
            points_xy = local_points @ frame.T
            positions = context.to_positions(points_xy)
            if previous is None:
                moves.append(context.approach_move_down(positions[0]))
            elif mode == "zigzag":
                moves.append(context.link_move(previous, positions[0]))
            else:
                moves.append(context.rapid_between(previous, positions[0]))
            moves.append(
                context.cut_move(
                    points_xy,
                    pass_index=index,
                    label=f"等残留 · 第 {index + 1} 刀",
                )
            )
            previous = positions[-1]

        assert previous is not None
        valid_steps = [step for step in used_steps if step > 1e-9]
        step_min = min(valid_steps) if valid_steps else nominal
        step_max = max(valid_steps) if valid_steps else nominal
        step_profile = [step if step > 1e-9 else nominal for _, _, _, step in passes]
        moves.append(context.retract_move_up(previous))
        return Toolpath(
            moves=tuple(moves),
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(
                context,
                mode,
                target,
                nominal,
                used_steps,
                equivalent_radius,
                len(passes),
            ),
            metadata={
                "adaptive": {
                    "target_scallop_mm": target,
                    "nominal_stepover_mm": nominal,
                    "min_stepover_mm": step_min,
                    "max_stepover_mm": step_max,
                    # 每个值对应一条切削刀线的局部步距；第一刀使用第一段步距。
                    "stepover_profile_mm": step_profile,
                    "pass_count": len(passes),
                },
            },
        )

    @staticmethod
    def _equivalent_radius(context: PlanningContext) -> float:
        """取刀尖圆弧半径；非球头刀用刀具半径作保守近似。"""

        corner_radius = float(context.tool.corner_radius_mm)
        return corner_radius if corner_radius > 1e-9 else float(context.tool.radius_mm)

    @staticmethod
    def _nominal_stepover(radius: float, scallop: float) -> float:
        """由球头刀弓高关系反解平坦区域的理论步距。"""

        value = max(0.0, 2.0 * radius * scallop - scallop * scallop)
        return 2.0 * sqrt(value)

    @classmethod
    def _adaptive_levels(
        cls,
        context: PlanningContext,
        planar_boundary: np.ndarray,
        frame: np.ndarray,
        v_axis: np.ndarray,
        nominal: float,
        minimum: float,
        maximum: float,
        equivalent_radius: float,
        offset: float,
    ) -> tuple[np.ndarray, list[float]]:
        v_start = float(planar_boundary[:, 1].min()) + offset
        v_end = float(planar_boundary[:, 1].max()) - offset
        if v_end - v_start < -1e-6:
            raise PlanningError(
                f"刀具足迹半径 {offset:g} mm 已经超过区域在该方向上的宽度，"
                "请减小刀具直径或扩大区域"
            )

        levels = [v_start]
        used_steps: list[float] = []
        current = v_start
        while v_end - current > 1e-7:
            step = cls._stepover_at(
                context,
                planar_boundary,
                frame,
                v_axis,
                current,
                nominal,
                minimum,
                maximum,
                equivalent_radius,
            )
            next_level = min(v_end, current + step)
            if next_level - current <= 1e-8:
                raise PlanningError("自适应步距计算未能向前推进")
            used_steps.append(float(next_level - current))
            levels.append(next_level)
            current = next_level
        if len(levels) == 1:
            used_steps.append(0.0)
        return np.asarray(levels, dtype=np.float64), used_steps

    @staticmethod
    def _stepover_at(
        context: PlanningContext,
        planar_boundary: np.ndarray,
        frame: np.ndarray,
        v_axis: np.ndarray,
        level: float,
        nominal: float,
        minimum: float,
        maximum: float,
        equivalent_radius: float,
    ) -> float:
        intervals = scanline_intervals(planar_boundary, float(level))
        if not intervals:
            return nominal
        interval = max(intervals, key=lambda item: item.end - item.start)
        delta = max(0.5, min(2.0, nominal * 0.5))
        # 一条刀线沿 u 方向可能跨过多个曲率峰值；取 7 个样本中的最大
        # 二阶变化，避免只在刀线中点采样而漏掉高曲率区域。
        samples = np.linspace(interval.start, interval.end, 7)
        sample_local = np.column_stack((samples, np.full(samples.shape, level)))
        sample_xy = sample_local @ frame.T
        points = np.vstack((sample_xy - v_axis * delta,
                            sample_xy,
                            sample_xy + v_axis * delta))
        heights = context.surface.height_at(points).reshape(3, -1)
        curvature = float(np.max(np.abs(
            heights[2] - 2.0 * heights[1] + heights[0]
        ))) / (delta * delta)
        # 曲率越大，横向弓高累积越快；用平滑因子避免步距突变。
        curvature_factor = 1.0 + min(6.0, 4.0 * equivalent_radius * curvature)
        step = nominal / sqrt(curvature_factor)
        return float(np.clip(step, minimum, maximum))

    @staticmethod
    def _notes(
        context: PlanningContext,
        mode: str,
        target: float,
        nominal: float,
        used_steps: list[float],
        equivalent_radius: float,
        pass_count: int,
    ) -> tuple[str, ...]:
        valid_steps = [step for step in used_steps if step > 1e-9]
        minimum = min(valid_steps) if valid_steps else nominal
        maximum = max(valid_steps) if valid_steps else nominal
        radius_note = "刀尖圆弧半径" if context.tool.corner_radius_mm > 1e-9 else "刀具半径近似"
        return (
            f"目标残留高度 {target:g} mm，共 {pass_count} 刀，"
            f"实际步距 {minimum:g}～{maximum:g} mm",
            f"平坦区域理论步距 {nominal:g} mm，按局部曲率自适应加密",
            f"等效{radius_note} {equivalent_radius:g} mm，采用{('往复' if mode == 'zigzag' else '单向')}走刀",
            "残留高度由球头刀弓高关系估算，曲面区域仍按安全高度抬刀连接",
        )
