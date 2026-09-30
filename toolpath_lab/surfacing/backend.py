"""opencamlib 可用性探测与延迟导入。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

INSTALL_HINT = "缺少 opencamlib。安装：\n    python -m pip install opencamlib"


class OclUnavailableError(RuntimeError):
    """opencamlib 没装或导入失败。"""


def _probe() -> tuple[bool, str]:
    try:
        import opencamlib as ocl  # noqa: F401
    except Exception as error:  # pragma: no cover - 取决于环境
        return False, f"{type(error).__name__}: {error}"
    # 顶层 import 成功还不够：真正要用的类要单独确认
    missing = [name for name in ("STLSurf", "Triangle", "Point", "CylCutter",
                                 "PathDropCutter")
               if not hasattr(ocl, name)]
    if missing:
        return False, f"opencamlib 已导入但缺少 {', '.join(missing)}"
    return True, ""


OCL_AVAILABLE, OCL_PROBE_ERROR = _probe()


def require_ocl() -> None:
    if not OCL_AVAILABLE:
        raise OclUnavailableError(f"{INSTALL_HINT}\n探测结果：{OCL_PROBE_ERROR}")


__all__ = ["INSTALL_HINT", "OCL_AVAILABLE", "OCL_PROBE_ERROR", "OclUnavailableError",
           "require_ocl"]
