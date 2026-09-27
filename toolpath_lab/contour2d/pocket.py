"""型腔环切（contour-parallel）刀路：用偏置把加工区一圈圈缩成环。

**只生成 2D 刀轨**（XY 平面上的环 + 连接），Z 分层与进给由上层负责。
这一步的产物是"每个深度层的环"，上层把各层串起来就是完整的型腔铣刀路。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Literal

import numpy as np

from toolpath_lab.contour2d.clipper import ContourClipper
from toolpath_lab.contour2d.polygon import Polygon2D, Region2D

logger = logging.getLogger(__name__)

#: 小于这个面积的残料就认为切完了（mm²）。
DEFAULT_MIN_AREA_MM2 = 0.05
#: 相邻环的连接方式：直接连（G1）还是抬刀（G0）。二维层里只标出来，由上层决定 Z。
LinkMode = Literal["straight", "nearest"]


@dataclass(frozen=True, slots=True)
class RingPass:
    """一圈切削环。"""

    polygon: Polygon2D
    level: int
    #: 相对"刀心可达区"又内缩了多少（0 = 贴轮廓的最外圈）
    inset_mm: float

    @property
    def is_closed(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PocketResult:
    """一次环切规划的结果。"""

    passes: tuple[RingPass, ...]
    #: 刀心可达区（轮廓内缩一个刀半径）；为空表示刀具放不进去
    core: tuple[Region2D, ...] = ()
    #: 步距覆盖不到的中心残料
    leftover: tuple[Region2D, ...] = ()
    stepover_mm: float = 0.0
    tool_radius_mm: float = 0.0
    #: 每个环相对上一环的连接段（用于估算空行程）
    links: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = ()

    @property
    def cut_length_mm(self) -> float:
        return float(sum(item.polygon.length for item in self.passes))

    @property
    def link_length_mm(self) -> float:
        return float(sum(
            float(np.hypot(b[0] - a[0], b[1] - a[1])) for a, b in self.links
        ))

    @property
    def leftover_area_mm2(self) -> float:
        return float(sum(item.area for item in self.leftover))


def ring_passes(region: Region2D, tool_radius: float, stepover_mm: float, *,
                clipper: ContourClipper | None = None,
                order: Literal["outside_in", "inside_out"] = "outside_in",
                min_area_mm2: float = DEFAULT_MIN_AREA_MM2,
                max_rings: int = 4096) -> PocketResult:
    """生成环切刀路。

    算法（就是对"刀心可达区"反复内缩）：

    1. ``core = 区域内缩一个刀半径`` —— 刀心能到达的范围，这一步同时完成**刀具半径补偿**；
    2. 从 ``core`` 开始，每内缩一个 ``stepover`` 得到下一圈，直到区域消失或小于 ``min_area``；
    3. 最后剩下的那块就是步距覆盖不到的中心残料（回形腔里判断"切干净了没有"就看它）。

    :param region: 加工区域（外环 + 孔）
    :param tool_radius: 刀具半径 mm
    :param stepover_mm: 步距（相邻两圈的距离）mm，必须 > 0
    :param order: 从外往里（``outside_in``，常规做法）还是从里往外
    :param min_area_mm2: 残料小于它就停机
    :returns: :class:`PocketResult`
    """

    if tool_radius <= 0:
        raise ValueError("刀具半径必须为正")
    if stepover_mm <= 0:
        raise ValueError("步距必须为正")
    clipper = clipper or ContourClipper()

    core = clipper.offset(region, -tool_radius)
    if not core:
        logger.info("刀具半径 %.3fmm 太大：区域 %.1fmm² 内缩后什么都不剩",
                    tool_radius, region.area)
        return PocketResult(passes=(), stepover_mm=stepover_mm, tool_radius_mm=tool_radius)

    passes: list[RingPass] = []
    current = core
    inset = 0.0
    for level in range(max_rings):
        area = clipper.total_area(current)
        if area < min_area_mm2:
            break
        for item in current:
            passes.append(RingPass(polygon=item.outer, level=level, inset_mm=inset))
            for hole in item.holes:
                passes.append(RingPass(polygon=hole, level=level, inset_mm=inset))
        nxt = clipper.offset(current[0], -stepover_mm) if len(current) == 1 else _offset_many(
            clipper, current, -stepover_mm
        )
        if not nxt or clipper.total_area(nxt) < min_area_mm2:
            break
        current = nxt
        inset += stepover_mm

    if order == "inside_out":
        passes.reverse()

    links = _link(passes)
    leftover = current if clipper.total_area(current) >= min_area_mm2 else ()
    logger.info("环切：核心区 %.1fmm²，%d 圈，切削 %.1fmm，连接 %.1fmm，残料 %.3fmm²",
                clipper.total_area(core), len(passes),
                sum(item.polygon.length for item in passes),
                sum(float(np.hypot(b[0] - a[0], b[1] - a[1])) for a, b in links),
                clipper.total_area(leftover) if leftover else 0.0)
    return PocketResult(
        passes=tuple(passes), core=core, leftover=tuple(leftover),
        stepover_mm=stepover_mm, tool_radius_mm=tool_radius, links=links,
    )


def _offset_many(clipper: ContourClipper, regions: Iterable[Region2D],
                 delta: float) -> tuple[Region2D, ...]:
    """对多个区域一起偏置（逐环送进同一个 ClipperOffset，避免区域间的边互相干扰）。"""

    collected: list[Region2D] = []
    for region in regions:
        collected.extend(clipper.offset(region, delta))
    return tuple(collected)


def _link(passes: list[RingPass], *,
          mode: LinkMode = "nearest") -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    """决定"上一圈结束 → 下一圈开始"的连接方式。

    闭环的起点是任意的，所以按**最近点**连接能显著减少空行程；
    ``straight`` 就老实按各自的起点连。
    """

    links: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for previous, following in zip(passes, passes[1:]):
        end = tuple(float(v) for v in previous.polygon.points[-1])
        if mode == "straight":
            start = tuple(float(v) for v in following.polygon.points[0])
        else:
            start = _nearest_point(following.polygon, end)
        links.append((end, start))  # type: ignore[arg-type]
    return tuple(links)


def _nearest_point(polygon: Polygon2D, target: tuple[float, float]) -> tuple[float, float]:
    delta = polygon.points - np.asarray(target, dtype=np.float64)
    index = int(np.argmin(np.linalg.norm(delta, axis=1)))
    return (float(polygon.points[index, 0]), float(polygon.points[index, 1]))


def rotate_to(polygon: Polygon2D, start: tuple[float, float]) -> Polygon2D:
    """把环的起点转到离 ``start`` 最近的那个顶点（生成 G 代码时用）。"""

    delta = polygon.points - np.asarray(start, dtype=np.float64)
    index = int(np.argmin(np.linalg.norm(delta, axis=1)))
    return Polygon2D(np.roll(polygon.points, -index, axis=0))
