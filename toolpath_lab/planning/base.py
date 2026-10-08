"""刀路策略的共同契约。

一个策略拿到 PlanningContext（刀具 + 区域 + 加工面 + 自己的参数），返回一个 Toolpath。
它不知道 HTTP、JSON 与界面的存在，因此可以脱离服务单独测试、单独调用。

加工面（surface）决定"给定 XY，Z 是多少"。平面加工面下 Z 是常量，一刀两个端点就够；
非平面的加工面下刀路要沿曲面离散，用 `context.sample_line()` 取点，
再用 `context.to_positions()` 抬成三维。

下面这些量在本工程里是**固定常量**而不是参数：安全高度、快移速度、边界处理方式。
如需改为可在界面上调整的参数，见 docs/extending.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, isfinite
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import ParameterSet
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.stock import NoStock, Stock
from toolpath_lab.core.surface import FlatSurface, Surface
from toolpath_lab.core.tool import Tool
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
    surface: Surface = field(default_factory=FlatSurface)
    stock: Stock = field(default_factory=NoStock)
    warnings: list[str] = field(default_factory=list)
    _memo: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
        """把平面点 (N, 2) 抬成工件坐标 (N, 3)，Z 取自加工面。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        heights = np.asarray(self.surface.heights(planar), dtype=np.float64).reshape(-1)
        return np.column_stack((planar, heights))

    def sample_line(
        self, start_xy: NDArray[np.float64], end_xy: NDArray[np.float64], step_mm: float
    ) -> NDArray[np.float64]:
        """把一条直线离散成 (N, 2)：曲面靠这些点跟随高度。"""

        start = np.asarray(start_xy, dtype=np.float64).reshape(2)
        end = np.asarray(end_xy, dtype=np.float64).reshape(2)
        length = float(np.linalg.norm(end - start))
        step = max(float(step_mm), 1e-3)
        count = max(int(ceil(length / step)), 1)
        ratios = np.linspace(0.0, 1.0, count + 1)[:, None]
        return start + ratios * (end - start)

    @property
    def surface_extremes_mm(self) -> tuple[float, float]:
        """加工面在区域内的 (最低, 最高) 高度（记忆化）。"""

        cached = self._memo.get("surface_extremes")
        if cached is None:
            cached = self.surface.sample_extremes(self.boundary)
            self._memo["surface_extremes"] = cached
        return cached

    @property
    def safe_z_mm(self) -> float:
        """快移平面：加工面最高点与毛坯顶面取高者，再抬起一个安全高度。

        平面加工面且没有毛坯时正好是 SAFE_HEIGHT_MM（与旧行为一致）；
        曲面或毛坯下自动跟着抬高，否则快移会撞到工件或毛坯。
        """

        cached = self._memo.get("safe_z_mm")
        if cached is None:
            top = float(self.surface_extremes_mm[1])
            if self.stock.is_set:
                top = max(top, self.stock.top_mm)
            cached = SAFE_HEIGHT_MM + max(top, 0.0)
            self._memo["safe_z_mm"] = cached
        return float(cached)

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

    def level_move(
        self, points_xy: NDArray[np.float64], z_mm: float, *, pass_index: int, label: str
    ) -> Move:
        """在给定高度的水平层上切削（分层粗加工用）。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        heights = np.full(planar.shape[0], float(z_mm), dtype=np.float64)
        return Move(
            MoveKind.CUT,
            np.column_stack((planar, heights)),
            self.feed_mm_per_min,
            pass_index=pass_index,
            label=label,
        )

    def rapid_between(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> Move:
        return retract_move(start, end, self.safe_z_mm, RAPID_FEED_MM_PER_MIN)

    def rapid_over(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> Move:
        """抬刀 → 在安全平面上横移到终点正上方（不下刀）。

        分层粗加工用：横移之后要自己接一段 `plunge_to()`，因为从安全平面垂直下到
        毛坯里是在切材料，不能像空行程那样用快移。
        """

        origin = np.asarray(start, dtype=np.float64).reshape(3)
        target = np.asarray(end, dtype=np.float64).reshape(3)
        safe = self.safe_z_mm
        points = np.array(
            [origin, [origin[0], origin[1], safe], [target[0], target[1], safe]],
            dtype=np.float64,
        )
        return Move(MoveKind.RAPID, points, RAPID_FEED_MM_PER_MIN, label="抬刀-横移")

    def plunge_to(self, point: NDArray[np.float64]) -> Move:
        """从正上方以切削进给垂直下刀到该点（分层加工的入刀）。

        与 approach_move_down 的区别：那一段走的是空气，用快移；
        这一段是在毛坯里下刀，必须用进给。
        """

        target = np.asarray(point, dtype=np.float64).reshape(3)
        origin = np.array([target[0], target[1], self.safe_z_mm], dtype=np.float64)
        return Move(
            MoveKind.CUT,
            np.vstack([origin, target]),
            self.feed_mm_per_min,
            label="分层下刀",
        )

    def approach_move_down(self, point: NDArray[np.float64]) -> Move:
        """从安全高度下刀到该点。"""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = np.array([target[0], target[1], self.safe_z_mm], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, target]), RAPID_FEED_MM_PER_MIN,
                    label="下刀")

    def retract_move_up(self, point: NDArray[np.float64]) -> Move:
        """从该点抬刀到安全高度。"""

        start = np.asarray(point, dtype=np.float64).reshape(3)
        end = np.array([start[0], start[1], self.safe_z_mm], dtype=np.float64)
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
