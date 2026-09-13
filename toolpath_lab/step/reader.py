"""STEP 读取入口。

对外只有两个函数：:func:`read_step`（文件）与 :func:`read_step_bytes`（HTTP 上传的字节）。
它们负责校验、解析、离散、坐标系归一，并把所有失败都翻译成 :mod:`toolpath_lab.step.errors`
里的异常——上层（HTTP 接口 / 脚本）只需要按异常类型决定状态码或提示文案。

坐标系归一
----------
导入后的模型统一搬到"机床坐标系"：XY 的中心落在原点、Z 的最低点落在 0。
毛坯计算、刀路生成、切削仿真都基于这个约定，因此整个下游只需要处理一种摆放。
原始位置不会被丢掉——归一只是平移网格顶点，尺寸与形状完全不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from toolpath_lab.step.errors import StepFormatError, StepSizeError, StepUnsupportedError
from toolpath_lab.step.parser import parse_step
from toolpath_lab.step.tessellate import TessellatedModel, tessellate_step

#: 默认单文件体积上限（字节）。工业 STEP 常有几 MB，32 MB 足够且不会拖垮内存。
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
#: 允许的扩展名（大小写不敏感）。
ALLOWED_SUFFIXES = (".step", ".stp", ".stpz")

#: STEP 文件在 Part 21 里是 ASCII/ISO-8859-1 兼容的，按 latin-1 读永远不会抛解码错误。
_ENCODING = "latin-1"


def read_step(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> TessellatedModel:
    """读取磁盘上的 STEP 文件并离散成网格。"""

    file_path = Path(path)
    if not file_path.is_file():
        raise StepFormatError(f"文件不存在：{file_path}")
    size = file_path.stat().st_size
    if size > max_bytes:
        raise StepSizeError(
            f"文件 {size / 1048576:.1f} MB 超过上限 {max_bytes / 1048576:.1f} MB"
        )
    if size == 0:
        raise StepFormatError("文件是空的")
    try:
        raw = file_path.read_bytes()
    except OSError as error:  # pragma: no cover - 权限问题
        raise StepFormatError(f"无法读取文件：{error}") from error
    return read_step_bytes(raw, source_name=file_path.name, max_bytes=max_bytes)


def read_step_bytes(raw: bytes, *, source_name: str = "", max_bytes: int = DEFAULT_MAX_BYTES,
                    normalize: bool = True) -> TessellatedModel:
    """读取内存里的 STEP 内容（HTTP 上传路径）。

    会捕获三类问题并抛出对应的异常：

    - 体积超限 -> :class:`StepSizeError`（HTTP 413）
    - 不是 STEP / 语法坏了 -> :class:`StepFormatError`（HTTP 400）
    - 合法但没有可离散的几何 -> :class:`StepUnsupportedError`（HTTP 422）
    """

    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise StepFormatError("STEP 内容必须是字节流")
    payload = bytes(raw)
    if len(payload) > max_bytes:
        raise StepSizeError(
            f"上传内容 {len(payload) / 1048576:.1f} MB 超过上限 {max_bytes / 1048576:.1f} MB"
        )
    if not payload:
        raise StepFormatError("上传内容为空")

    try:
        text = payload.decode(_ENCODING)
    except UnicodeDecodeError as error:  # pragma: no cover - latin-1 覆盖所有字节
        raise StepFormatError(f"无法解码文件内容：{error}") from error

    step = parse_step(text)
    model = tessellate_step(step, source_name=source_name)
    if model.triangle_count == 0:
        raise StepUnsupportedError("离散结果为空：文件里没有可用的三角形")
    if not np.all(np.isfinite(model.positions)):
        raise StepFormatError("离散结果含非法坐标（NaN / Inf），文件可能已损坏")

    if normalize:
        model.translated(model.translation_to_origin())
        model.warnings.append("模型已按机床坐标系归一：XY 居中、Z 最低点为 0")
    return model


def probe_step(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """只做轻量检查（大小、编码、段头），用于界面上的"预检"。"""

    file_path = Path(path)
    if not file_path.is_file():
        return {"ok": False, "error": f"文件不存在：{file_path}"}
    size = file_path.stat().st_size
    if size > max_bytes:
        return {"ok": False, "error": f"文件 {size / 1048576:.1f} MB 超过上限"}
    if file_path.suffix.lower() not in ALLOWED_SUFFIXES:
        return {"ok": False, "error": f"扩展名 {file_path.suffix} 不是 STEP（.step/.stp）"}
    if size == 0:
        return {"ok": False, "error": "文件是空的"}
    with file_path.open("rb") as handle:
        head = handle.read(4096).decode(_ENCODING, errors="ignore")
    if "ISO-10303-21" not in head.upper():
        return {"ok": False, "error": "文件头缺少 ISO-10303-21 标记"}
    return {"ok": True, "size_bytes": size, "name": file_path.name}


__all__ = [
    "ALLOWED_SUFFIXES",
    "DEFAULT_MAX_BYTES",
    "probe_step",
    "read_step",
    "read_step_bytes",
]
