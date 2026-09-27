"""Z 向分层切片：用水平面切 BRep，得到该高度的 2D 加工轮廓。

这是 2.5D 加工（型腔铣/平面铣/轮廓铣）的入口：切片产物是
:class:`~toolpath_lab.contour2d.polygon.Region2D`，交给 ``contour2d`` 做偏置与环切。

**踩过的坑**：OCP 的 ``BRepAlgoAPI_Section`` 返回的是**一堆散乱的边**（edges），
**不是** wire。实测切一个 200×200 的零件，结果是 17 条边 / 0 条 wire。
所以必须自己按端点把边拼成闭环 —— 本模块的 :func:`_stitch_edges` 就是干这个的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.brep.backend import require_ocp
from toolpath_lab.brep.model import BrepModel
from toolpath_lab.contour2d.grouping import regions_from_polygons
from toolpath_lab.contour2d.polygon import Polygon2D, Region2D

logger = logging.getLogger(__name__)

#: 端点拼合容差（mm）。切片边的端点本该严格重合，给一点点余量容忍浮点误差。
DEFAULT_JOIN_TOLERANCE_MM = 1e-4


@dataclass(frozen=True, slots=True)
class SliceOptions:
    """切片参数。

    :param deflection: 曲线离散的弦高容差（mm）。越小圆弧越平滑。
        刀路用 0.01~0.05；预览 0.1 即可。
    :param tolerance: 端点拼合容差（mm）。
    :param min_area_mm2: 小于这个面积的环丢掉（切片在尖角处会掉出针状碎环）。
    """

    deflection: float = 0.02
    tolerance: float = DEFAULT_JOIN_TOLERANCE_MM
    min_area_mm2: float = 1e-3


@dataclass(slots=True)
class SliceResult:
    """一个 Z 高度上的切片结果。"""

    z: float
    regions: tuple[Region2D, ...]
    options: SliceOptions
    #: 没能拼成闭环的边数（>0 说明轮廓可能不完整，要留意）
    open_chains: int = 0

    @property
    def area_mm2(self) -> float:
        return float(sum(region.area for region in self.regions))

    def to_payload(self) -> dict:
        return {
            "z": round(self.z, 4),
            "regions": [region.tolist() for region in self.regions],
            "area_mm2": round(self.area_mm2, 4),
            "open_chains": self.open_chains,
        }


def slice_at(model: BrepModel, z: float, options: SliceOptions | None = None) -> SliceResult:
    """在高度 ``z`` 处水平切一刀。

    :param model: BRep 模型
    :param z: 切平面高度（模型自身坐标系，mm）
    :param options: 见 :class:`SliceOptions`
    :returns: :class:`SliceResult`；切不到东西时 ``regions`` 为空
    """

    require_ocp()
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    options = options or SliceOptions()
    section = BRepAlgoAPI_Section(
        model.shape, gp_Pln(gp_Pnt(0.0, 0.0, float(z)), gp_Dir(0.0, 0.0, 1.0))
    )
    section.Build()
    if not section.IsDone():
        raise ValueError(f"Z={z} 切片失败（OCP 没有完成布尔截面运算）")

    edges = _discretize_edges(section.Shape(), options.deflection)
    loops, open_chains = _stitch_edges(edges, options.tolerance)
    # 切平面是水平的（Z 恒定），投到 XY 就得到 2D 轮廓。
    # 注意要显式取前两列：拼出来的折线是 (N,3)，而 Polygon2D 只接受 (N,2)。
    polygons = []
    for loop in loops:
        if len(loop) < 3:
            continue
        try:
            polygons.append(Polygon2D(np.ascontiguousarray(loop[:, :2])))
        except ValueError as error:
            logger.debug("丢掉一个退化的切片环：%s", error)
    regions = regions_from_polygons(polygons, min_area_mm2=options.min_area_mm2)
    if open_chains:
        logger.warning("Z=%.3f 切片有 %d 条边没能拼成闭环，轮廓可能不完整", z, open_chains)
    logger.debug("Z=%.3f：%d 条边 -> %d 个闭环 -> %d 个区域，面积 %.3fmm²",
                 z, len(edges), len(loops), len(regions),
                 sum(region.area for region in regions))
    return SliceResult(z=float(z), regions=regions, options=options, open_chains=open_chains)


def slice_range(model: BrepModel, *, step_mm: float, z_min: float | None = None,
                z_max: float | None = None, options: SliceOptions | None = None
                ) -> list[SliceResult]:
    """按固定层高切一串高度，从下往上。

    层高 ``step_mm`` 是"切多密"，不是"切多深"—— 每层切出来的轮廓都完整，
    真正决定吃刀深度的是相邻两层之间那一段（由上层加工策略决定）。
    """

    if step_mm <= 0:
        raise ValueError("层高必须为正")
    x0, y0, z0, x1, y1, z1 = model.bounds()
    low = z0 if z_min is None else max(float(z_min), z0)
    high = z1 if z_max is None else min(float(z_max), z1)
    options = options or SliceOptions()

    levels: list[float] = []
    level = low + step_mm
    # 用 1e-6 而不是 1e-9 的余量：切平面**正好压在上下表面上**是退化情形
    # （平面与面共面，截面只剩零散的掠射边，拼不出完整闭环）。宁可少切一层。
    margin = max(1e-6, step_mm * 1e-6)
    while level < high - margin:
        levels.append(level)
        level += step_mm
    if not levels:
        levels = [(low + high) / 2.0]  # 太薄，至少给中间一刀

    results = [slice_at(model, level, options) for level in levels]
    logger.info("分层切片 %s：Z %.2f~%.2f，层高 %.2f -> %d 层",
                model.name or "模型", low, high, step_mm, len(results))
    return results


def _discretize_edges(shape, deflection: float
                      ) -> list[NDArray[np.float64]]:
    """把截面里的每条边离散成 3D 折线（保留 Z，拼环时再用 XY 判端点）。"""

    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    polylines: list[NDArray[np.float64]] = []
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge(explorer.Current())
        curve = BRepAdaptor_Curve(edge)
        discretizer = GCPnts_QuasiUniformDeflection(curve, float(deflection))
        if discretizer.IsDone() and discretizer.NbPoints() >= 2:
            points = np.array(
                [[discretizer.Value(i).X(), discretizer.Value(i).Y(), discretizer.Value(i).Z()]
                 for i in range(1, discretizer.NbPoints() + 1)],
                dtype=np.float64,
            )
            polylines.append(points)
        explorer.Next()
    return polylines


def _stitch_edges(polylines: Iterable[NDArray[np.float64]], tolerance: float
                  ) -> tuple[list[NDArray[np.float64]], int]:
    """把散乱的边按端点拼成闭环。

    贪心算法：取一条没用过的边当链头，然后不断找"起点或终点与链尾重合"的边接上去
    （需要时把那条边反向），直到回到链头（闭环）或再也接不上（开链）。

    :returns: (闭环列表, 接不上的开链数量)
    """

    remaining = [np.asarray(item, dtype=np.float64) for item in polylines
                 if len(item) >= 2]
    closed: list[NDArray[np.float64]] = []
    open_chains = 0

    while remaining:
        chain = remaining.pop(0)
        extended = True
        while extended:
            extended = False
            for index, candidate in enumerate(remaining):
                if np.linalg.norm(chain[-1, :2] - candidate[0, :2]) <= tolerance:
                    chain = np.vstack([chain, candidate[1:]])
                elif np.linalg.norm(chain[-1, :2] - candidate[-1, :2]) <= tolerance:
                    chain = np.vstack([chain, candidate[-2::-1]])
                elif np.linalg.norm(chain[0, :2] - candidate[-1, :2]) <= tolerance:
                    chain = np.vstack([candidate[:-1], chain])
                elif np.linalg.norm(chain[0, :2] - candidate[0, :2]) <= tolerance:
                    chain = np.vstack([candidate[::-1][:-1], chain])
                else:
                    continue
                remaining.pop(index)
                extended = True
                break

        if np.linalg.norm(chain[0, :2] - chain[-1, :2]) <= tolerance and len(chain) >= 4:
            closed.append(chain[:-1])  # 去掉重复的收尾点
        else:
            open_chains += 1
    return closed, open_chains


__all__ = ["SliceOptions", "SliceResult", "slice_at", "slice_range"]
