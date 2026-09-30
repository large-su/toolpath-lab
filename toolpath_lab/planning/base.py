"""刀路策略的共同契约。

一个策略拿到 PlanningContext（刀具 + 区域 + 自己的参数），返回一个 Toolpath。
它不知道 HTTP、JSON 与界面的存在，因此可以脱离服务单独测试、单独调用。

下面这些量在本工程里是**固定常量**而不是参数：安全高度、快移速度、边界处理方式。
如需改为可在界面上调整的参数，见 docs/extending.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    ParameterSpec,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape, polygon_bounds
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.geometry2d import ensure_ccw

#: 快速移动时相对工件上表面抬起的距离（mm）。
SAFE_HEIGHT_MM = 5.0
#: 快速移动的进给速度（mm/min）。
RAPID_FEED_MM_PER_MIN = 5000.0
#: 沿加工面进刀时的引入长度（mm）：斜面上从低处贴着加工面切进来，而不是垂直扎下去。
ENTRY_LEAD_IN_MM = 5.0

#: 与"加工面不是水平面"有关的公共参数，两个内置策略都带上它。
SURFACE_PARAMETERS: tuple[ParameterSpec, ...] = (
    spec("entry", "下刀方式", K.CHOICE, "auto", group="刀路",
         choices=(
             Choice("auto", "自动（平面垂直下刀，斜面沿面切入）"),
             Choice("plunge", "垂直下刀"),
             Choice("slope", "沿斜面进刀"),
         ),
         help="自动：水平面直接扎下去；斜面/起伏面从低处沿加工面切进来，并改成由低往高走刀"),
)

#: 分层粗加工与毛坯的公共参数：两个内置策略都带上它。
LAYER_PARAMETERS: tuple[ParameterSpec, ...] = (
    spec("layer_depth_mm", "每层深度 ap", K.FLOAT, 0.0, minimum=0.0, maximum=100.0,
         step=0.5, unit="mm", group="刀路",
         help="大于 0 时先把毛坯逐层铣掉（每层这么深，一路到加工面），最后仍走一遍沿加工面的精加工；"
              "0 = 不分层"),
    spec("stock_margin_mm", "毛坯顶部余量", K.FLOAT, 2.0, minimum=0.0, maximum=50.0,
         step=0.5, unit="mm", group="刀路",
         help="毛坯顶面比加工面最高点高出多少：分层粗加工从它开始往下，界面里的毛坯也按它画"),
)


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """一次规划需要的全部输入，且不依赖任何传输层。"""

    tool: Tool
    region: RegionShape
    parameters: Mapping[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: 分层粗加工时当前这一层的 Z；None = 直接沿加工面走刀（精加工 / 不分层）。
    level_z: float | None = None

    # -- 参数 --------------------------------------------------------------
    @property
    def feed_mm_per_min(self) -> float:
        return float(self.parameters["feed_mm_per_min"])

    @property
    def layer_depth_mm(self) -> float:
        """每层深度；0 表示不分层。"""

        return max(float(self.parameters.get("layer_depth_mm", 0.0) or 0.0), 0.0)

    @property
    def stock_margin_mm(self) -> float:
        """毛坯顶面比加工面最高点高出的余量。"""

        return max(float(self.parameters.get("stock_margin_mm", 2.0) or 0.0), 0.0)

    def at_level(self, z_mm: float) -> "PlanningContext":
        """换到"第 z 层"的上下文：加工面变成 Z = z 的水平面，刀路范围收窄到该层还有料的地方。

        这样策略的其余逻辑（取 Z、安全面、下刀、连接、统计）全都不用改——分层只是把同一套
        走法在若干个水平面上各跑一遍。
        """

        return replace(self, level_z=float(z_mm))

    # -- 分层 --------------------------------------------------------------
    @property
    def surface_z_range(self) -> tuple[float, float]:
        """加工面的最低 / 最高高度（分层粗加工与毛坯都要用）。

        取轮廓顶点加一层 9 × 9 网格：方形 / 圆形 / 斜坡的极值都在轮廓上，网格是给将来的曲面兜底。
        """

        polygon = self.region.boundary()
        bounds = polygon_bounds(polygon)
        xs = np.linspace(bounds[0][0], bounds[0][1], 9)
        ys = np.linspace(bounds[1][0], bounds[1][1], 9)
        grid = np.array([(x, y) for x in xs for y in ys], dtype=np.float64)
        heights = np.concatenate(
            (self.region.height_at(polygon), self.region.height_at(grid))
        )
        return float(heights.min()), float(heights.max())

    def layer_levels(self) -> list[float]:
        """要按层走一遍的 Z，从高到低。

        两部分合起来：

        - **分层粗削**（`layer_depth_mm > 0`）：从毛坯顶面往下、每层这么深，最下一层仍在加工面之上；
        - **平台那一层**：区域报出来的"平台高度"（斜坡的平顶）。平台之上的毛坯总得有人清，而沿加工面
          的那一遍未必到得了它，所以只要它低于毛坯顶面（也就是上面确实有料）就补一层。

        `layer_depth_mm = 0` 且没有平台时返回空——那就是只走一遍沿加工面的刀路，与以前完全一样。
        """

        if self.level_z is not None:
            return []
        low, high = self.surface_z_range
        top = high + self.stock_margin_mm
        candidates: list[float] = []
        if self.layer_depth_mm > 0.0 and top > low + 1e-9:
            count = max(int(np.ceil((top - low) / self.layer_depth_mm - 1e-9)), 1)
            candidates = [top - index * self.layer_depth_mm for index in range(count)]
        for step in self.region.surface_step_levels():
            if float(step) < top - 1e-9:  # 顶上没料就不用补这一层
                candidates.append(float(step))
        levels: list[float] = []
        for z in sorted(candidates, reverse=True):
            if not any(abs(z - kept) < 1e-6 for kept in levels):
                levels.append(z)
        return levels

    # -- 几何 --------------------------------------------------------------
    @property
    def boundary(self) -> NDArray[np.float64]:
        """逆时针的区域轮廓，形状 (N, 2)，也就是工件在 XY 上的外形。"""

        return ensure_ccw(self.region.boundary())

    @property
    def machining_boundary(self) -> NDArray[np.float64]:
        """刀路的可取范围，形状 (N, 2)。

        默认与轮廓相同；斜坡"只加工斜面段"时收窄到斜面段（分界处那条边留了一个足迹半径的
        余量，内缩之后刀路正好停在折痕上）。**在某一层上**则收窄到"这一层还有料"的范围。
        策略一律内缩它、而不是 region.boundary()。
        """

        footprint = self.tool.footprint_radius_mm
        if self.level_z is not None:
            return ensure_ccw(self.region.machining_boundary_at(self.level_z, footprint))
        return ensure_ccw(self.region.machining_boundary(footprint))

    @property
    def surface_varies(self) -> bool:
        """加工面是否随位置起伏（水平面为 False，斜面、曲面为 True）。

        分层粗加工的一层之内是水平面，所以这里是 False——下刀、连接、走刀方向都按平面处理。
        """

        if self.level_z is not None:
            return False
        bounds = polygon_bounds(self.region.boundary())
        xs = np.linspace(bounds[0][0], bounds[0][1], 9)
        ys = np.linspace(bounds[1][0], bounds[1][1], 9)
        grid = np.array([(x, y) for x in xs for y in ys], dtype=np.float64)
        heights = self.region.height_at(grid)
        return bool(float(heights.max() - heights.min()) > 1e-9)

    @property
    def entry_along_surface(self) -> bool:
        """下刀是否沿加工面切入（参数为"自动"时看加工面是否起伏）。"""

        mode = str(self.parameters.get("entry", "auto"))
        if mode == "plunge":
            return False
        if mode == "slope":
            return True
        return self.surface_varies

    @property
    def links_on_surface(self) -> bool:
        """单向走刀时，刀与刀之间是否沿加工面连接（不抬刀，参数为"自动"时看加工面）。"""

        mode = str(self.parameters.get("linking", "auto"))
        if mode == "retract":
            return False
        if mode == "surface":
            return True
        return self.surface_varies

    def surface_rise(self, direction: NDArray[np.float64]) -> float:
        """沿该方向从区域一头走到另一头，加工面净升高多少（正数 = 上坡）。

        斜面上用它决定走刀方向：由低往高走，下刀那一端才是低处。
        """

        planar = self.region.boundary()
        unit = np.asarray(direction, dtype=np.float64).reshape(2)
        norm = float(np.linalg.norm(unit))
        if norm <= 1e-12:
            return 0.0
        projection = planar @ (unit / norm)
        low = planar[int(np.argmin(projection))]
        high = planar[int(np.argmax(projection))]
        heights = self.region.height_at(np.vstack([low, high]))
        return float(heights[1] - heights[0])

    def to_positions(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """把平面点 (N, 2) 抬成工件坐标下的 (N, 3)：Z 取**加工面**在该点的高度。

        平面区域是 Z = 0，斜面这类分片平面的区域会先在折痕处补点，所以折线始终贴合加工面，
        策略本身不需要知道加工面是平的还是斜的。**分层粗加工的某一层**是一个水平面，Z 就是层高。
        """

        planar = self.surface_polyline(points_xy)
        if self.level_z is not None:
            return np.column_stack(
                (planar, np.full(planar.shape[0], self.level_z, dtype=np.float64))
            )
        return np.column_stack((planar, self.region.height_at(planar)))

    def surface_polyline(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """在加工面的折角处给折线补点；平面区域原样返回（"一刀两个点"因此不变）。"""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        if planar.shape[0] < 2:
            return planar
        result: list[NDArray[np.float64]] = [planar[0]]
        for start, end in zip(planar, planar[1:]):
            breaks = np.asarray(
                self.region.surface_breaks(start, end), dtype=np.float64
            ).reshape(-1, 2)
            for point in breaks:
                if float(np.linalg.norm(point - result[-1])) > 1e-9:
                    result.append(point)
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

    @staticmethod
    def safe_height_above(*points: NDArray[np.float64]) -> float:
        """安全高度：在给定点里最高的那个之上再抬 SAFE_HEIGHT_MM。

        平面区域退化成原来的绝对安全面 Z = SAFE_HEIGHT_MM；斜面这种有高度的加工面则跟着
        工件走，横移时不会一头扎进高处的材料里。
        """

        return max(float(np.asarray(point).reshape(3)[2]) for point in points) + SAFE_HEIGHT_MM

    def approach_move_down(self, point: NDArray[np.float64]) -> Move:
        """从安全高度下刀到该点。"""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = np.array([target[0], target[1], target[2] + SAFE_HEIGHT_MM], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, target]), RAPID_FEED_MM_PER_MIN,
                    label="下刀")

    def entry_moves(
        self,
        first_xy: NDArray[np.float64],
        second_xy: NDArray[np.float64],
        *,
        pass_index: int | None = None,
    ) -> list[Move]:
        """下刀动作：水平面垂直下刀；斜面从低处沿加工面切入。

        沿面切入＝沿刀路的反方向退 ``ENTRY_LEAD_IN_MM``，再沿加工面切进来。引入点通常落在
        刀路范围之外（那里没有材料），所以可以一路放到加工面高度——比垂直扎进斜面干净，
        也不会一上来就满宽切削。走刀方向由策略负责摆成"由低往高"。
        """

        first = np.asarray(first_xy, dtype=np.float64).reshape(2)
        target = self.to_positions(first.reshape(1, 2))[0]
        if not self.entry_along_surface:
            return [self.approach_move_down(target)]
        step = first - np.asarray(second_xy, dtype=np.float64).reshape(2)
        length = float(np.linalg.norm(step))
        if length <= 1e-9:
            return [self.approach_move_down(target)]
        lead_xy = first + step / length * ENTRY_LEAD_IN_MM
        lead = self.to_positions(lead_xy.reshape(1, 2))[0]
        return [
            self.approach_move_down(lead),
            self.cut_move(np.vstack([lead_xy, first]), pass_index=pass_index, label="沿面切入"),
        ]

    def surface_link(
        self,
        start_xy: NDArray[np.float64],
        end_xy: NDArray[np.float64],
        *,
        pass_index: int | None = None,
    ) -> Move:
        """沿加工面连接两点：XY 上直连，Z 跟着加工面（斜面上就是贴着斜面切过去）。"""

        planar = np.vstack([
            np.asarray(start_xy, dtype=np.float64).reshape(2),
            np.asarray(end_xy, dtype=np.float64).reshape(2),
        ])
        return Move(
            MoveKind.LINK,
            self.to_positions(planar),
            self.feed_mm_per_min,
            pass_index=pass_index,
            label="刀间连接（沿面）",
        )

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
