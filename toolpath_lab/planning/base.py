"""刀路策略的共同契约。

一个策略拿到 PlanningContext（刀具 + 区域 + 自己的参数），返回一个 Toolpath。
它不知道 HTTP、JSON 与界面的存在，因此可以脱离服务单独测试、单独调用。

"抬刀高度"与"快移速度"是每个策略都要用的动作参数，但具体数值属于调用方的选择，
因此在这里声明成一份共用的 MOTION_PARAMETERS，由策略并进自己的 ParameterSet；
策略没有声明时，PlanningContext 退回下面的默认值（第三方插件因此不会被这个约定绊住）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.geometry2d import ensure_ccw

#: 快速移动时相对工件上表面抬起的距离（mm）的默认值。
SAFE_HEIGHT_MM = 5.0
#: 快速移动的进给速度（mm/min）的默认值。
RAPID_FEED_MM_PER_MIN = 5000.0

#: 所有策略共用的动作参数：策略把它并进自己的 ParameterSet 即可在界面上调。
MOTION_PARAMETERS: ParameterSet = ParameterSet(
    (
        spec("safe_height_mm", "安全高度", K.FLOAT, SAFE_HEIGHT_MM, minimum=0.0,
             maximum=200.0, step=0.5, unit="mm", group="刀路",
             help="快移时抬到工件上表面（Z = 0）之上的高度；0 表示不抬刀"),
        spec("rapid_feed_mm_per_min", "快移速度", K.FLOAT, RAPID_FEED_MM_PER_MIN,
             minimum=100.0, maximum=50000.0, step=100.0, unit="mm/min", group="刀路",
             help="抬刀 / 横移 / 下刀的进给速度，会计入预计工时"),
    )
)


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

    @property
    def safe_height_mm(self) -> float:
        """抬刀高度；策略声明了 safe_height_mm 就用它的值，否则用默认值。"""

        return float(self.parameters.get("safe_height_mm", SAFE_HEIGHT_MM))

    @property
    def rapid_feed_mm_per_min(self) -> float:
        """快移进给；策略声明了 rapid_feed_mm_per_min 就用它的值，否则用默认值。"""

        return float(self.parameters.get("rapid_feed_mm_per_min", RAPID_FEED_MM_PER_MIN))

    # -- 几何 --------------------------------------------------------------
    @property
    def boundary(self) -> NDArray[np.float64]:
        """逆时针的区域轮廓，形状 (N, 2)。"""

        return ensure_ccw(self.region.boundary())

    def to_positions(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """把平面点 (N, 2) 抬成工件坐标下的 (N, 3)（加工面为 Z = 0）。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.column_stack((planar, np.zeros(planar.shape[0], dtype=np.float64)))

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
        return retract_move(start, end, self.safe_height_mm, self.rapid_feed_mm_per_min)

    def approach_move_down(self, point: NDArray[np.float64]) -> Move:
        """从安全高度下刀到该点。"""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = np.array([target[0], target[1], self.safe_height_mm], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, target]), self.rapid_feed_mm_per_min,
                    label="下刀")

    def retract_move_up(self, point: NDArray[np.float64]) -> Move:
        """从该点抬刀到安全高度。"""

        start = np.asarray(point, dtype=np.float64).reshape(3)
        end = np.array([start[0], start[1], self.safe_height_mm], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, end]), self.rapid_feed_mm_per_min,
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
