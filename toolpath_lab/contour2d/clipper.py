"""pyclipper 封装：2D 布尔运算与偏置（刀具半径补偿）。

**只做 2D 轮廓几何**，不涉及 Z、不涉及刀路顺序（顺序在 :mod:`pocket`）。

pyclipper 用**整数**坐标，所以所有坐标进出都要乘/除 ``scale``。
``scale=1000`` 表示 0.001 mm 的量化精度，对铣削足够，同时把整数控制在 1e6 量级
（Clipper 内部用 64 位整数，这个量级很安全）。
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping, Sequence

import numpy as np
import pyclipper

from toolpath_lab.contour2d.grouping import representative_point, regions_from_polygons
from toolpath_lab.contour2d.polygon import MIN_RING_AREA_MM2, Polygon2D, Region2D

logger = logging.getLogger(__name__)

#: 坐标量化：1 单位 = 0.001 mm。
DEFAULT_SCALE = 1000.0
#: 圆角偏置的弧弦高容差（mm）。越小圆弧越平滑、点越多。
DEFAULT_ARC_TOLERANCE_MM = 0.01

_JOIN_TYPES: Mapping[str, int] = {
    "round": pyclipper.JT_ROUND,
    "square": pyclipper.JT_SQUARE,
    "miter": pyclipper.JT_MITER,
}

_BOOLEAN_OPS: Mapping[str, int] = {
    "intersection": pyclipper.CT_INTERSECTION,
    "union": pyclipper.CT_UNION,
    "difference": pyclipper.CT_DIFFERENCE,
    "xor": pyclipper.CT_XOR,
}

#: 布尔填充规则。加工轮廓一律用 NONZERO：即使输入有自交/重复环也不会算丢。
_FILL = pyclipper.PFT_NONZERO


def _set_strictly_simple(clipper: "pyclipper.Pyclipper") -> None:
    """打开 StrictlySimple（这个版本的绑定名不一致，取不到就跳过）。"""

    setter = getattr(clipper, "StrictlySimple", None)
    if callable(setter):
        try:
            setter(True)
        except Exception:  # pragma: no cover - 绑定差异，不影响正确性
            logger.debug("该 pyclipper 版本不支持 StrictlySimple，已跳过")


class ContourClipper:
    """2D 轮廓的布尔与偏置。

    典型用法::

        clipper = ContourClipper()
        core = clipper.offset(region, -tool_radius)        # 内缩一个刀半径（刀心可达区）
        cut = clipper.boolean(core, island, "difference")  # 挖掉岛屿
    """

    def __init__(self, *, scale: float = DEFAULT_SCALE,
                 arc_tolerance_mm: float = DEFAULT_ARC_TOLERANCE_MM) -> None:
        if scale <= 0:
            raise ValueError("scale 必须为正")
        self.scale = float(scale)
        self.arc_tolerance = float(arc_tolerance_mm)

    # ------------------------------------------------------------ 坐标转换
    def _to_int(self, polygon: Polygon2D) -> list[tuple[int, int]]:
        scaled = np.rint(polygon.points * self.scale).astype(np.int64)
        return [(int(px), int(py)) for px, py in scaled]

    def _from_int(self, path: Sequence[Sequence[int]]) -> Polygon2D | None:
        if len(path) < 3:
            return None
        array = np.asarray(path, dtype=np.float64) / self.scale
        try:
            return Polygon2D(array)
        except ValueError:
            return None

    # ------------------------------------------------------------ 层级组装
    def _regions_from_paths(self, paths: Iterable[Sequence[Sequence[int]]]
                            ) -> tuple[Region2D, ...]:
        """把**扁平的**结果路径组装成"外环 + 孔"的区域列表。

        为什么要自己组装：pyclipper 1.4 的 ``PyclipperOffset.Execute`` 只返回扁平路径，
        ``Pyclipper.Execute`` 也没有可用的 PolyTree 输出（``PyPolyNode`` 传不进去），
        所以层级只能自己算。具体规则见 :mod:`toolpath_lab.contour2d.grouping`。
        """

        polygons = [item for item in (self._from_int(path) for path in paths)
                    if item is not None]
        return regions_from_polygons(polygons)

    # ------------------------------------------------------------ 偏置
    def offset(self, region: Region2D, delta_mm: float, *,
               join: str = "round") -> tuple[Region2D, ...]:
        """整体偏置。

        ``delta_mm > 0`` 外扩（留余量），``< 0`` 内缩（刀具半径补偿）。
        孔会自动往**相反方向**偏（Clipper 按有向面积判断），
        因此**不要**自己给孔取反符号 —— 那是最常见的错。

        :param region: 输入区域（外环 CCW + 孔 CW，:class:`Region2D` 已保证）
        :param delta_mm: 偏置量，mm
        :param join: 拐角处理 ``round`` / ``square`` / ``miter``
        :returns: 偏置后的区域元组；完全缩没了就是空元组
        """

        if join not in _JOIN_TYPES:
            raise ValueError(f"未知拐角类型 {join!r}，可选 {sorted(_JOIN_TYPES)}")
        if abs(delta_mm) < 1e-12:
            return (region,)

        # arc_tolerance 是**整数单位**（已缩放），不是 mm
        offset = pyclipper.PyclipperOffset(miter_limit=2.0,
                                           arc_tolerance=self.arc_tolerance * self.scale)
        for ring in region.rings():
            offset.AddPath(self._to_int(ring), _JOIN_TYPES[join], pyclipper.ET_CLOSEDPOLYGON)
        solution = offset.Execute(delta_mm * self.scale)
        regions = self._regions_from_paths(solution)
        logger.debug("偏置 %.3fmm：%d 个环 -> %d 个区域", delta_mm,
                     len(region.rings()), len(regions))
        return regions

    # ------------------------------------------------------------ 布尔
    def boolean(self, subject: Region2D, clip: Region2D | None = None, *,
                op: str = "difference") -> tuple[Region2D, ...]:
        """布尔运算。

        :param subject: 主区域
        :param clip: 参与运算的另一个区域（``union`` 且只并自身时可省略）
        :param op: ``difference``（subject - clip）/ ``union`` / ``intersection`` / ``xor``
        """

        if op not in _BOOLEAN_OPS:
            raise ValueError(f"未知布尔运算 {op!r}，可选 {sorted(_BOOLEAN_OPS)}")
        clipper = pyclipper.Pyclipper()
        _set_strictly_simple(clipper)
        for ring in subject.rings():
            clipper.AddPath(self._to_int(ring), pyclipper.PT_SUBJECT, True)
        if clip is not None:
            for ring in clip.rings():
                clipper.AddPath(self._to_int(ring), pyclipper.PT_CLIP, True)
        solution = clipper.Execute(_BOOLEAN_OPS[op], _FILL, _FILL)
        return self._regions_from_paths(solution)

    def union_all(self, regions: Iterable[Region2D]) -> tuple[Region2D, ...]:
        """把多个区域并成一个（重叠部分自动合并）。"""

        clipper = pyclipper.Pyclipper()
        _set_strictly_simple(clipper)
        count = 0
        for region in regions:
            for ring in region.rings():
                clipper.AddPath(self._to_int(ring), pyclipper.PT_SUBJECT, True)
                count += 1
        if not count:
            return ()
        solution = clipper.Execute(pyclipper.CT_UNION, _FILL, _FILL)
        return self._regions_from_paths(solution)

    # ------------------------------------------------------------ 便利方法
    @staticmethod
    def total_area(regions: Iterable[Region2D]) -> float:
        return float(sum(region.area for region in regions))

    def cleanup(self, region: Region2D, *, tolerance_mm: float = 0.0005
                ) -> tuple[Region2D, ...]:
        """清理自交与微小毛刺（先小幅外扩再等量内缩）。

        从 CAD 或 ACP 切片拿到的轮廓经常有自交、重复点、针状毛刺，直接偏置会炸出大量碎环。
        """

        grown = self.offset(region, tolerance_mm)
        if not grown:
            return ()
        merged = self.union_all(grown)
        out: list[Region2D] = []
        for item in merged:
            shrunk = self.offset(item, -tolerance_mm)
            out.extend(shrunk)
        return tuple(out)
