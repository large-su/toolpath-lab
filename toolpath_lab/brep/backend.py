"""OCP 可用性探测与延迟导入。

OCP（CadQuery 的 OpenCascade 绑定）是一个很大的可选依赖，没装时整层都要能"优雅缺席"：
导入 ``toolpath_lab.brep`` 不报错，只在真正调用时给出可操作的提示。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 安装提示（用户看到的就是这一句，所以要把命令写全）。
INSTALL_HINT = (
    "缺少 OCP（OpenCascade Python 绑定）。安装：\n"
    "    python -m pip install cadquery-ocp\n"
    "注意：PyPI 上还有一个同名的无关包 `ocp`，它只包含一个可执行文件，**不是** OpenCascade 绑定。"
)


class OcpUnavailableError(RuntimeError):
    """OCP 没装或导入失败。"""


def _probe() -> tuple[bool, str]:
    try:
        import OCP  # noqa: F401
    except Exception as error:  # pragma: no cover - 取决于环境
        return False, f"{type(error).__name__}: {error}"
    # 只 import 到顶层还不够：绑定不全时真正的类会在子模块里缺失
    try:
        from OCP.STEPControl import STEPControl_Reader  # noqa: F401
        from OCP.BRepMesh import BRepMesh_IncrementalMesh  # noqa: F401
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Section  # noqa: F401
    except Exception as error:  # pragma: no cover - 取决于环境
        return False, f"OCP 已导入但缺少必要子模块：{type(error).__name__}: {error}"
    return True, ""


OCP_AVAILABLE, OCP_PROBE_ERROR = _probe()


def require_ocp() -> None:
    """在真正要用 OCP 的地方调用；不可用时抛出带安装说明的异常。"""

    if not OCP_AVAILABLE:
        raise OcpUnavailableError(f"{INSTALL_HINT}\n探测结果：{OCP_PROBE_ERROR}")


__all__ = ["INSTALL_HINT", "OCP_AVAILABLE", "OCP_PROBE_ERROR", "OcpUnavailableError",
           "require_ocp"]
