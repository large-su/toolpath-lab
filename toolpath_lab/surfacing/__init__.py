"""3 轴曲面加工层（三角网格 → 沿曲面起伏的刀路）。

职责边界（**不要越界**）：

* 本包**只吃三角网格**（:class:`~toolpath_lab.core.stock.Mesh`）与 BRep 的**分层截面**
  —— ``opencamlib`` 不认识 BRep，BRep 必须先由 :mod:`toolpath_lab.brep` 离散成网格
  （或按层剖切成 2D 轮廓）。
* 不读文件、不碰 BRep 拓扑、不做 2D 多边形布尔与偏置（那是 ``contour2d`` 的活）。
* 输出 :class:`~toolpath_lab.core.path.Toolpath`，与实验台的刀路同一种结构，
  因此导出与切削仿真不需要任何改动。

两条刀路：

* **平行行切**（``parallel_toolpath``）：用 opencamlib 的落刀（drop cutter）沿一族平行线
  求出刀心高度，适合平缓曲面。
* **等高铣**（``waterline_toolpath``）：按 Z 分层，每层用 **OCP 切轮廓 + pyclipper 偏置**
  得到刀心轨迹。之所以不用 opencamlib 的 ``Waterline``/``AdaptiveWaterline``，
  见 :mod:`toolpath_lab.surfacing.waterline` 里的实测说明。

两个第三方库各司其职、没有互相越界：OCL 只做它擅长的落刀，OCP 只剖切，pyclipper 只偏置。
"""

from toolpath_lab.surfacing.backend import OCL_AVAILABLE, OclUnavailableError, require_ocl
from toolpath_lab.surfacing.parameters import (coerce_surface_parameters,
                                               surface_defaults_for, surface_parameters,
                                               surface_parameters_for)

__all__ = ["OCL_AVAILABLE", "OclUnavailableError", "coerce_surface_parameters", "require_ocl",
           "surface_defaults_for", "surface_parameters", "surface_parameters_for"]

if OCL_AVAILABLE:  # pragma: no cover - 依赖缺失时不导入
    from toolpath_lab.surfacing.dropcutter import (ParallelRequest, ParallelResult,
                                                   parallel_toolpath)
    from toolpath_lab.surfacing.mesh_input import make_cutter, mesh_to_stlsurf
    from toolpath_lab.surfacing.waterline import (WaterlineRequest, WaterlineResult,
                                                  waterline_toolpath)

    __all__ += [
        "ParallelRequest",
        "ParallelResult",
        "WaterlineRequest",
        "WaterlineResult",
        "make_cutter",
        "mesh_to_stlsurf",
        "parallel_toolpath",
        "waterline_toolpath",
    ]
