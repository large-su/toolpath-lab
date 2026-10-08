"""栅格（平行扫描线）刀路。

两种模式：

- **往复 Zigzag**：奇数刀反向，相邻两刀在端头直接连过去，效率高；
- **单向 One-way**：每刀都朝同一个方向，刀与刀之间抬刀到安全面再回到起点，
  慢一些，但每一刀的切削状态一致（顺铣/逆铣方向固定）。

做法很简单，也是这个基座最值得读的一段代码：

1. 把区域轮廓旋转到"走刀坐标系"：u 沿走刀方向，v 垂直于它；
2. 在 v 方向每隔一个切宽布一条刀线；
3. 每条刀线用扫描线求交，得到它在区域内部的区间（圆形是弦，方形是整条）；
4. 区间两端各内缩一个刀具足迹半径（平底刀是 R，球头刀是 0）；
5. 按模式决定方向与刀间连接，补上下刀和抬刀。

加工面是平面时一刀只有两个点（Z 由加工面决定）；加工面是曲面时，
每条刀线按"离散步长"取点，Z 逐点取自加工面。

**切深（分层粗加工）**：给定毛坯（顶面高度）与切深 ap 之后，先按
`Z = 毛坯顶面 − k·ap` 布若干水平层，每一层只在该层还有材料的地方（加工面低于该层）切削，
最后再沿加工面补一刀精加工。毛坯顶面与加工面之间没有余量时，就退化成原来的单层走刀。
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.core.mathutil import direction_2d
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.base import Planner, PlanningContext
from toolpath_lab.planning.geometry2d import scanline_intervals
from toolpath_lab.planning.registry import PLANNERS

_MODE_LABELS = {"zigzag": "往复", "one_way": "单向"}

#: 末刀余量小于切宽的多少倍时补一刀，保证区域被切满。
_ALIGN_TOLERANCE = 0.05
#: 分层加工的最大层数：切深过小会生成海量运动段，直接报错让用户改参数。
MAX_LAYERS = 200
#: 判断"这一点在本层还有材料"的容差（mm）。
_LAYER_TOLERANCE = 1e-6
#: 求"加工面与层平面交点"的二分次数。
_CROSSING_STEPS = 12


@PLANNERS.register
class RasterPlanner(Planner):
    """平行扫描线，往复或单向；可选按切深分层粗加工。"""

    id: ClassVar[str] = "raster"
    label: ClassVar[str] = "栅格刀路"
    description: ClassVar[str] = (
        "平行扫描线：往复（之字形）或单向（每刀抬刀返回）；选择毛坯后可按切深分层粗加工"
    )
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("mode", "走刀模式", K.CHOICE, "zigzag", group="刀路", choices=(
                Choice("zigzag", "往复 Zigzag"),
                Choice("one_way", "单向 One-way"),
            )),
            spec("stepover_mm", "切宽 ae", K.FLOAT, 6.0, minimum=0.5, maximum=100.0,
                 step=0.5, unit="mm", group="刀路", help="相邻两条刀线的间距"),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=5.0, unit="°", group="刀路", help="扫描线的行进方向；切宽方向与之垂直"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 600.0, minimum=10.0,
                 maximum=10000.0, step=50.0, unit="mm/min", group="刀路"),
            spec("sample_step_mm", "离散步长", K.FLOAT, 1.0, minimum=0.1, maximum=20.0,
                 step=0.1, unit="mm", group="刀路",
                 help="曲面加工时沿每条刀线的取点间距；平面加工面固定取两端点，不受影响"),
            spec("depth_per_pass_mm", "切深 ap", K.FLOAT, 0.0, minimum=0.0, maximum=100.0,
                 step=0.5, unit="mm", group="分层",
                 help="每层切掉的深度；0 = 不分层（沿加工面单层走刀）。"
                      "分层需要在「毛坯」里选模型包容体，它提供毛坯顶面高度"),
            spec("finish_pass", "最后精加工", K.BOOL, True, group="分层",
                 help="分层之后沿加工面再走一刀，把留下的余量切干净"),
        )
    )

    def plan(self, context: PlanningContext) -> Toolpath:
        mode = str(context.parameters["mode"])
        stepover = self.require_positive(
            float(context.parameters["stepover_mm"]), "切宽 stepover_mm"
        )
        step = self.require_positive(
            float(context.parameters["sample_step_mm"]), "离散步长 sample_step_mm"
        )
        depth = float(context.parameters["depth_per_pass_mm"])
        finish = bool(context.parameters["finish_pass"])
        offset = context.tool.footprint_radius_mm

        self._warn_if_stepover_too_large(context, stepover)
        self._describe_surface(context, step)

        frame, layout = self._layout(context, stepover, offset)
        layers = self._layer_levels(context, depth)
        if depth > 0.0:
            self._describe_layers(context, layers, depth, finish)

        sequence = _Sequence(context, mode, step)
        for index, level in enumerate(layers):
            for run, scanline in self._layer_runs(context, layout, frame, step, level):
                sequence.add(run, level=scanline, layer_z=level,
                             label=f"第 {index + 1} 层 Z={level:g}")
        if finish or not layers:
            for index, item in enumerate(layout):
                for run in self._surface_runs(context, item, frame, step):
                    sequence.add(run, level=item[2], layer_z=None, label=f"第 {index + 1} 刀")

        moves = sequence.finish()
        if not moves:  # pragma: no cover - 理论上前面就会报错
            raise PlanningError("没有生成任何刀轨：请检查毛坯余量、区域尺寸与切宽")
        return Toolpath(
            moves=moves,
            planner=self.id,
            planner_label=self.label,
            notes=self._notes(context, mode, stepover, step, sequence.pass_index,
                              layers, depth, finish),
        )

    # -- 刀具路径布局 ------------------------------------------------------
    def _layout(
        self, context: PlanningContext, stepover: float, offset: float
    ) -> tuple[np.ndarray, list[tuple[float, float, float]]]:
        """走刀坐标系与每条刀线（区间）的清单：(起点, 终点, 层高度)。"""

        boundary = context.boundary
        u_axis = direction_2d(float(context.parameters["direction_deg"]))
        v_axis = np.array([-u_axis[1], u_axis[0]], dtype=np.float64)
        frame = np.column_stack((u_axis, v_axis))
        planar = boundary @ frame

        levels = self._pass_levels(planar[:, 1], stepover, offset)
        layout: list[tuple[float, float, float]] = []
        for level in levels:
            for interval in scanline_intervals(planar, float(level)):
                start = interval.start + offset
                end = interval.end - offset
                if end - start <= 1e-6:
                    continue
                layout.append((start, end, float(level)))
        if not layout:
            raise PlanningError(
                "没有生成任何刀轨：请检查区域尺寸、刀具直径与切宽是否匹配"
            )
        return frame, layout

    # -- 分层 --------------------------------------------------------------
    def _layer_levels(self, context: PlanningContext, depth: float) -> list[float]:
        """毛坯顶面往下布层：Z = 顶面 − k·ap，直到最低的加工面之上。"""

        if depth <= 0.0:
            return []
        if not context.stock.is_set:
            raise ParameterError(
                f"切深 {depth:g} mm 需要先选择毛坯：请在「毛坯」里选「模型包容体」，"
                "它提供毛坯顶面高度，切深才能算出分几层"
            )
        top = context.stock.top_mm
        lowest = float(context.surface_extremes_mm[0])
        thickness = top - lowest
        if thickness <= _LAYER_TOLERANCE:
            return []
        count = int(np.floor(thickness / depth + 1e-9))
        if count <= 0:
            return []
        if count > MAX_LAYERS:
            raise ParameterError(
                f"切深 {depth:g} mm 需要切 {count} 层，超过 {MAX_LAYERS} 层上限："
                "请增大切深，或检查毛坯顶面与加工面最低点的高低差"
            )
        return [top - depth * (index + 1) for index in range(count)]

    def _layer_runs(
        self,
        context: PlanningContext,
        layout: list[tuple[float, float, float]],
        frame: np.ndarray,
        step: float,
        layer_z: float,
    ) -> list[tuple[np.ndarray, float]]:
        """本层所有"还有材料"的游程：(工件坐标点列 (N, 3), 该游程所在的扫描线高度)。

        同一条扫描线上的两段之间一定隔着立起来的材料，连接时要抬刀，
        所以游程必须带上扫描线高度（见 _Sequence._can_link）。
        """

        runs: list[tuple[np.ndarray, float]] = []
        for start, end, level in layout:
            head = np.array([start, level], dtype=np.float64)
            tail = np.array([end, level], dtype=np.float64)
            # 平面加工面在整条刀线上高度相同：要么整条切，要么整条不切。
            scan = np.vstack([head, tail]) if context.surface.is_planar \
                else context.sample_line(head, tail, step)
            world = scan @ frame.T
            heights = np.asarray(context.surface.heights(world), dtype=np.float64)
            indices = np.flatnonzero(heights < layer_z - _LAYER_TOLERANCE)
            if indices.size == 0:
                continue
            gaps = np.flatnonzero(np.diff(indices) > 1)
            for group in np.split(indices, gaps + 1):
                points = _run_points(context, world, group, layer_z)
                if points.shape[0] < 2:
                    continue
                runs.append(
                    (np.column_stack((points, np.full(points.shape[0], layer_z))), level)
                )
        return runs

    def _surface_runs(
        self,
        context: PlanningContext,
        item: tuple[float, float, float],
        frame: np.ndarray,
        step: float,
    ) -> list[np.ndarray]:
        """沿加工面的一条刀线：平面两个端点，曲面按步长离散。"""

        start, end, level = item
        head = np.array([start, level], dtype=np.float64)
        tail = np.array([end, level], dtype=np.float64)
        scan = np.vstack([head, tail]) if context.surface.is_planar else context.sample_line(head, tail, step)
        return [context.to_positions(scan @ frame.T)]

    @staticmethod
    def _pass_levels(
        v_values: np.ndarray, stepover: float, offset: float
    ) -> np.ndarray:
        """按切宽在 v 方向布刀，两端各内缩一个刀具足迹半径。"""

        v_start = float(v_values.min()) + offset
        v_end = float(v_values.max()) - offset
        if v_end - v_start < -1e-6:
            raise PlanningError(
                f"刀具足迹半径 {offset:g} mm 已经超过区域在该方向上的宽度，"
                "请减小刀具直径或扩大区域"
            )
        count = int(np.floor((v_end - v_start) / stepover + 1e-9)) + 1
        levels = v_start + np.arange(count, dtype=np.float64) * stepover
        if (v_end - float(levels[-1])) > _ALIGN_TOLERANCE * stepover:
            levels = np.append(levels, v_end)
        return levels

    # -- 提示 --------------------------------------------------------------
    @staticmethod
    def _warn_if_stepover_too_large(context: PlanningContext, stepover: float) -> None:
        if stepover > context.tool.diameter_mm:
            context.warn(
                f"切宽 {stepover:g} mm 大于刀具直径 {context.tool.diameter_mm:g} mm，"
                "两刀之间会留下未切除的残余"
            )

    @staticmethod
    def _describe_surface(context: PlanningContext, step: float) -> None:
        """把加工面的情况写进提醒：曲面要说明离散与"切宽按 XY 投影"。"""

        for message in context.surface.warnings():
            context.warn(message)
        if context.surface.is_planar:
            return
        context.warn(
            f"加工面为{context.surface.label}：刀路按 {step:g} mm 步长离散成三维折线"
            "（三轴联动，刀轴恒为 +Z）；切宽与残留高度按 XY 投影计算，"
            "实际切宽会随表面倾角变化"
        )

    @staticmethod
    def _describe_layers(
        context: PlanningContext, layers: list[float], depth: float, finish: bool
    ) -> None:
        """分层加工的两个诚实提醒：没分到层、以及没做精加工。"""

        top = context.stock.top_mm
        lowest = float(context.surface_extremes_mm[0])
        thickness = max(top - lowest, 0.0)
        if not layers:
            context.warn(
                f"毛坯顶面 Z={top:g} mm 到加工面最低点 Z={lowest:g} mm 的余量只有 "
                f"{thickness:g} mm，不足一个切深 {depth:g} mm，因此不分层走单刀"
            )
        elif not finish and len(layers) > 0:
            context.warn(
                f"未做最后精加工：加工面之上仍留有最多一个切深（{depth:g} mm）的余量，"
                f"最深一层在 Z={min(layers):g} mm"
            )

    @staticmethod
    def _notes(
        context: PlanningContext,
        mode: str,
        stepover: float,
        step: float,
        pass_count: int,
        layers: list[float],
        depth: float,
        finish: bool,
    ) -> tuple[str, ...]:
        direction = float(context.parameters["direction_deg"])
        notes = [
            f"{_MODE_LABELS[mode]}走刀，共 {pass_count} 刀，"
            f"切宽 {stepover:g} mm，走刀方向 {direction:g}°",
            context.surface.note(),
        ]
        if context.stock.is_set:
            notes.append(context.stock.note())
        if layers:
            top = context.stock.top_mm
            lowest = float(context.surface_extremes_mm[0])
            tail = "，最后一刀沿加工面精加工" if finish else "，未做精加工"
            notes.append(
                f"毛坯余量 {top - lowest:g} mm，按切深 {depth:g} mm 分 {len(layers)} 层"
                f"（Z = {top - depth:g} … {min(layers):g} mm），每层只切该层还有材料的地方"
                f"{tail}"
            )
        if context.surface.is_planar:
            notes.append(f"{_inset_note(context.tool)}，安全高度 5 mm、快移 5000 mm/min 为固定值")
        else:
            top = context.safe_z_mm - 5.0
            notes.append(
                f"{_inset_note(context.tool)}；每条刀线按 {step:g} mm 离散，"
                f"安全高度为加工面最高点 Z={top:g} mm 之上 5 mm（固定值），"
                "快移 5000 mm/min 为固定值"
            )
        residual = context.tool.residual_height_mm(stepover)
        if residual > 0.0:
            notes.append(
                f"球头刀以刀尖对刀，切宽 {stepover:g} mm 在平面上留下 "
                f"{residual:.3g} mm 的理论残留高度（R{context.tool.radius_mm:g} 球面的弓高）"
            )
        return tuple(notes)


def _run_points(
    context: PlanningContext,
    world: np.ndarray,
    indices: np.ndarray,
    layer_z: float,
) -> np.ndarray:
    """把一段连续的"还有材料"的采样点整理成刀路点，两端推到与层平面的交点上。

    交点是二分出来的：这样每层刀路的端点正好落在"加工面等于层高"的位置，
    不会因为离散步长而在墙上留下一条凸台。
    """

    first = int(indices[0])
    last = int(indices[-1])
    pieces: list[np.ndarray] = []
    if first > 0:
        pieces.append(_crossing(context, world[first], world[first - 1], layer_z))
    pieces.extend(world[first:last + 1])
    if last + 1 < world.shape[0]:
        pieces.append(_crossing(context, world[last], world[last + 1], layer_z))

    points = np.array(pieces, dtype=np.float64).reshape(-1, 2)
    keep = np.ones(points.shape[0], dtype=bool)
    if points.shape[0] > 1:
        keep[1:] = np.linalg.norm(np.diff(points, axis=0), axis=1) > 1e-9
    return np.ascontiguousarray(points[keep])


def _crossing(
    context: PlanningContext, inside: np.ndarray, outside: np.ndarray, layer_z: float
) -> np.ndarray:
    """二分求加工面与层平面 Z = layer_z 的交点（inside 侧还有材料）。"""

    inner = np.asarray(inside, dtype=np.float64).reshape(2)
    outer = np.asarray(outside, dtype=np.float64).reshape(2)
    for _ in range(_CROSSING_STEPS):
        middle = 0.5 * (inner + outer)
        if _height_at(context, middle) < layer_z:
            inner = middle
        else:
            outer = middle
    return inner


def _height_at(context: PlanningContext, point_xy: np.ndarray) -> float:
    return float(
        np.asarray(context.surface.heights(point_xy.reshape(1, 2)), dtype=np.float64)[0]
    )


class _Sequence:
    """按顺序拼装运动段：负责"怎么走过去"、zigzag 方向、刀轨编号与刀位点。"""

    def __init__(self, context: PlanningContext, mode: str, step: float) -> None:
        self.context = context
        self.mode = mode
        self.step = step
        self.moves: list[Move] = []
        self.previous: np.ndarray | None = None
        self.pass_index = 0
        self.last_level: float | None = None
        self.last_layer: float | None = None
        self._toggle = 0

    def add(
        self, points: np.ndarray, *, level: float, layer_z: float | None, label: str
    ) -> None:
        """切一条游程；layer_z 非空表示这是某个水平层上的一刀。"""

        if points.shape[0] < 2:
            return
        if self.mode == "zigzag" and self._toggle % 2 == 1:
            points = points[::-1]
        self._toggle += 1
        start = points[0]
        self._travel(start, level, layer_z)
        self.moves.append(
            Move(
                MoveKind.CUT,
                points,
                self.context.feed_mm_per_min,
                pass_index=self.pass_index,
                label=label,
            )
        )
        self.pass_index += 1
        self.previous = points[-1]
        self.last_level = level
        self.last_layer = layer_z

    def _travel(self, start: np.ndarray, level: float, layer_z: float | None) -> None:
        if self.previous is None:
            # 第一刀的起点：平面加工面走快移（目标是空气），分层加工走进给（目标在毛坯里）。
            self.moves.append(
                self.context.plunge_to(start)
                if layer_z is not None
                else self.context.approach_move_down(start)
            )
            return
        if self._can_link(start, level, layer_z):
            self.moves.append(self.context.link_move(self.previous, start))
        elif layer_z is None and self.last_layer is None:
            self.moves.append(self.context.rapid_between(self.previous, start))
        else:
            self.moves.append(self.context.rapid_over(self.previous, start))
            self.moves.append(self.context.plunge_to(start))

    def _can_link(self, start: np.ndarray, level: float, layer_z: float | None) -> bool:
        """zigzag 才连刀；分层时还要确认两点之间既没有换层、也没有立着的材料。"""

        if self.mode != "zigzag":
            return False
        if layer_z is None and self.last_layer is None:
            # 不分层的单层走刀：与以前完全一致，刀与刀直接连过去。
            return True
        if layer_z != self.last_layer:
            # 换层（包括从最后一层进入精加工）一律抬刀，不做斜穿。
            return False
        if self.last_level is not None and level == self.last_level:
            # 同一条扫描线上的两段之间一定隔着材料（凸起），必须抬刀。
            return False
        assert self.previous is not None
        line = self.context.sample_line(self.previous[:2], start[:2], self.step)
        heights = np.asarray(self.context.surface.heights(line), dtype=np.float64)
        return bool(np.all(heights < layer_z - _LAYER_TOLERANCE))

    def finish(self) -> tuple[Move, ...]:
        if self.previous is not None:
            self.moves.append(self.context.retract_move_up(self.previous))
        return tuple(self.moves)


def _inset_note(tool: Tool) -> str:
    """说明刀路相对区域轮廓的内缩量。

    平底刀的底面是一个平面，内缩一个半径；球头刀只有刀尖接触加工面，
    足迹是一个点，所以不内缩。
    """

    if tool.footprint_radius_mm > 0.0:
        return f"边界内缩一个刀具足迹半径（本刀 R{tool.footprint_radius_mm:g} mm）"
    return "球头刀在加工面上的足迹是一个点，边界不内缩（0 mm）"
