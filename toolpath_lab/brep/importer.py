"""模型导入入口：文件/字节 -> :class:`TessellatedModel`。

上层（HTTP 接口、工程）只调这里的 :func:`import_model`，
不直接碰 OCP，也不需要知道离散参数怎么传。

与旧的 ``read_step_bytes`` 相比有两个变化：

* **格式**：除了 STEP，还支持 IGES（OCP 一并读）；
* **精度**：体积/面积走 OpenCascade 的解析计算，不是网格近似。
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from toolpath_lab.brep.backend import OcpUnavailableError, require_ocp
from toolpath_lab.brep.errors import BrepFormatError, BrepSizeError, BrepUnsupportedError
from toolpath_lab.brep.model import (ALLOWED_SUFFIXES, DEFAULT_MAX_BYTES, SUPPORTED_SUFFIXES,
                                     BrepModel, load_brep)
from toolpath_lab.brep.tessellate_model import MeshOptions, to_tessellated_model
from toolpath_lab.core.tessellation import TessellatedModel

logger = logging.getLogger(__name__)

__all__ = ["ALLOWED_SUFFIXES", "DEFAULT_MAX_BYTES", "SUPPORTED_SUFFIXES", "ImportResult",
           "import_model", "import_model_bytes", "import_model_bytes_full", "import_model_full",
           "probe_model"]


@dataclass(frozen=True, slots=True)
class ImportResult:
    """一次导入的两个产物。

    绝大多数调用方只要 :attr:`model`（中立网格）就够了；**等高铣要 :attr:`brep`**——
    分层剖切需要 BRep 拓扑，网格里没有。两者共用同一次读取与同一次归一化，
    因此 :attr:`brep` 与 :attr:`model` 一定在同一个坐标系里。
    """

    brep: BrepModel
    model: TessellatedModel


def import_model(path: str | Path, *, name: str = "",
                 options: MeshOptions | None = None,
                 normalize: bool = True,
                 heal: bool = False,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> TessellatedModel:
    """读一个模型文件并离散。

    :param path: STEP / IGES 文件
    :param name: 覆盖模型名（默认用文件名）
    :param options: 离散参数，见 :class:`MeshOptions`
    :param normalize: 归一化到机床坐标系（XY 居中、Z 最低点 0）
    :param heal: 先做一次 ShapeFix（模型不干净时打开）
    :param max_bytes: 文件体积上限
    :raises BrepSizeError: 文件超过上限
    :raises BrepFormatError: 后缀不支持 / 读不出来
    :raises BrepUnsupportedError: 读出来了但没有可用的几何
    :raises OcpUnavailableError: 没装 OCP
    """

    return import_model_full(path, name=name, options=options, normalize=normalize,
                             heal=heal, max_bytes=max_bytes).model


def import_model_full(path: str | Path, *, name: str = "",
                      options: MeshOptions | None = None,
                      normalize: bool = True,
                      heal: bool = False,
                      max_bytes: int = DEFAULT_MAX_BYTES) -> ImportResult:
    """同 :func:`import_model`，但把 BRep 也一并返回（等高铣需要）。"""

    require_ocp()
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise BrepFormatError(
            f"不支持的后缀 {suffix!r}，支持 {', '.join(SUPPORTED_SUFFIXES)}"
        )
    if not file_path.is_file():
        raise BrepFormatError(f"找不到模型文件：{file_path}")
    size = file_path.stat().st_size
    if size > max_bytes:
        raise BrepSizeError(
            f"文件 {size / 1048576:.1f} MB 超过上限 {max_bytes / 1048576:.1f} MB"
        )
    if size == 0:
        raise BrepFormatError("文件是空的")

    model = load_brep(file_path, heal=heal, normalize=False, name=name or file_path.stem)
    return _finish(model, options=options, normalize=normalize)


def import_model_bytes(data: bytes, *, source_name: str = "",
                       options: MeshOptions | None = None,
                       normalize: bool = True,
                       heal: bool = False,
                       max_bytes: int = DEFAULT_MAX_BYTES) -> TessellatedModel:
    """HTTP 上传的字节流版本。

    OCP 的读取器只认文件路径，所以这里落到临时文件再读 —— 后缀要保留，
    读取器是靠它挑 STEP / IGES 的。
    """

    return import_model_bytes_full(data, source_name=source_name, options=options,
                                   normalize=normalize, heal=heal,
                                   max_bytes=max_bytes).model


def import_model_bytes_full(data: bytes, *, source_name: str = "",
                            options: MeshOptions | None = None,
                            normalize: bool = True,
                            heal: bool = False,
                            max_bytes: int = DEFAULT_MAX_BYTES) -> ImportResult:
    """同 :func:`import_model_bytes`，但把 BRep 也一并返回。"""

    require_ocp()
    if not data:
        raise BrepFormatError("上传的文件是空的")
    if len(data) > max_bytes:
        raise BrepSizeError(
            f"文件 {len(data) / 1048576:.1f} MB 超过上限 {max_bytes / 1048576:.1f} MB"
        )
    stem = Path(source_name or "model").stem or "model"
    suffix = Path(source_name or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise BrepFormatError(
            f"不支持的文件名 {source_name!r}，支持 {', '.join(SUPPORTED_SUFFIXES)}"
        )
    with tempfile.TemporaryDirectory(prefix="tplab-model-") as folder:
        temp_path = Path(folder) / f"{stem}{suffix}"
        temp_path.write_bytes(data)
        logger.debug("上传 %d 字节落到临时文件 %s", len(data), temp_path)
        return import_model_full(temp_path, name=stem, options=options,
                                 normalize=normalize, heal=heal, max_bytes=max_bytes)


def probe_model(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """只做便宜的检查（存在、后缀、体积），不解析几何。"""

    file_path = Path(path)
    if not file_path.is_file():
        return {"ok": False, "error": "找不到文件", "name": file_path.name}
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return {"ok": False, "error": f"不支持的后缀 {suffix}", "name": file_path.name}
    size = file_path.stat().st_size
    if size > max_bytes:
        return {"ok": False, "error": "文件过大", "name": file_path.name, "size_bytes": size}
    return {"ok": True, "name": file_path.name, "size_bytes": size}


def _finish(model: BrepModel, *, options: MeshOptions | None,
            normalize: bool) -> ImportResult:
    """把 BRep 收尾成上层的离散模型，并把 OCP 的异常翻译成本层的错误类型。

    归一化**在 BRep 上做一次**，再让离散走 ``normalize=False``：这样
    :attr:`ImportResult.brep` 与 :attr:`ImportResult.model` 必然同坐标系，
    等高铣剖切出来的层高才能和网格刀路对齐。
    """

    if model.face_count == 0:
        raise BrepUnsupportedError(
            "文件里没有可加工的 B-rep 面（可能只有线框、二维图纸或曲面片）"
        )
    if normalize:
        model = model.normalized()
    try:
        result = to_tessellated_model(model, options, normalize=False)
    except Exception as error:  # pragma: no cover - 取决于具体模型
        raise BrepUnsupportedError(f"模型离散失败：{type(error).__name__}: {error}") from error
    if result.triangle_count == 0:
        raise BrepUnsupportedError("模型离散后没有任何三角面，无法用于加工")
    if normalize:
        result.warnings.append("模型已按机床坐标系归一：XY 居中、Z 最低点为 0")
    result.warnings.extend(model.warnings)
    return ImportResult(brep=model, model=result)
