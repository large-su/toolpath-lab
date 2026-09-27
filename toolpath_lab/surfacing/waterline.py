"""等高铣（Z-level / waterline）曲面刀路。

**为什么不用 opencamlib 的 Waterline / AdaptiveWaterline**

实测（cylindrical 试件，半径 20 的圆柱，球刀 D6，理论刀心半径应当 = 20 + 3 = 23）：

===================  ==================  ==========================
输入                 结果                结论
===================  ==================  ==========================
AdaptiveWaterline    半径 21.5（球刀）    偏小 1.5mm，等于只补了半个刀半径
AdaptiveWaterline    半径 22.998（平底）  **正确**，但夹杂半径到 176 的飞点
Waterline            700+ 个碎片环        完全不可用
===================  ==================  ==========================

也就是说 opencamlib 的等高线只对平底刀勉强可用，而且会混入远离工件的飞点；
球刀直接算错。用它做等高铣会得到"看起来有刀路、实际尺寸不对"的结果，比没有更危险。

**这里改用 CAD/CAM 里真正通行的做法**（NX 的 Z-level 就是这么做的）：

1. 用 **OCP** 在高度 Z 处切一刀 → 得到该层的 2D 轮廓（:func:`brep.slice_at`）；
2. 用 **pyclipper** 把轮廓偏置一个刀具半径（+ 侧面余量）→ 得到该层的**刀心轨迹**
   （:class:`contour2d.ContourClipper`）；
3. 把各层的刀心环按 Z 串起来，加上抬刀/快移。

这两步都是**解析精确**的：切片是 OpenCascade 的布尔截面，偏置是 Clipper 的精确等距线。
比 opencamlib 的采样式等高线既准又稳。局限：等高线只按"轮廓偏置"算，
所以对**垂直侧壁**精确；球刀/圆鼻刀在斜面上的接触点会横向内移，
这里按平底刀处理（本项目首期只支持平底刀），斜面上会留一点余量。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.brep.backend import require_ocp
from toolpath_lab.brep.model import BrepModel
from toolpath_lab.brep.section import SliceOptions, slice_at
from toolpath_lab.contour2d.clipper import ContourClipper
from toolpath_lab.contour2d.polygon import Polygon2D, Region2D
from toolpath_lab.core.path import Move, MoveKind, Toolpath

logger = logging.getLogger(__name__)

Order = Literal["top_down", "bottom_up"]


@dataclass(frozen=True, slots=True)
class WaterlineRequest:
    """等高铣参数。

    :param step_down_mm: 每层下降量（层高）
    :param z_top: 起始高度；None 表示用模型最高点
    :param z_bottom: 终止高度；None 表示用模型最低点
    :param tool_diameter_mm: 刀具直径（等高用平底刀）
    :param side_allowance_mm: 侧面余量，偏置时额外留出
    :param safe_height_mm: 安全高度（相对 z_top）
    :param feed_mm_per_min: 切削进给
    :param rapid_feed_mm_per_min: 快移速度
    :param deflection_mm: 曲线离散弦高容差
    :param order: ``top_down`` 自上而下（常规）/ ``bottom_up``
    :param include_safe_links: 层间是否抬到安全高度快移（False 则直接连）
    """

    step_down_mm: float = 2.0
    z_top: float | None = None
    z_bottom: float | None = None
    tool_diameter_mm: float = 6.0
    side_allowance_mm: float = 0.0
    safe_height_mm: float = 10.0
    feed_mm_per_min: float = 800.0
    rapid_feed_mm_per_min: float = 5000.0
    deflection_mm: float = 0.02
    order: Order = "top_down"
    include_safe_links: bool = True
    #: 每个 Z 层至少要有这么大面积（mm²）才生成刀路，避免在顶面附近刷出一堆碎环
    min_area_mm2: float = 0.5


@dataclass(slots=True)
class WaterlineResult:
    """等高铣结果。"""

    toolpath: Toolpath
    #: 各层的刀心环 (z, 环)
    levels: list[tuple[float, tuple[NDArray[np.float64], ...]]] = field(default_factory=list)
    request: WaterlineRequest | None = None

    @property
    def level_count(self) -> int:
        return len(self.levels)

    @property
    def contour_count(self) -> int:
        return sum(len(loops) for _, loops in self.levels)


def waterline_toolpath(model: BrepModel, request: WaterlineRequest) -> WaterlineResult:
    """生成等高铣刀路。

    :param model: BRep 模型（会用 OCP 在每层切片）
    :param request: 见 :class:`WaterlineRequest`
    """

    require_ocp()
    if request.step_down_mm <= 0:
        raise ValueError("层高必须为正")
    radius = request.tool_diameter_mm / 2.0
    if radius <= 0:
        raise ValueError("刀具直径必须为正")
    offset = radius + request.side_allowance_mm

    x0, y0, z0, x1, y1, z1 = model.bounds()
    z_top = float(z1 if request.z_top is None else min(request.z_top, z1))
    z_bottom = float(z0 if request.z_bottom is None else max(request.z_bottom, z0))
    if z_top - z_bottom < 1e-9:
        raise ValueError("等高铣的高度范围为空")

    clipper = ContourClipper()
    options = SliceOptions(deflection=request.deflection_mm)

    levels = _layer_heights(z_top, z_bottom, request.step_down_mm, request.order)
    slices: list[tuple[float, tuple[NDArray[np.float64], ...]]] = []
    for z in levels:
        clipped = slice_at(model, z, options)
        if not clipped.regions:
            continue
        # **偏置方向是 +（外扩）**，别搞反：这里切出来的是**材料截面** M，
        # 刀心轨迹 = offset(M, +r)。它的外环外扩到 R+r（铣外轮廓的刀心），
        # 而孔环会被缩到 a-r —— 那正是"刀心在空腔里、离壁 r"的位置，也就是型腔内壁的刀心。
        # 一开始写成 -r，结果圆柱试件算出半径 17（= 20-3）而不是 23（= 20+3）。
        # 侧面余量同理加在半径上：外壁多留、内壁也多留。
        core = clipper.offset(clipped.regions[0], offset) if len(clipped.regions) == 1 \
            else _offset_many(clipper, clipped.regions, offset)
        loops = _loops_of(core, request.min_area_mm2, z)
        if loops:
            slices.append((z, loops))

    if not slices:
        raise ValueError(
            f"没有任何一层切出可用轮廓：刀具直径 {request.tool_diameter_mm:g}mm "
            f"可能相对零件太大，或高度范围不对"
        )

    safe_z = z_top + request.safe_height_mm
    moves = _moves_from_levels(slices, request, safe_z)
    toolpath = Toolpath(
        moves=tuple(moves), planner="waterline",
        planner_label=f"等高铣 层高{request.step_down_mm:g}mm",
        notes=(f"{len(slices)} 层，{sum(len(l) for _, l in slices)} 条轮廓",),
    )
    logger.info("等高铣：%d 层 / %d 条轮廓，%d 个刀点，切削 %.1fmm",
                len(slices), toolpath.pass_count, toolpath.point_count,
                toolpath.cut_length_mm)
    return WaterlineResult(toolpath=toolpath, levels=slices, request=request)


# ---------------------------------------------------------------- 内部
def _layer_heights(z_top: float, z_bottom: float, step: float, order: Order) -> list[float]:
    """层高序列。**不切在 z_top / z_bottom 上**：正好切在表面上会让截面退化。"""

    heights: list[float] = []
    margin = max(1e-6, step * 1e-6)
    z = z_top - step
    while z > z_bottom + margin:
        heights.append(z)
        z -= step
    if not heights:
        heights = [(z_top + z_bottom) / 2.0]
    # BUG-006 修：先前 while 在 z ≤ z_bottom + margin 时停止，导致底部 [z_bottom, last]
    # 这一段完全没有刀路；典型 (z_top=20, step=2, z_bottom=0) → 层高 [18..2]，
    # 底部 2mm 永远切不到。补一层紧贴底面（但留 ε 不切在表面）。
    if heights and heights[-1] - z_bottom > 1e-6:
        heights.append(z_bottom + margin)
    if order == "bottom_up":
        heights.reverse()
    return heights


def _offset_many(clipper: ContourClipper, regions: tuple[Region2D, ...], delta: float
                 ) -> tuple[Region2D, ...]:
    collected: list[Region2D] = []
    for region in regions:
        collected.extend(clipper.offset(region, delta))
    return tuple(collected)


def _loops_of(regions: tuple[Region2D, ...], min_area: float, z: float
              ) -> tuple[NDArray[np.float64], ...]:
    """把区域里所有环（外环 + 孔）取出来，转成**首尾闭合**的 3D 折线，并写好层高。

    这里就把 z 写好、把环闭合，而不是留到生成移动段时再补：
    ``WaterlineResult.levels`` 是给上层预览与检查用的，它必须和真正下刀的那条线一模一样。
    """

    loops: list[NDArray[np.float64]] = []
    for region in regions:
        for ring in region.rings():
            if abs(ring.area) < min_area:
                continue
            loops.append(close_loop(_to_3d(ring, z), z))
    return tuple(loops)


def _to_3d(polygon: Polygon2D, z: float) -> NDArray[np.float64]:
    points = np.asarray(polygon.points, dtype=np.float64)
    return np.column_stack((points[:, 0], points[:, 1],
                            np.full(points.shape[0], float(z))))


def close_loop(loop: NDArray[np.float64], z: float | None = None
               ) -> NDArray[np.float64]:
    """把环闭合（首点补到末尾）。G 代码与仿真都按"闭合折线"理解一圈。"""

    points = np.array(loop, dtype=np.float64)   # 复制：调用方传进来的数组不该被就地改
    if z is not None:
        points[:, 2] = float(z)
    if not np.allclose(points[0], points[-1]):
        points = np.vstack([points, points[0]])
    return points


def _moves_from_levels(slices: list[tuple[float, tuple[NDArray[np.float64], ...]]],
                       request: WaterlineRequest, safe_z: float) -> list[Move]:
    """逐层逐环生成刀路。

    每个环单独抬到安全高度、快移过去、下刀，再切一圈 —— 等高铣的侧壁通常不连续，
    直接连过去会撞刀。切完整个环后按 Z 下到下一层。
    """

    moves: list[Move] = []
    rapid = request.rapid_feed_mm_per_min
    feed = request.feed_mm_per_min

    def move_to(points: NDArray[np.float64], feed_rate: float, kind: MoveKind) -> None:
        moves.append(Move(kind, np.asarray(points, dtype=np.float64), feed_rate))

    anchor: NDArray[np.float64] | None = None
    for z, loops in slices:
        for index, loop in enumerate(loops):
            ring = close_loop(loop, z)
            start = ring[0]
            if request.include_safe_links or anchor is None:
                base = anchor if anchor is not None else start
                # 一条折线完成 "抬刀 → 横移 → 下刀"，避免斜向穿刀；
                # BUG-001 修：先前分别发两条 move_to 时，第二条把 start[2] 拍到 safe_z，
                # 但首点又停在切削深度，等同于一条 G0 斜向冲程。
                move_to(
                    np.array(
                        [base,
                         [base[0], base[1], safe_z],
                         [start[0], start[1], safe_z],
                         start],
                        dtype=np.float64,
                    ),
                    rapid, MoveKind.RAPID,
                )
            else:
                move_to(np.vstack([anchor, start]), rapid, MoveKind.RAPID)
            move_to(ring, feed, MoveKind.CUT)
            anchor = ring[-1]
    if anchor is not None:
        move_to(np.vstack([anchor, [anchor[0], anchor[1], safe_z]]), rapid, MoveKind.RAPID)
    return moves


__all__ = ["WaterlineRequest", "WaterlineResult", "close_loop", "waterline_toolpath"]
