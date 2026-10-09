"""把三角网格喂给 opencamlib。

**这是 BRep 与网格之间的一道硬边界**：opencamlib 只认三角面片，
所以进入本模块的必须已经是 :class:`~toolpath_lab.core.stock.Mesh`
（由 :mod:`toolpath_lab.brep` 从 BRep 离散而来），不能是 BRep。

实测坑点（都是踩出来的）：

* **z 不能为负**：落刀在 z<0 的区域一律返回"无接触"（z=0）。
  实测同一张 20° 斜面，z 范围 −21.8..21.8 时最大偏差 17.1mm，
  整体抬高 30mm 后偏差降到 1.0mm。本模块因此在转换时**强制检查 z_min ≥ 0**。
* **共面网格会被离散精度影响**：整块平面若被切成很多小三角形，落刀结果会整体
  偏高约一个格宽（实测 1mm 格 → 偏高 1.0000mm）。OCP 的大平面只出 2 个三角形，
  所以真实零件基本不受影响——但自己造网格时要注意。
"""

from __future__ import annotations

import logging

import numpy as np

from toolpath_lab.core.stock import Mesh
from toolpath_lab.core.tessellation import TessellatedModel
from toolpath_lab.surfacing.backend import require_ocl

logger = logging.getLogger(__name__)

#: z 低于这个值就认为网格没归一化到机床坐标系（会触发 opencamlib 的"无接触"）。
MIN_Z_MM = -1e-6


def mesh_to_stlsurf(source: Mesh | TessellatedModel, *, tolerance: float = 1e-9):
    """网格 -> ``ocl.STLSurf``。

    :param source: :class:`Mesh` 或 :class:`TessellatedModel`（后者取其网格部分）
    :param tolerance: 允许的 z 负值容差
    :raises ValueError: 网格为空，或 z 明显为负（opencamlib 会静默返回无接触）
    """

    require_ocl()
    import opencamlib as ocl

    if isinstance(source, TessellatedModel):
        positions, indices = source.positions, source.indices
    elif isinstance(source, Mesh):
        positions, indices = source.positions, source.indices
    else:
        raise TypeError(f"只接受 Mesh 或 TessellatedModel，收到 {type(source).__name__}")

    if indices.shape[0] == 0 or positions.shape[0] == 0:
        raise ValueError("网格是空的，无法生成曲面刀路")

    z_min = float(positions[:, 2].min())
    if z_min < MIN_Z_MM - tolerance:
        raise ValueError(
            f"网格最低点 z={z_min:.3f} 为负。opencamlib 的落刀对 z<0 的区域会返回"
            "『无接触』（结果 z=0），必须先归一化到 Z 最低点为 0"
            "（用 BrepModel.normalized() 或 TessellatedModel.translation_to_origin()）"
        )

    surf = ocl.STLSurf()
    point = ocl.Point
    triangle = ocl.Triangle
    for corners in indices:
        a, b, c = positions[corners[0]], positions[corners[1]], positions[corners[2]]
        surf.addTriangle(triangle(
            point(float(a[0]), float(a[1]), float(a[2])),
            point(float(b[0]), float(b[1]), float(b[2])),
            point(float(c[0]), float(c[1]), float(c[2])),
        ))
    logger.debug("网格 -> STLSurf：%d 个三角形", int(indices.shape[0]))
    return surf


def make_cutter(kind: str, diameter_mm: float, length_mm: float):
    """按刀具类型创建 opencamlib 刀具。

    :param kind: ``flat`` / ``ball`` / ``bull``
    :param diameter_mm: 刀具直径
    :param length_mm: 刃长
    """

    require_ocl()
    import opencamlib as ocl

    diameter = float(diameter_mm)
    if diameter <= 0:
        raise ValueError("刀具直径必须为正")
    if kind == "flat":
        return ocl.CylCutter(diameter, float(length_mm))
    if kind == "ball":
        return ocl.BallCutter(diameter, float(length_mm))
    if kind == "bull":
        # BullCutter(diameter, corner_radius, length)：圆鼻刀的角半径取直径的 1/4 作为默认
        return ocl.BullCutter(diameter, diameter / 4.0, float(length_mm))
    raise ValueError(f"未知刀具类型 {kind!r}，可选 flat / ball / bull")


__all__ = ["MIN_Z_MM", "make_cutter", "mesh_to_stlsurf"]
