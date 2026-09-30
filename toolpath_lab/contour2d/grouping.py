"""把一堆散乱的闭环组装成"外环 + 孔"的区域。

偏置结果、布尔结果、CAD 切片结果都是**扁平的一堆环**，谁是外环、谁是孔、谁包着谁，
需要按**包含深度**判断：被别的环包住的层数 d，d 为偶数是外环、奇数为孔。

只看"有向面积正负"是不够的 —— 遇到"孔里还有岛"（深度 2 的环）就会判错。
本模块在 pyclipper 层与 OCP 层之间共用，避免两边各写一份。
"""

from __future__ import annotations

import logging
from typing import Iterable

from toolpath_lab.contour2d.polygon import MIN_RING_AREA_MM2, Polygon2D, Region2D

logger = logging.getLogger(__name__)


def representative_point(polygon: Polygon2D) -> tuple[float, float]:
    """取一个"确实落在环内部"的点，用于判包含。

    面积质心对凹多边形可能落在外面（L 形、C 形很常见），所以要检查一次再退回顶点。
    """

    centroid = polygon.centroid()
    if polygon.contains(centroid):
        return centroid
    return (float(polygon.points[0, 0]), float(polygon.points[0, 1]))


def regions_from_polygons(polygons: Iterable[Polygon2D], *,
                          min_area_mm2: float = MIN_RING_AREA_MM2
                          ) -> tuple[Region2D, ...]:
    """按包含深度把环组装成区域。

    :param polygons: 任意顺序的闭环（方向不限，:class:`Region2D` 会统一）
    :param min_area_mm2: 小于这个面积的环按退化丢掉（偏置到快消失时会产生针状碎环）
    :returns: 区域元组，顺序不保证
    """

    items = [poly for poly in polygons if abs(poly.area) > min_area_mm2]
    if not items:
        return ()

    reps = [representative_point(poly) for poly in items]
    depths: list[int] = []
    for index, poly in enumerate(items):
        depth = 0
        for other_index, other in enumerate(items):
            if other_index == index:
                continue
            if abs(other.area) > abs(poly.area) and other.contains(reps[index]):
                depth += 1
        depths.append(depth)

    outer_indices = [i for i, d in enumerate(depths) if d % 2 == 0]
    grouped: dict[int, list[Polygon2D]] = {index: [] for index in outer_indices}
    for index, depth in enumerate(depths):
        if depth % 2 == 0:
            continue
        hole = items[index]
        # 挂到"能包住它、且面积最小"的那个外环上。
        # 注意取的是外环的**下标**（元组第二项），不是面积 —— 用面积当键会直接 KeyError。
        candidates = [(abs(items[outer].area), outer)
                      for outer in outer_indices if items[outer].contains(reps[index])]
        if candidates:
            grouped[min(candidates)[1]].append(hole)
        else:
            logger.warning("有个孔找不到所属外环（面积 %.3fmm²），已丢弃", hole.area)

    return tuple(Region2D(items[index], tuple(grouped[index])) for index in outer_indices)
