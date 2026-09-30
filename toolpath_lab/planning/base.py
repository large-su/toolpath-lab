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
from toolpath_lab.core.parameters import ParameterSet
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.geometry2d import ensure_ccw

#: 快速移动时相对工件上表面抬起的距离（mm）。
SAFE_HEIGHT_MM = 5.0
#: 快速移动的进给速度（mm/min）。
RAPID_FEED_MM_PER_MIN = 5000.0
#: 计算横移安全面时沿路径的采样步长（mm）；区域自带曲面采样步长时用区域的那个。
SAFE_SAMPLE_STEP_MM = 1.0


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """一次规划需要的全部输入，且不依赖任何传输层。"""

    tool: Tool
    region: RegionShape
    parameters: Mapping[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # -- 参数 --------------------------------------------------------------
    @property
    def feed_mm_per_min(self) -> float:
        return float(self.parameters["feed_mm_per_min"])

    # -- 几何 --------------------------------------------------------------
    @property
    def boundary(self) -> NDArray[np.float64]:
        """逆时针的区域轮廓，形状 (N, 2)。"""

        return ensure_ccw(self.region.boundary())

    def to_positions(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """把平面点 (N, 2) 抬成工件坐标下的 (N, 3)：Z 取**加工面**在该点的高度。

        平面区域是 Z = 0，斜面这类分片平面的区域会先在折痕处补点，曲面区域会按采样步长
        加密，所以折线始终贴合加工面——策略不需要知道加工面是平的、斜的还是弯的。
        """

        planar = self.surface_polyline(points_xy)
        return np.column_stack((planar, self.region.height_at(planar)))

    def surface_polyline(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """把折线补成贴合加工面的折线：折角处插点，曲面按采样步长加密。

        平面区域原样返回，所以"一刀两个点"的约定不受影响；分片平面只在折痕处补点就已经
        精确，曲面则沿折线每 ``surface_sample_step_mm`` 取一个点。
        """

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        if planar.shape[0] < 2:
            return planar
        step = self.region.surface_sample_step_mm
        result: list[NDArray[np.float64]] = [planar[0]]
        for start, end in zip(planar, planar[1:]):
            direction = end - start
            length = float(np.linalg.norm(direction))
            if length <= 1e-12:
                continue
            fractions: list[float] = []
            breaks = np.asarray(
                self.region.surface_breaks(start, end), dtype=np.float64
            ).reshape(-1, 2)
            for point in breaks:
                fractions.append(float(np.dot(point - start, direction) / (length * length)))
            if step:
                count = int(np.ceil(length / step))
                fractions.extend(index / count for index in range(1, count))
            for fraction in sorted(value for value in fractions if 1e-9 < value < 1.0 - 1e-9):
                result.append(start + fraction * direction)
            if float(np.linalg.norm(end - result[-1])) > 1e-9:
                result.append(end)
        return np.array(result, dtype=np.float64)

    def warn(self, message: str) -> None:
        """记录一条不致命的提醒，会随响应返回并显示在界面上。"""

        if message not in self.warnings:
            self.warnings.append(message)

    # -- 运动段构造 --------------------------------------------------------
    def cut_move(self, points_xy: NDArray[np.float64], *, pass_index: int, label: str) -> Move:
        return Move(
            MoveKind.CUT,
            self.to_positions(points_xy),
            self.feed_mm_per_min,
            pass_index=pass_index,
            label=label,
        )

    def link_move(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> Move:
        return Move(
            MoveKind.LINK,
            np.vstack([start, end]),
            self.feed_mm_per_min,
            label="刀间连接",
        )

    def rapid_between(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> Move:
        return retract_move(
            start, end, self.safe_height_above(start, end), RAPID_FEED_MM_PER_MIN
        )

    def safe_height_above(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> float:
        """横移用的安全高度：抬到这段路径**经过的加工面最高点**之上 SAFE_HEIGHT_MM。

        只看两个端点是不够的——柱面的拱顶通常落在刀路中间，两端的横移若不抬到拱顶之上
        就会撞进工件。平面区域仍然退化成原来的绝对安全面 Z = SAFE_HEIGHT_MM。
        """

        first = np.asarray(start, dtype=np.float64).reshape(3)
        last = np.asarray(end, dtype=np.float64).reshape(3)
        surface_max = self.region.surface_max_along(first[:2], last[:2])
        if surface_max is None:
            step = self.region.surface_sample_step_mm or SAFE_SAMPLE_STEP_MM
            span = float(np.linalg.norm(last - first))
            count = int(np.clip(np.ceil(span / step), 2, 512))
            fractions = np.linspace(0.0, 1.0, count + 1)
            planar = first[:2] + fractions[:, None] * (last[:2] - first[:2])
            surface_max = float(self.region.height_at(planar).max())
        highest = max(float(first[2]), float(last[2]), float(surface_max))
        return highest + SAFE_HEIGHT_MM

    def approach_move_down(self, point: NDArray[np.float64]) -> Move:
        """从安全高度下刀到该点。"""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = np.array([target[0], target[1], target[2] + SAFE_HEIGHT_MM], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, target]), RAPID_FEED_MM_PER_MIN,
                    label="下刀")

    def retract_move_up(self, point: NDArray[np.float64]) -> Move:
        """从该点抬刀到安全高度。"""

        start = np.asarray(point, dtype=np.float64).reshape(3)
        end = np.array([start[0], start[1], start[2] + SAFE_HEIGHT_MM], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, end]), RAPID_FEED_MM_PER_MIN,
                    label="抬刀")


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
