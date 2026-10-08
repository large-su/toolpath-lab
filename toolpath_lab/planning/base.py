"""刀路策略的共同契约。

一个策略拿到 PlanningContext（刀具 + 区域 + 自己的参数），返回一个 Toolpath。
它不知道 HTTP、JSON 与界面的存在，因此可以脱离服务单独测试、单独调用。

下面这些量在本工程里是**固定常量**而不是参数：安全高度、快移速度、边界处理方式。
如需改为可在界面上调整的参数，见 docs/extending.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.mathutil import unit
from toolpath_lab.core.parameters import ParameterSet
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.core.surface import FlatSurface, SurfaceShape
from toolpath_lab.planning.geometry2d import ensure_ccw

#: 快速移动时相对工件上表面抬起的距离（mm）。
SAFE_HEIGHT_MM = 5.0
#: 快速移动的进给速度（mm/min）。
RAPID_FEED_MM_PER_MIN = 5000.0


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """一次规划需要的全部输入，且不依赖任何传输层。"""

    tool: Tool
    region: RegionShape
    parameters: Mapping[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    surface: SurfaceShape = field(default_factory=FlatSurface)

    # -- 参数 --------------------------------------------------------------
    @property
    def feed_mm_per_min(self) -> float:
        return float(self.parameters["feed_mm_per_min"])

    @property
    def safe_z_mm(self) -> float:
        """曲面最高点以上的安全快移高度。"""

        return self.surface.height_bounds()[1] + SAFE_HEIGHT_MM

    # -- 几何 --------------------------------------------------------------
    @property
    def boundary(self) -> NDArray[np.float64]:
        """逆时针的区域轮廓，形状 (N, 2)。"""

        return ensure_ccw(self.region.boundary())

    def to_positions(
        self,
        points_xy: NDArray[np.float64],
        *,
        tool_axes: NDArray[np.float64] | None = None,
        compensate_tool: bool = False,
    ) -> NDArray[np.float64]:
        """把平面点 (N, 2) 映射到加工曲面上的 (N, 3)。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        heights = self.surface.height_at(planar)
        positions = np.column_stack((planar, heights))
        if compensate_tool and tool_axes is not None:
            axes = np.asarray(tool_axes, dtype=np.float64).reshape(-1, 3)
            if axes.shape[0] != planar.shape[0]:
                raise PlanningError("刀轴姿态数量必须与补偿点数量一致")
            normals = self.surface.normal_at(planar)
            clearance = np.array(
                [self.tool.orientation_clearance_mm(axis, normal)
                 for axis, normal in zip(axes, normals)],
                dtype=np.float64,
            )
            positions = positions + normals * clearance[:, None]
        return positions

    def sample_cut_points(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """按曲面的建议采样间距加密一条平面扫描线。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        if isinstance(self.surface, FlatSurface) or len(planar) < 2:
            return planar
        spacing = max(float(self.surface.sampling_spacing_mm), 0.25)
        samples = [planar[0]]
        for start, end in zip(planar[:-1], planar[1:]):
            count = max(1, int(np.ceil(np.linalg.norm(end - start) / spacing)))
            samples.extend(
                start + (end - start) * (index / count)
                for index in range(1, count + 1)
            )
        return np.asarray(samples, dtype=np.float64)

    def warn(self, message: str) -> None:
        """记录一条不致命的提醒，会随响应返回并显示在界面上。"""

        if message not in self.warnings:
            self.warnings.append(message)

    # -- 运动段构造 --------------------------------------------------------
    def cut_move(
        self,
        points_xy: NDArray[np.float64],
        *,
        pass_index: int,
        label: str,
        tool_axes: NDArray[np.float64] | None = None,
        compensate_tool: bool = False,
        sampled: bool = False,
    ) -> Move:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        axes = None if tool_axes is None else np.asarray(tool_axes, dtype=np.float64).reshape(-1, 3)
        if axes is not None and axes.shape[0] != planar.shape[0]:
            raise PlanningError("刀轴姿态数量必须与切削点数量一致")
        if not isinstance(self.surface, FlatSurface) and not sampled:
            # 平面策略原本每刀只有两个端点；曲面加工需沿线加密采样。
            sampled_planar = self.sample_cut_points(planar)
            sampled_axes = None if axes is None else [axes[0]]
            for segment, (start, end) in enumerate(zip(planar[:-1], planar[1:])):
                count = max(1, int(np.ceil(np.linalg.norm(end - start) /
                                           max(float(self.surface.sampling_spacing_mm), 0.25))))
                if sampled_axes is not None:
                    sampled_axes.extend(
                        axes[segment] + (axes[segment + 1] - axes[segment]) * (index / count)
                        for index in range(1, count + 1)
                    )
            planar = sampled_planar
            if sampled_axes is not None:
                axes = np.asarray(sampled_axes, dtype=np.float64)
        elif axes is not None:
            axes = np.asarray([unit(axis) for axis in axes], dtype=np.float64)
        return Move(
            MoveKind.CUT,
            self.to_positions(planar, tool_axes=axes, compensate_tool=compensate_tool),
            self.feed_mm_per_min,
            pass_index=pass_index,
            label=label,
            tool_axes=axes,
        )

    def link_move(
        self,
        start: NDArray[np.float64],
        end: NDArray[np.float64],
        start_tool_axis: NDArray[np.float64] | None = None,
        end_tool_axis: NDArray[np.float64] | None = None,
    ) -> Move:
        if not isinstance(self.surface, FlatSurface):
            # 曲面上的直线连接可能穿入中间凸起，改从全局安全高度跨越。
            return self.rapid_between(start, end, start_tool_axis, end_tool_axis)
        axes = None
        if start_tool_axis is not None or end_tool_axis is not None:
            first = unit(start_tool_axis if start_tool_axis is not None else end_tool_axis)
            last = unit(end_tool_axis if end_tool_axis is not None else first)
            axes = np.vstack((first, last))
        return Move(
            MoveKind.LINK,
            np.vstack([start, end]),
            self.feed_mm_per_min,
            label="刀间连接",
            tool_axes=axes,
        )

    def rapid_between(
        self,
        start: NDArray[np.float64],
        end: NDArray[np.float64],
        start_tool_axis: NDArray[np.float64] | None = None,
        end_tool_axis: NDArray[np.float64] | None = None,
    ) -> Move:
        return retract_move(
            start, end, self.safe_z_mm, RAPID_FEED_MM_PER_MIN,
            start_tool_axis, end_tool_axis,
        )

    def approach_move_down(
        self, point: NDArray[np.float64], tool_axis: NDArray[np.float64] | None = None
    ) -> Move:
        """从安全高度下刀到该点。"""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = np.array([target[0], target[1], self.safe_z_mm], dtype=np.float64)
        axis = unit(tool_axis) if tool_axis is not None else None
        axes = None if axis is None else np.vstack((axis, axis))
        return Move(MoveKind.RAPID, np.vstack([start, target]), RAPID_FEED_MM_PER_MIN,
                    label="下刀", tool_axes=axes)

    def retract_move_up(
        self, point: NDArray[np.float64], tool_axis: NDArray[np.float64] | None = None
    ) -> Move:
        """从该点抬刀到安全高度。"""

        start = np.asarray(point, dtype=np.float64).reshape(3)
        end = np.array([start[0], start[1], self.safe_z_mm], dtype=np.float64)
        axis = unit(tool_axis) if tool_axis is not None else None
        axes = None if axis is None else np.vstack((axis, axis))
        return Move(MoveKind.RAPID, np.vstack([start, end]), RAPID_FEED_MM_PER_MIN,
                    label="抬刀", tool_axes=axes)


class Planner:
    """所有刀路策略的基类。

    子类声明 id（接口里的标识）、label（界面上的名字）、description，
    以及一份 parameters 参数声明，然后实现 plan()。
    """

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()

    def plan(self, context: PlanningContext) -> Toolpath:
        raise NotImplementedError

    @staticmethod
    def require_positive(value: float, name: str) -> float:
        if not isfinite(value) or value <= 0.0:
            raise PlanningError(f"{name} 必须是有限正数（收到 {value!r}）")
        return value
