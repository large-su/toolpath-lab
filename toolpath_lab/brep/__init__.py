"""BRep 层（OCP / OpenCascade 引擎）。

职责边界（**不要越界**）：

* 本包只做 **BRep**：读 STEP/IGES、拓扑查询、模型修复、Z 向分层切片、BRep → 三角网格。
* **不做任何刀路计算**。切片产物是 2D 轮廓（交给 ``contour2d`` 做偏置/环切），
  网格产物是三角面片（交给 ``surfacing`` 做 3 轴刀路）。
* 不 import ``contour2d`` / ``surfacing`` / ``opencamlib`` / ``pyclipper`` —— 只依赖 OCP + numpy。

层间只走两个中立结构：:class:`~toolpath_lab.contour2d.polygon.Region2D`（XY 平面的 2D 轮廓）
与 :class:`~toolpath_lab.core.stock.Mesh`（三角面片）。
"""

from toolpath_lab.brep.backend import OCP_AVAILABLE, OcpUnavailableError, require_ocp
from toolpath_lab.brep.errors import (BrepError, BrepFormatError, BrepSizeError,
                                      BrepUnsupportedError)

__all__ = ["OCP_AVAILABLE", "BrepError", "BrepFormatError", "BrepSizeError",
           "BrepUnsupportedError", "OcpUnavailableError", "require_ocp"]

if OCP_AVAILABLE:  # pragma: no cover - 依赖缺失时不导入下列模块
    from toolpath_lab.brep.importer import (ALLOWED_SUFFIXES, DEFAULT_MAX_BYTES,
                                            SUPPORTED_SUFFIXES, ImportResult, import_model,
                                            import_model_bytes, import_model_bytes_full,
                                            import_model_full, probe_model)
    from toolpath_lab.brep.model import BrepFaceInfo, BrepModel, load_brep
    from toolpath_lab.brep.section import SliceOptions, SliceResult, slice_at, slice_range
    from toolpath_lab.brep.tessellate_model import MeshOptions, to_tessellated_model

    __all__ += [
        "ALLOWED_SUFFIXES",
        "BrepFaceInfo",
        "BrepModel",
        "DEFAULT_MAX_BYTES",
        "ImportResult",
        "MeshOptions",
        "SUPPORTED_SUFFIXES",
        "SliceOptions",
        "SliceResult",
        "import_model",
        "import_model_bytes",
        "import_model_bytes_full",
        "import_model_full",
        "load_brep",
        "probe_model",
        "slice_at",
        "slice_range",
        "to_tessellated_model",
    ]
