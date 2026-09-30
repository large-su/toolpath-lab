"""平行行切（direction-parallel / raster）曲面刀路 —— opencamlib 落刀。

做法：在 XY 平面上铺一族平行直线，用落刀求出每条线上各点的**刀心高度**，
再把相邻刀点连成切削段。这是 3 轴曲面加工最常用的粗/半精加工方式。

用到的 opencamlib 语义（实测确认）：

* ``PathDropCutter.setSampling(d)``：沿路径每 ``d`` mm 采一个点；
* 返回的 ``CLPoint`` 的 ``z`` **就是刀心高度**（不是刀尖）：平底刀实测与解析值完全一致；
* **找不到接触时 z 返回 0**（OCL 的约定）。这个值会被当成"切到工件底面"，
  因此本模块把低于模型最低点的点判为无效并丢掉——否则刀路会猛地扎到底。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.stock import Mesh
from toolpath_lab.core.tessellation import TessellatedModel
from toolpath_lab.surfacing.backend import require_ocl
from toolpath_lab.surfacing.mesh_input import make_cutter, mesh_to_stlsurf

logger = logging.getLogger(__name__)

CutMode = Literal["zigzag", "one_way"]


@dataclass(frozen=True, slots=True)
class ParallelRequest:
    """平行行切参数。

    :param stepover_mm: 行距（相邻两条平行线的距离）
    :param direction_deg: 走刀方向（XY 平面内与 +X 的夹角，度）
    :param sampling_mm: 沿路径的采样间距。越小刀路越贴合曲面，也越慢
    :param tool_kind: ``flat`` / ``ball`` / ``bull``
    :param tool_diameter_mm: 刀具直径
    :param tool_length_mm: 刃长
    :param safe_height_mm: 安全高度（快移平面）
    :param feed_mm_per_min: 切削进给
    :param rapid_feed_mm_per_min: 快移速度
    :param cut_mode: ``zigzag`` 往复 / ``one_way`` 单向（单向要抬刀回起点）
    :param stock_allowance_mm: 留量：把整条刀路整体抬高这么多（粗加工留余量）
    :param min_z_mm: 低于它判为无效点。默认 None 时取"模型最低点 − 1mm"
    """

    stepover_mm: float = 2.0
    direction_deg: float = 0.0
    sampling_mm: float = 0.5
    tool_kind: str = "flat"
    tool_diameter_mm: float = 6.0
    tool_length_mm: float = 40.0
    safe_height_mm: float = 10.0
    feed_mm_per_min: float = 800.0
    rapid_feed_mm_per_min: float = 5000.0
    cut_mode: CutMode = "zigzag"
    stock_allowance_mm: float = 0.0
    min_z_mm: float | None = None


@dataclass(slots=True)
class ParallelResult:
    """平行行切结果。"""

    toolpath: Toolpath
    #: 每条扫描线上的有效刀点（已按顺序）
    passes: list[NDArray[np.float64]] = field(default_factory=list)
    #: 被判为"无接触"而丢掉的采样点数
    dropped_points: int = 0
    request: ParallelRequest | None = None

    @property
    def pass_count(self) -> int:
        return len(self.passes)


def parallel_toolpath(source: Mesh | TessellatedModel, request: ParallelRequest, *,
                      bounds: tuple[float, float, float, float] | None = None
                      ) -> ParallelResult:
    """生成平行行切刀路。

    :param source: 三角网格（**必须** z ≥ 0，见 :mod:`mesh_input`）
    :param request: 见 :class:`ParallelRequest`
    :param bounds: 扫描范围 ``(x_min, y_min, x_max, y_max)``；
        不给就用网格的 XY 包围盒，再按刀半径外扩一点，保证边缘也扫到
    """

    require_ocl()
    import opencamlib as ocl

    if request.stepover_mm <= 0:
        raise ValueError("行距必须为正")
    if request.sampling_mm <= 0:
        raise ValueError("采样间距必须为正")

    positions = source.positions
    indices = source.indices
    surf = mesh_to_stlsurf(source)
    cutter = make_cutter(request.tool_kind, request.tool_diameter_mm,
                         request.tool_length_mm)

    if bounds is None:
        radius = request.tool_diameter_mm / 2.0
        bounds = (float(positions[:, 0].min()) - radius, float(positions[:, 1].min()) - radius,
                  float(positions[:, 0].max()) + radius, float(positions[:, 1].max()) + radius)
    x_min, y_min, x_max, y_max = (float(v) for v in bounds)
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("扫描范围为空")

    lines = _raster_lines(bounds, request.stepover_mm, request.direction_deg)
    if not lines:
        raise ValueError("扫描范围太小，连一条线都放不下")

    min_z = request.min_z_mm
    if min_z is None:
        min_z = float(positions[:, 2].min()) - 1.0
    allowance = float(request.stock_allowance_mm)
    start_z = float(positions[:, 2].max()) + request.safe_height_mm

    # 一条线跑一次落刀。逐条跑比"全部塞进一个 Path"慢一点（每条一次 run 的开销），
    # 但换来的是**无需猜测分段边界**——OCL 把所有线的刀点混在一个列表里返回，
    # 靠坐标回跳去猜上一版就是在这里写复杂了，还容易把相邻线认错。
    passes: list[NDArray[np.float64]] = []
    dropped = 0
    for (ax, ay), (bx, by) in lines:
        path = ocl.Path()
        path.append(ocl.Line(ocl.Point(ax, ay, start_z), ocl.Point(bx, by, start_z)))
        drop = ocl.PathDropCutter()
        drop.setSTL(surf)
        drop.setCutter(cutter)
        drop.setPath(path)
        drop.setSampling(float(request.sampling_mm))
        drop.run()

        collected: list[tuple[float, float, float]] = []
        for point in drop.getCLPoints():
            # 判"有没有碰到工件"要用**接触点类型**，不能只看 z：
            # opencamlib 找不到接触时把 z 留在 0，但工件本身也可能正好在 z=0，
            # 只看 z 会把"没碰到"和"碰到了 z=0 的面"混为一谈。
            if int(point.cc().type) == int(ocl.CCType.NONE):
                dropped += 1
                continue
            z = float(point.z)
            if z < min_z:
                dropped += 1
                continue
            collected.append((float(point.x), float(point.y), z + allowance))
        if len(collected) >= 2:
            passes.append(np.asarray(collected, dtype=np.float64))

    if not passes:
        raise ValueError("落刀没有得到任何有效刀点：检查网格是否在扫描范围内")

    moves = _moves_from_passes(passes, request)
    notes = [f"行距 {request.stepover_mm:g}mm，共 {len(passes)} 条扫描线"]
    if dropped:
        notes.append(f"无接触点 {dropped} 个已丢弃")
    toolpath = Toolpath(moves=tuple(moves), planner="parallel",
                        planner_label=f"平行行切 {request.direction_deg:g}°",
                        notes=tuple(notes))
    logger.info("平行行切：%d 条扫描线，%d 个刀点，丢弃无接触点 %d 个，切削 %.1fmm",
                len(passes), toolpath.point_count, dropped, toolpath.cut_length_mm)
    return ParallelResult(toolpath=toolpath, passes=passes, dropped_points=dropped,
                          request=request)


def _raster_lines(bounds: tuple[float, float, float, float], stepover: float,
                  direction_deg: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """在矩形范围内铺一族平行线。

    做法：把矩形绕原点转 ``-direction`` 变成轴对齐，在旋转后的坐标系里按行距切条，
    再把每条线的两端转回去。这样方向角是"走刀方向"，与 NX 的"切削角"一致。
    """

    x_min, y_min, x_max, y_max = bounds
    cx, cy = (x_min + x_max) / 2.0, (y_min + y_max) / 2.0
    half_w, half_h = (x_max - x_min) / 2.0, (y_max - y_min) / 2.0
    # 旋转后的半对角线，保证转回去以后仍然覆盖整个矩形
    reach = float(np.hypot(half_w, half_h))

    theta = math.radians(float(direction_deg))
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    # 旋转坐标系里：u 是走刀方向，v 是行距方向。u 方向取 ±reach 即可覆盖。
    count = int(2.0 * reach / stepover) + 1
    if count <= 0:
        return []
    offsets = (np.arange(count) - (count - 1) / 2.0) * stepover + stepover * 0.5
    lines = []
    for v in offsets:
        if abs(v) > reach:
            continue
        # 旋转坐标系 -> 世界坐标
        ax = cx + (-reach) * cos_t - v * sin_t
        ay = cy + (-reach) * sin_t + v * cos_t
        bx = cx + (+reach) * cos_t - v * sin_t
        by = cy + (+reach) * sin_t + v * cos_t
        lines.append(((ax, ay), (bx, by)))
    return lines


def _moves_from_passes(passes: list[NDArray[np.float64]], request: ParallelRequest
                       ) -> list[Move]:
    """把每条扫描线转成切削段，并补上抬刀/快移。

    往复（zigzag）：相邻两条线用 LINK 直连，方向交替（省一半空行程）。
    单向（one_way）：每条线切完抬到安全高度、快移回起点再下刀（表面质量更一致）。
    """

    moves: list[Move] = []
    safe_z = float(max(pass_.max(axis=0)[2] for pass_ in passes) + request.safe_height_mm)
    rapid = request.rapid_feed_mm_per_min
    feed = request.feed_mm_per_min
    previous_end: NDArray[np.float64] | None = None

    for index, pass_ in enumerate(passes):
        points = pass_[::-1] if (request.cut_mode == "zigzag" and index % 2 == 1) else pass_
        start = np.asarray(points[0], dtype=np.float64)

        if previous_end is None:
            moves.append(retract_move(start, start, safe_z, rapid))
        elif request.cut_mode == "one_way":
            moves.append(retract_move(previous_end, start, safe_z, rapid))
        else:
            moves.append(Move(MoveKind.LINK, np.vstack([previous_end, start]), feed,
                              pass_index=index))

        moves.append(Move(MoveKind.CUT, np.asarray(points, dtype=np.float64), feed,
                          pass_index=index))
        previous_end = np.asarray(points[-1], dtype=np.float64)

    if previous_end is not None:
        moves.append(retract_move(previous_end, previous_end, safe_z, rapid))
    return moves


__all__ = ["ParallelRequest", "ParallelResult", "parallel_toolpath"]
