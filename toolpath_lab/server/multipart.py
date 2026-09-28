"""HTTP 请求体的解析：JSON 与 multipart/form-data（文件上传）。

标准库没有 multipart 解析器，而导入 STEP 必须支持直接选文件上传，所以这里实现一个
够用的版本：

- 只解析 ``multipart/form-data``，边界由 Content-Type 给出；
- 逐个部件读出 ``name`` / ``filename`` / ``Content-Type`` 与内容；
- 对体积与部件数量设上限，畸形数据抛 :class:`ParameterError`（HTTP 400）。

不追求 RFC 全兼容：只处理本工程前端与 curl 会发出的写法（字段名可带引号、
文件名的引号与路径已处理），够用且没有任何第三方依赖。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError

#: 单个部件（文件）的体积上限，与 STEP 读取上限保持一致。
MAX_PART_BYTES = 64 * 1024 * 1024
#: 部件数量上限。
MAX_PARTS = 32

_BOUNDARY_PATTERN = re.compile(rb'boundary="?([^";,]+)"?', re.IGNORECASE)
_DISPOSITION_NAME = re.compile(r'name="([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(r'filename="([^"]*)"', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class UploadedPart:
    """一个 multipart 部件。"""

    name: str
    filename: str
    content_type: str
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def parse_content_type(header: str | None) -> tuple[str, str]:
    """把 ``Content-Type`` 拆成 (类型, 边界)。"""

    if not header:
        return "", ""
    parts = [item.strip() for item in header.split(";")]
    kind = parts[0].lower()
    boundary = ""
    for item in parts[1:]:
        if item.lower().startswith("boundary="):
            boundary = item.split("=", 1)[1].strip().strip('"')
    return kind, boundary


def parse_multipart(body: bytes, content_type: str | None,
                    *, max_parts: int = MAX_PARTS) -> dict[str, UploadedPart]:
    """解析 multipart 请求体，返回 ``字段名 -> 部件``。"""

    kind, boundary = parse_content_type(content_type)
    if kind != "multipart/form-data":
        raise ParameterError(f"期望 multipart/form-data，收到 {kind or '空'}")
    if not boundary:
        raise ParameterError("multipart 请求缺少 boundary")
    delimiter = b"--" + boundary.encode("latin-1")
    chunks = body.split(delimiter)
    if len(chunks) > max_parts + 2:
        raise ParameterError(f"multipart 部件过多（上限 {max_parts}）")

    parts: dict[str, UploadedPart] = {}
    for chunk in chunks[1:]:
        if chunk in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        # 结束标记：分隔符后面紧跟 `--`。**不能**用 `chunk.startswith(b"--")` 判断 ——
        # 浏览器生成的 boundary 本身就常带连字符（`----WebKitFormBoundaryXXXX`），
        # split 之后剩下的内容正好以 `--` 开头，会被误判成结束标记，
        # 于是整个请求"一个部件都没有"（实测 Chromium 上传就是这么失败的）。
        if chunk.startswith(b"--\r\n") or chunk.startswith(b"--\n") or chunk == b"--":
            break
        cleaned = chunk.lstrip(b"\r\n")
        header_end = cleaned.find(b"\r\n\r\n")
        if header_end < 0:
            header_end = cleaned.find(b"\n\n")
            if header_end < 0:
                continue
            raw_headers = cleaned[:header_end].decode("latin-1", errors="replace")
            content = cleaned[header_end + 2:]
        else:
            raw_headers = cleaned[:header_end].decode("latin-1", errors="replace")
            content = cleaned[header_end + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]
        if len(content) > MAX_PART_BYTES:
            raise ParameterError(
                f"上传内容 {len(content) / 1048576:.1f} MB 超过上限 "
                f"{MAX_PART_BYTES / 1048576:.0f} MB"
            )
        disposition = ""
        part_type = ""
        for line in raw_headers.splitlines():
            if line.lower().startswith("content-disposition:"):
                disposition = line.split(":", 1)[1].strip()
            elif line.lower().startswith("content-type:"):
                part_type = line.split(":", 1)[1].strip()
        name_match = _DISPOSITION_NAME.search(disposition)
        filename_match = _DISPOSITION_FILENAME.search(disposition)
        name = name_match.group(1) if name_match else ""
        filename = filename_match.group(1) if filename_match else ""
        if not name:
            continue
        parts[name] = UploadedPart(
            name=name,
            filename=filename.replace("\\", "/").split("/")[-1],
            content_type=part_type or "application/octet-stream",
            data=content,
        )
    if not parts:
        raise ParameterError("multipart 请求里没有可用的部件")
    return parts


def part_json(part: UploadedPart | None, *, field: str = "payload") -> Mapping[str, Any] | None:
    """把一个小部件当作 JSON 对象读取（前端把参数放在名为 payload 的字段里）。"""

    if part is None:
        return None
    text = part.text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ParameterError(f"字段 {field!r} 不是合法 JSON：{error}") from error
    if not isinstance(payload, Mapping):
        raise ParameterError(f"字段 {field!r} 必须是 JSON 对象")
    return payload


__all__ = [
    "MAX_PART_BYTES",
    "UploadedPart",
    "parse_content_type",
    "parse_multipart",
    "part_json",
]
