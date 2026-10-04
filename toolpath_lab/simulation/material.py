"""材料切除仿真：Z-map 高度场毛坯。

刀路是几何，播放是时间，而"加工成什么样"是第三件事：把毛坯离散成一张**高度场**
（Z-map，每个网格单元一个当前顶面高度），让刀具沿刀路扫过、逐点把高于刀底的材料削掉。
每段运动按自己的进给采样，所以仿真结果和播放进度天然对齐。

为什么是 Z-map
--------------
规则区域 + 三轴加工意味着"从上往下看，每列材料只剩一个高度"，于是毛坯可以只用
一张二维标量数组表示：更新便宜（一次只改刀底覆盖的那几十个单元）、传输便宜、
显示便宜（一张网格的顶点高度）。要做倒扣、侧铣或五轴，才需要换成体素或 Dexel。

关键约定
--------
- 加工面是 XY 平面（Z = 0），刀路的切削点在 Z = 0，快移在安全高度；
- 毛坯上表面在 :attr:`HeightField.stock_top_mm` > 0，底面在 :attr:`HeightField.floor_mm` < 0，
  所以刀路真的会切掉东西，而不是在表面上蹭；
- 材料只减不增，因此每一格的 ``removed_mm`` 单调不减，帧序列可以直接做逐帧对比与插值。

刀具底部几何按通用的圆角半径建模（见 core/tool.py 的足迹半径表）：
平底刀 rc = 0 得到平底，球头刀 rc = R 得到球面。当前界面只开放平底刀，
但公式已经对三种刀具成立。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.tool import Tool

#: 高度场的网格数上限（横纵各自），约束响应体积。
MAX_GRID_CELLS = 200
#: 一条刀轨至少要占多少格，否则坑的边缘会退化成楼梯状锯齿。
CELLS_PER_PASS = 6.0
#: 一次仿真默认产生多少帧；帧越多动画越连续，载荷越大。
DEFAULT_FRAME_BUDGET = 24
FRAME_BUDGET_RANGE = (4, 48)
#: 扫掠采样的最小间距：再密也只会重复覆盖同一批网格单元。
MIN_SWEEP_STEP_MM = 0.25
#: 坐标系容差。
_EPS = 1e-9


def stock_parameters() -> ParameterSet:
    """毛坯分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("depth_mm", "毛坯厚度", K.FLOAT, 20.0, minimum=0.5, maximum=200.0,
                 step=0.5, unit="mm", group="毛坯",
                 help="从毛坯上表面往下留多少材料；切削深度本身由刀路的 Z 决定"),
            spec("top_mm", "上表面余量", K.FLOAT, 2.0, minimum=0.0, maximum=20.0,
                 step=0.1, unit="mm", group="毛坯",
                 help="毛坯上表面高出加工面多少——留 0 会看不出刀路把材料削掉了"),
            spec("margin_mm", "侧向余量", K.FLOAT, 2.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="毛坯",
                 help="毛坯在刀路扫掠范围之外再放宽多少，用于显示未加工到的肩部"),
            spec("resolution_mm", "网格精度", K.FLOAT, 1.5, minimum=0.2, maximum=10.0,
                 step=0.1, unit="mm", group="毛坯",
                 help="高度场单元边长；越小越细腻，仿真与传输也越重"),
            spec("frame_budget", "动画帧数", K.INT, DEFAULT_FRAME_BUDGET,
                 minimum=FRAME_BUDGET_RANGE[0], maximum=FRAME_BUDGET_RANGE[1],
                 step=4, unit="帧", group="毛坯",
                 help="仿真保留多少个中间状态；界面在帧之间做线性插值"),
        )
    )


@dataclass(frozen=True, slots=True)
class StockSettings:
    """一份经过校验的毛坯设置。"""

    depth_mm: float = 20.0
    top_mm: float = 2.0
    margin_mm: float = 2.0
    resolution_mm: float = 1.5
    frame_budget: int = DEFAULT_FRAME_BUDGET

    def __post_init__(self) -> None:
        if not np.isfinite(self.depth_mm) or self.depth_mm <= 0.0:
            raise ParameterError("毛坯厚度必须是有限正数")
        if not np.isfinite(self.top_mm) or self.top_mm < 0.0:
            raise ParameterError("毛坯上表面余量必须是非负数")
        if not np.isfinite(self.margin_mm) or self.margin_mm < 0.0:
            raise ParameterError("毛坯侧向余量必须是非负数")
        if not np.isfinite(self.resolution_mm) or self.resolution_mm <= 0.0:
            raise ParameterError("网格精度必须是有限正数")
        if not (FRAME_BUDGET_RANGE[0] <= int(self.frame_budget) <= FRAME_BUDGET_RANGE[1]):
            raise ParameterError(
                f"动画帧数必须在 {FRAME_BUDGET_RANGE[0]} 到 {FRAME_BUDGET_RANGE[1]} 之间"
            )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any] | None) -> "StockSettings":
        """由界面/接口的参数字典构造（缺省项用默认值补齐并校验）。"""

        values = stock_parameters().coerce(params)
        return cls(
            depth_mm=float(values["depth_mm"]),
            top_mm=float(values["top_mm"]),
            margin_mm=float(values["margin_mm"]),
            resolution_mm=float(values["resolution_mm"]),
            frame_budget=int(values["frame_budget"]),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "depth_mm": self.depth_mm,
            "top_mm": self.top_mm,
            "margin_mm": self.margin_mm,
            "resolution_mm": self.resolution_mm,
            "frame_budget": int(self.frame_budget),
        }


@dataclass(frozen=True, slots=True)
class HeightField:
    """一张离散的毛坯高度场。

    坐标系：``x_mm`` / ``y_mm`` 是网格线的工件坐标，``removed_mm`` 的每个元素是该单元
    已经被切掉的深度（0 表示原表面，正值表示凹下去）。
    """

    x_mm: NDArray[np.float64]
    y_mm: NDArray[np.float64]
    removed_mm: NDArray[np.float64]
    stock_top_mm: float
    floor_mm: float

    @property
    def columns(self) -> int:
        return int(self.x_mm.shape[0])

    @property
    def rows(self) -> int:
        return int(self.y_mm.shape[0])

    @property
    def cell_count(self) -> int:
        return self.columns * self.rows

    @property
    def top_mm(self) -> NDArray[np.float64]:
        """当前顶面高度（工件坐标 Z）。"""

        return self.stock_top_mm - self.removed_mm

    @property
    def removed_volume_mm3(self) -> float:
        dx = float(abs(self.x_mm[1] - self.x_mm[0])) if self.columns > 1 else 0.0
        dy = float(abs(self.y_mm[1] - self.y_mm[0])) if self.rows > 1 else 0.0
        return float(self.removed_mm.sum()) * dx * dy

    @property
    def max_cut_depth_mm(self) -> float:
        return float(self.removed_mm.max()) if self.removed_mm.size else 0.0

    @property
    def machined_ratio(self) -> float:
        """已经被切到的单元占比。"""

        if self.removed_mm.size == 0:
            return 0.0
        return float((self.removed_mm > 1e-6).mean())


def _spacing(values: NDArray[np.float64]) -> float:
    return float(values[1] - values[0]) if values.shape[0] > 1 else 0.0


def build_height_field(
    toolpath: Toolpath,
    tool: Tool,
    settings: StockSettings,
) -> HeightField:
    """按刀路的扫掠范围和刀具半径确定毛坯尺寸，并建一张未切削的高度场。"""

    points = np.vstack([move.points for move in toolpath.moves]).reshape(-1, 3)
    if points.shape[0] == 0:
        raise ParameterError("刀路没有点，无法建立毛坯")

    # 毛坯要盖住**刀具真实切削半径**的扫掠范围：平底刀等于半径，球头刀/圆鼻刀虽然
    # 足迹半径小（偏置量小），但刀体仍然会切到半径那么宽，只按足迹半径取会切掉边料。
    reach = float(tool.radius_mm) + float(settings.margin_mm)
    low = points[:, :2].min(axis=0) - reach
    high = points[:, :2].max(axis=0) + reach

    step = _grid_step(high - low, float(settings.resolution_mm), toolpath)
    x_mm = _grid_axis(float(low[0]), float(high[0]), step)
    y_mm = _grid_axis(float(low[1]), float(high[1]), step)

    stock_top = float(settings.top_mm) if settings.top_mm > 0.0 else _default_stock_top(tool)
    floor = round(-float(settings.depth_mm), 4)
    return HeightField(
        x_mm=x_mm,
        y_mm=y_mm,
        removed_mm=np.zeros((y_mm.shape[0], x_mm.shape[0]), dtype=np.float64),
        stock_top_mm=stock_top,
        floor_mm=floor,
    )


def _default_stock_top(tool: Tool) -> float:
    """上表面余量填 0 时，给一个"能看出被削掉"的最小厚度。"""

    return float(max(0.2, min(1.0, tool.radius_mm * 0.25)))


def _adaptive_step(span: NDArray[np.float64], toolpath: Toolpath) -> float:
    """按"一条刀轨要占多少格"反推网格步长。

    高度场是离散的，如果每条刀轨只落在一两格里，坑的边缘就会变成楼梯/锯齿。
    这里用扫掠面积除以切削总长估出相邻刀轨的间距（平面铣里就是切宽），
    再要求每条刀轨至少占 CELLS_PER_PASS 格。估不出来（刀路太短）时返回 0，表示不干预。
    """

    cut_length = float(toolpath.cut_length_mm)
    if cut_length <= _EPS:
        return 0.0
    # 刀心停留的范围 = 扫掠范围（B）减去两侧各一个刀具半径，所以 B = 包围盒边长 + 2R。
    # 用外包矩形估面积会偏大，换算出来的步长偏保守（更细），这里可以接受。
    swept_area = float(np.prod(np.maximum(np.asarray(span, dtype=np.float64), _EPS)))
    pass_spacing = 2.0 * swept_area / cut_length
    return pass_spacing / float(CELLS_PER_PASS)


def _grid_step(
    span: NDArray[np.float64],
    resolution_mm: float,
    toolpath: Toolpath,
) -> float:
    """把请求的精度夹到网格数上限之内，并按需要自动加密。

    - 用户要得更细就用用户的：``resolution_mm`` 是上限，不会被改粗；
    - 用户要得太粗（刀路痕迹会被网格吃掉）就自动加密到 :func:`_adaptive_step`；
    - 无论哪种，都不超过 :data:`MAX_GRID_CELLS` 的单轴格数，保证响应体积可控。
    """

    span = np.maximum(np.asarray(span, dtype=np.float64), _EPS)
    cap = float(span.max()) / float(MAX_GRID_CELLS)
    step = min(float(resolution_mm), _adaptive_step(span, toolpath) or float("inf"))
    return max(step, cap, 1e-3)


def _grid_axis(low: float, high: float, step: float) -> NDArray[np.float64]:
    count = max(int(np.floor((high - low) / step)) + 1, 2)
    return low + step * np.arange(count, dtype=np.float64)


def carve(
    target: HeightField,
    x_mm: NDArray[np.float64] | float,
    y_mm: NDArray[np.float64] | float,
    z_mm: NDArray[np.float64] | float,
    tool: Tool,
) -> None:
    """让刀具扫过一串位置，就地削掉高于刀底的材料。

    约定与 planning 层一致：**刀路的 Z 就是切削点**（刀具最低点）的高度。
    对圆角半径为 rc 的刀具，底面是半径为 R 的圆盘加上半径 rc 的圆角环：离刀心
    ``d <= R - rc`` 的部分是平的，再往外的刀体按圆角圆弧**向上抬起**（所以那一圈
    材料残留得更高）。平底刀 rc = 0，于是退化成"半径 R 的平底圆盘"这一最常见的情况；
    球头刀 rc = R，得到标准球面。
    """

    x = np.atleast_1d(np.asarray(x_mm, dtype=np.float64))
    y = np.atleast_1d(np.asarray(y_mm, dtype=np.float64))
    z = np.atleast_1d(np.asarray(z_mm, dtype=np.float64))
    if x.size == 0 or target.cell_count == 0:
        return

    radius = float(tool.radius_mm)
    corner = max(0.0, float(tool.radius_mm) - float(tool.footprint_radius_mm))

    # 列窗口逐点取（x 在变），行窗口按整段运动的 y 范围外扩一个半径取一次。
    column_low = np.searchsorted(target.x_mm, x - radius, side="left")
    column_high = np.searchsorted(target.x_mm, x + radius, side="right")
    row_low = int(np.searchsorted(target.y_mm, float(y.min()) - radius, side="left"))
    row_high = int(np.searchsorted(target.y_mm, float(y.max()) + radius, side="right"))
    if row_high <= row_low:
        return
    y_window = target.y_mm[row_low:row_high]
    inner = max(radius - corner, 0.0)

    for index in range(x.size):
        first = max(int(column_low[index]), 0)
        last = min(int(column_high[index]), target.columns)
        if last <= first:
            continue
        x_window = target.x_mm[first:last]
        distance = np.hypot(x_window[None, :] - x[index], y_window[:, None] - y[index])
        inside = distance <= radius + _EPS
        if corner <= _EPS:
            # 平底刀：半径以内整片切到刀尖同一个 Z。
            offset = np.zeros_like(distance)
        else:
            # 球头/圆鼻刀：半径以内面按圆角圆弧向上抬起。
            outer = np.clip(distance, inner, radius)
            offset = corner - np.sqrt(np.maximum(corner * corner - (outer - inner) ** 2, 0.0))
        surface = float(z[index]) + offset
        # 半径以外的格子这一刀根本没碰到，用 -1 表示"不改变"，
        # 于是它既不会被切，也不会因为刀体抬起而误切旁边的材料。
        removal = np.where(
            inside,
            np.maximum(target.stock_top_mm - np.maximum(surface, target.floor_mm), 0.0),
            -1.0,
        )
        view = target.removed_mm[row_low:row_high, first:last]
        np.maximum(view, removal, out=view)


def carve_at_distance(
    target: HeightField,
    samples: NDArray[np.float64],
    cumulative: NDArray[np.float64],
    start_mm: float,
    stop_mm: float,
    tool: Tool,
    step_mm: float,
) -> None:
    """只扫掠折线上 ``[start_mm, stop_mm]`` 这一段弧长。

    取这一段的首尾点与落在区间内的采样点，再按步长重采样，保证刀底扫过的
    每一处都被覆盖——帧边界往往落在一段运动中间，不能只扫整段。
    """

    if stop_mm <= start_mm + _EPS:
        return
    ends = np.array([start_mm, stop_mm], dtype=np.float64)
    endpoints = np.column_stack(
        [np.interp(ends, cumulative, samples[:, axis]) for axis in range(3)]
    )
    inside = (cumulative > start_mm + _EPS) & (cumulative < stop_mm - _EPS)
    points = np.vstack([endpoints[:1], samples[inside], endpoints[1:]])
    resampled = resample_points(points, step_mm)
    carve(target, resampled[:, 0], resampled[:, 1], resampled[:, 2], tool)


def resample_points(points: NDArray[np.float64], step_mm: float) -> NDArray[np.float64]:
    """按等弧长把一段折线重采样成不超过 step_mm 间距的点列。"""

    array = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if array.shape[0] < 2:
        return array
    segments = np.linalg.norm(np.diff(array, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(segments)))
    total = float(cumulative[-1])
    if total <= _EPS:
        return array[[0]]
    step = max(float(step_mm), 1e-3)
    count = int(np.ceil(total / step)) + 1
    targets = np.linspace(0.0, total, count)
    return np.column_stack(
        [np.interp(targets, cumulative, array[:, axis]) for axis in range(3)]
    )


@dataclass(frozen=True, slots=True)
class MaterialRemoval:
    """一次材料切除仿真的结果：帧序列 + 统计。"""

    settings: StockSettings
    x_mm: NDArray[np.float64]
    y_mm: NDArray[np.float64]
    times_s: NDArray[np.float64]
    frames_mm: NDArray[np.float64]
    stock_top_mm: float
    floor_mm: float
    duration_s: float
    step_mm: float
    cut_cells: int
    notes: tuple[str, ...] = ()

    @property
    def frame_count(self) -> int:
        return int(self.frames_mm.shape[0])

    @property
    def resolution_mm(self) -> float:
        return max(_spacing(self.x_mm), _spacing(self.y_mm))

    @property
    def columns(self) -> int:
        return int(self.x_mm.shape[0])

    @property
    def rows(self) -> int:
        return int(self.y_mm.shape[0])

    @property
    def final_removed_mm(self) -> NDArray[np.float64]:
        return self.frames_mm[-1]

    def _cell_area_mm2(self) -> float:
        dx = _spacing(self.x_mm) if self.columns > 1 else 0.0
        dy = _spacing(self.y_mm) if self.rows > 1 else 0.0
        return dx * dy

    @property
    def removed_volume_mm3(self) -> float:
        return float(self.final_removed_mm.sum()) * self._cell_area_mm2()

    @property
    def max_cut_depth_mm(self) -> float:
        return float(self.final_removed_mm.max()) if self.final_removed_mm.size else 0.0

    @property
    def machined_ratio(self) -> float:
        if self.final_removed_mm.size == 0:
            return 0.0
        return float((self.final_removed_mm > 1e-6).mean())

    def frame_at(self, time_s: float) -> NDArray[np.float64]:
        """任意时刻的高度场：在相邻两帧之间线性插值。"""

        if self.frame_count == 0:
            return np.zeros((self.rows, self.columns), dtype=np.float64)
        if self.frame_count == 1:
            return self.frames_mm[0]
        query = float(np.clip(time_s, float(self.times_s[0]), float(self.times_s[-1])))
        index = int(np.searchsorted(self.times_s, query, side="right") - 1)
        index = int(np.clip(index, 0, self.frame_count - 1))
        nxt = min(index + 1, self.frame_count - 1)
        t0 = float(self.times_s[index])
        t1 = float(self.times_s[nxt])
        ratio = 0.0 if t1 <= t0 else (query - t0) / (t1 - t0)
        start = self.frames_mm[index]
        return start + ratio * (self.frames_mm[nxt] - start)

    def to_payload(self) -> dict[str, Any]:
        """紧凑的 JSON 形式。

        高度场按**刀尖/顶面在工件坐标里的 Z**量化成整数网格，每格两个字节
        （UTF-16LE 码点），还原公式统一是 ``value * step_mm + offset_mm``，
        offset_mm 就是工件坐标的 0 平面。第 0 帧是未切削的毛坯（量化值是常数，
        不需要任何数据），之后每帧只存相对上一帧发生变化的格子——材料只减不增，
        于是每帧的改动集中在当前刀轨扫过的那条带上，载荷比逐帧全量小得多。
        每个游程是 ``[行号, 起始列, 值串]``：行号必须写进载荷，因为一行的变化
        格子本身可以是断开的，解码端无法从"格数"反推行号。
        """

        step_mm = 0.01
        offset_mm = 0.0

        def quantize(frame: NDArray[np.float64]) -> NDArray[np.int64]:
            index = np.rint((np.clip(frame, 0.0, None) - offset_mm) / step_mm)
            return np.clip(index, 0, 65535).astype(np.int64)

        stock_index = int(quantize(np.array([[self.stock_top_mm]]))[0, 0])
        encoded: list[list[str]] = [[]]
        previous = np.full((self.rows, self.columns), stock_index, dtype=np.int64)
        for frame in self.frames_mm:
            # frames_mm 存的是"已切深度"，换算成绝对顶面高度再量化。
            current = quantize(self.stock_top_mm - frame)
            encoded.append(_encode_cells(np.flatnonzero(current != previous), current))
            previous = current

        floor_vertices = _floor_vertices(self.x_mm, self.y_mm, self.floor_mm)
        return {
            "enabled": True,
            "stock_top_mm": round(self.stock_top_mm, 4),
            "floor_mm": round(self.floor_mm, 4),
            "resolution_mm": round(self.resolution_mm, 4),
            "columns": self.columns,
            "rows": self.rows,
            "x_range_mm": [round(float(self.x_mm[0]), 4), round(float(self.x_mm[-1]), 4)],
            "y_range_mm": [round(float(self.y_mm[0]), 4), round(float(self.y_mm[-1]), 4)],
            "times": [0.0] + [round(float(value), 4) for value in self.times_s],
            "frames": encoded,
            "encoding": {
                "type": "uint16",
                "endian": "little",
                "step_mm": step_mm,
                "offset_mm": round(offset_mm, 4),
                "layout": "runs",
            },
            "floor_vertices": floor_vertices,
            "statistics": {
                "duration_s": round(self.duration_s, 4),
                # frame_count 是载荷里的帧数（含第 0 帧的未切削毛坯），
                # keyframe_count 是仿真实际算出的关键帧数（不含毛坯帧）。
                "frame_count": len(encoded),
                "keyframe_count": self.frame_count,
                "resolution_mm": round(self.resolution_mm, 4),
                "removed_volume_mm3": round(self.removed_volume_mm3, 2),
                "max_cut_depth_mm": round(self.max_cut_depth_mm, 4),
                "machined_ratio": round(self.machined_ratio, 4),
                "cut_cells": int(self.cut_cells),
                "step_mm": round(self.step_mm, 4),
            },
            "notes": list(self.notes),
        }


def _encode_cells(
    changed: NDArray[np.int64], current: NDArray[np.int64]
) -> list[str]:
    """把"发生变化的格子"编码成游程 + UTF-16LE 数值串。

    格式：``[[行内起始列, 值], ...]``。每个游程**严格落在同一行内**，因此解码时
    只需要一条规则：起点列比上一段写到的位置更靠前，就说明进入了新的一行。
    每格两个字节，数值是量化后的整数码点；行内下标小于列数，字符集与 JSON 转义
    都不构成问题。
    """

    if changed.size == 0:
        return []
    columns = int(current.shape[1])
    values = current.reshape(-1)[changed]
    rows = changed // columns
    # 游程在两种地方断开：相邻变化格子不再连续，以及行号变了。行号必须写进载荷：
    # 一行的变化格子本身可以是断开的，解码端无法从"格数"反推行号。
    breaks = np.flatnonzero((np.diff(changed) != 1) | (np.diff(rows) != 0)) + 1
    starts = np.concatenate(([0], breaks))
    stops = np.concatenate((breaks, [changed.size]))
    return [
        [
            int(rows[start]),
            int(changed[start] % columns),
            values[start:stop].astype("<u2").tobytes().decode("latin-1"),
        ]
        for start, stop in zip(starts, stops)
    ]


def _floor_vertices(
    x_mm: NDArray[np.float64], y_mm: NDArray[np.float64], floor_mm: float
) -> list[list[float]]:
    """底面轮廓线（毛坯四边 + 加工区投影），用来说明"现在是多深"。"""

    x0, x1 = float(x_mm[0]), float(x_mm[-1])
    y0, y1 = float(y_mm[0]), float(y_mm[-1])
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [[round(x, 4), round(y, 4), round(floor_mm, 4)] for x, y in corners]


def simulate_material_removal(
    toolpath: Toolpath,
    tool: Tool,
    settings: StockSettings | None = None,
    *,
    frame_budget: int | None = None,
) -> MaterialRemoval:
    """沿刀路做一次材料切除仿真。

    刀具按 ``min(足迹半径 / 4, 1 mm)`` 的间距沿每段运动扫掠，每扫一步就更新一次
    高度场；同时按等时间间隔保留 ``frame_budget`` 帧，供界面做插值动画。
    """

    settings = settings or StockSettings()
    if frame_budget is not None:
        settings = StockSettings(
            depth_mm=settings.depth_mm,
            top_mm=settings.top_mm,
            margin_mm=settings.margin_mm,
            resolution_mm=settings.resolution_mm,
            frame_budget=int(frame_budget),
        )

    stock_field = build_height_field(toolpath, tool, settings)
    duration = float(toolpath.estimated_time_s)
    frames = max(int(settings.frame_budget), 2)
    interval = duration / frames if duration > _EPS else 0.0
    step_mm = max(float(tool.footprint_radius_mm) / 4.0, MIN_SWEEP_STEP_MM)
    step_mm = max(step_mm, settings.resolution_mm / 2.0)

    notes: list[str] = []
    elapsed = 0.0
    recorded_times: list[float] = []
    recorded_frames: list[NDArray[np.float64]] = []
    next_target = interval if interval > _EPS else 0.0

    for move in toolpath.moves:
        samples = resample_points(move.points, step_mm)
        distances = np.linalg.norm(np.diff(samples, axis=0), axis=1)
        cumulative = np.concatenate(([0.0], np.cumsum(distances)))
        total = float(cumulative[-1])
        # 在帧边界处把这段运动切开：先扫到边界、记一帧，再继续扫下一段。
        # 这样每一帧都是"当时真实切出来的形状"，而不是每刀跳一次。
        cursor = 0.0
        while total > _EPS and next_target > _EPS and next_target <= elapsed + total + 1e-9:
            fraction = float(np.clip((next_target - elapsed) / total, 0.0, 1.0))
            boundary = fraction * total
            carve_at_distance(stock_field, samples, cumulative, cursor, boundary, tool, step_mm)
            recorded_times.append(min(next_target, duration))
            recorded_frames.append(stock_field.removed_mm.copy())
            cursor = boundary
            next_target += interval
            if len(recorded_frames) >= frames:
                break
        if cursor < total - _EPS:
            carve_at_distance(stock_field, samples, cumulative, cursor, total, tool, step_mm)
        elapsed += total / move.feed_mm_per_min * 60.0
        if len(recorded_frames) >= frames:
            next_target = float("inf")

    if not recorded_frames:
        # 刀路太短（或时间几乎为零）：至少给一帧，界面才不会空着。
        recorded_times.append(elapsed)
        recorded_frames.append(stock_field.removed_mm.copy())
        notes.append("刀路时长过短，仿真只保留了一帧结果")

    # 最后一帧必须是终态：把与终态时刻重合（或更晚）的那一帧去掉，只留终态。
    while recorded_times and recorded_times[-1] >= elapsed - _EPS:
        recorded_times.pop()
        recorded_frames.pop()

    final = stock_field.removed_mm
    times_array = np.array(recorded_times + [elapsed], dtype=np.float64)
    frames_array = np.stack(recorded_frames + [final]).astype(np.float64)

    cut_cells = int(np.count_nonzero(final > 1e-6))
    if cut_cells == 0:
        notes.append("刀路没有切入毛坯：请提高毛坯上表面余量，或检查刀路的 Z 高度")

    return MaterialRemoval(
        settings=settings,
        x_mm=stock_field.x_mm,
        y_mm=stock_field.y_mm,
        times_s=times_array,
        frames_mm=frames_array,
        stock_top_mm=stock_field.stock_top_mm,
        floor_mm=stock_field.floor_mm,
        duration_s=duration,
        step_mm=step_mm,
        cut_cells=cut_cells,
        notes=tuple(notes),
    )


__all__ = [
    "DEFAULT_FRAME_BUDGET",
    "MAX_GRID_CELLS",
    "HeightField",
    "MaterialRemoval",
    "StockSettings",
    "build_height_field",
    "carve",
    "resample_points",
    "simulate_material_removal",
    "stock_parameters",
]
