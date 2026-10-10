"""标准库 HTTP 服务：JSON 接口 + 静态前端。

不依赖任何 Web 框架——整个服务就是一个 http.server，路由一屏能读完。
默认只监听回环地址，并且只从 toolpath_lab/web 目录提供静态文件。
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from toolpath_lab import __version__
from toolpath_lab.core.errors import ParameterError, PlanningError, RegistryError
from toolpath_lab.core.payload import coerce_group
from toolpath_lab.export import (
    EXPORT_FORMATS,
    toolpath_to_csv,
    toolpath_to_gcode,
    toolpath_to_json,
)
from toolpath_lab.server.catalog import catalog_payload
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 4 * 1024 * 1024
#: 请求体超限时仍然会先抽干的字节数上限；超过这个量才真的断开连接。
DRAIN_LIMIT_BYTES = 64 * 1024 * 1024

CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


@dataclass(frozen=True, slots=True)
class Response:
    """一个准备好的 HTTP 响应。"""

    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    filename: str | None = None


def json_response(payload: Any, status: int = HTTPStatus.OK) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(int(status), body)


def error_response(message: str, status: int) -> Response:
    return json_response({"ok": False, "error": message, "status": int(status)}, status)


def text_response(text: str, *, content_type: str, filename: str | None = None) -> Response:
    return Response(int(HTTPStatus.OK), text.encode("utf-8"), content_type, filename)


class ToolpathLabHandler(BaseHTTPRequestHandler):
    """接口路由 + 静态文件。"""

    server_version = f"ToolpathLab/{__version__}"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - 名字由 BaseHTTPRequestHandler 规定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[toolpath-lab] {self.address_string()} {format % args}", flush=True)

    # -- 分发 --------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            # 请求体必须**先读完再路由**，哪怕这个路由压根不关心它。
            # 否则在 HTTP/1.1 保持连接下，没被读掉的字节会留在 socket 缓冲里，
            # 被下一个请求当成开头来解析，把这条连接彻底搞乱（客户端表现为
            # ConnectionAbortedError / ConnectionResetError）。
            self._read_body()
            response = self._route(method, path)
        except (ParameterError, RegistryError, ValueError) as error:
            response = error_response(str(error), HTTPStatus.BAD_REQUEST)
        except PlanningError as error:
            response = error_response(str(error), HTTPStatus.UNPROCESSABLE_ENTITY)
        except BrokenPipeError:  # pragma: no cover - 客户端提前断开
            return
        except Exception as error:  # pragma: no cover - 兜底
            traceback.print_exc()
            response = error_response(
                f"内部错误：{type(error).__name__}: {error}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        self._send(response)

    def _route(self, method: str, path: str) -> Response:
        if path.startswith("/api/"):
            return self._route_api(method, path)
        if method != "GET":
            return error_response("方法不允许", HTTPStatus.METHOD_NOT_ALLOWED)
        return self._serve_static(path)

    def _route_api(self, method: str, path: str) -> Response:
        if path == "/api/health" and method == "GET":
            return json_response({"ok": True, "version": __version__, "service": "toolpath-lab"})
        if path == "/api/catalog" and method == "GET":
            return json_response(catalog_payload())
        if path == "/api/plan" and method == "POST":
            result = execute_plan(PlanRequest.from_payload(self._read_json()))
            return json_response(result.to_payload())
        if path == "/api/export/gcode" and method == "POST":
            return self._export(self._read_json(), "gcode")
        if path == "/api/export/csv" and method == "POST":
            return self._export(self._read_json(), "csv")
        if path == "/api/export/json" and method == "POST":
            return self._export(self._read_json(), "json")
        return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)

    def _export(self, payload: Mapping[str, Any] | None, fmt: str) -> Response:
        """按格式导出。每种格式一个分支，都是纯函数调用。"""

        if fmt not in EXPORT_FORMATS:
            raise ParameterError(f"未实现的导出格式 {fmt!r}")
        request = PlanRequest.from_payload(payload)
        result = execute_plan(request, with_timeline=False)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        extension = EXPORT_FORMATS[fmt][1]
        stem = f"toolpath_{request.planner_id}_{stamp}"

        if fmt == "gcode":
            content = toolpath_to_gcode(
                result.toolpath,
                program_name="TOOLPATH_LAB",
                description="\n".join(request.header_lines()),
            )
            return text_response(
                content,
                content_type="text/plain; charset=utf-8",
                filename=f"{stem}.nc",
            )

        if fmt == "csv":
            # 逐点刀点表。exclude_rapid 让调用方只要切削轨迹。
            exclude_rapid = bool(coerce_group(payload, "export").get("exclude_rapid", False))
            content = toolpath_to_csv(result.toolpath, include_rapid=not exclude_rapid)
            return text_response(
                content,
                content_type="text/csv; charset=utf-8",
                filename=f"{stem}.csv",
            )

        if fmt == "json":
            content = toolpath_to_json(
                result.toolpath,
                tool=request.tool,
                region=request.region,
                request=request.to_payload(),
            )
            return text_response(
                content,
                content_type="application/json; charset=utf-8",
                filename=f"{stem}.json",
            )

        # EXPORT_FORMATS 里登记了、但这里忘了写分支。
        raise ParameterError(f"导出格式 {fmt!r} 登记了但没有实现")

    # -- 静态文件 ----------------------------------------------------------
    def _serve_static(self, path: str) -> Response:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        if relative == "favicon.ico":
            # 浏览器和部分工具会直接请求 /favicon.ico，这里返回应用的 PNG 图标。
            icon = self._resolve_static("icon.png")
            if icon is None:
                return Response(int(HTTPStatus.NO_CONTENT), b"", "image/x-icon")
            return Response(int(HTTPStatus.OK), icon.read_bytes(), "image/png")
        candidate = self._resolve_static(relative)
        if candidate is None:
            return error_response(f"找不到文件：{path}", HTTPStatus.NOT_FOUND)
        content_type = CONTENT_TYPES.get(candidate.suffix.lower())
        if content_type is None:
            return error_response(f"不支持的文件类型：{candidate.suffix}", HTTPStatus.NOT_FOUND)
        return Response(int(HTTPStatus.OK), candidate.read_bytes(), content_type)

    @staticmethod
    def _resolve_static(relative: str) -> Path | None:
        root = WEB_ROOT.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        return candidate if candidate.is_file() else None

    # -- 工具 --------------------------------------------------------------
    def _read_body(self) -> bytes:
        """把请求体整个读出来，只做一次。`_dispatch` 一定会先调用它。"""

        body = getattr(self, "_body", None)
        if body is not None:
            return body
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._body = b""
            return self._body
        if length > MAX_BODY_BYTES:
            # 超过上限也要**先把数据抽干**：客户端此刻可能还在往这条连接里写，
            # 我们抢先回 400 又直接断开的话，客户端会在写的时候收到 RST，
            # 报出来的是 ConnectionAbortedError 而不是它期望的 400。
            self._drain(length)
            raise ParameterError(f"请求体 {length} 字节，超过 {MAX_BODY_BYTES} 字节上限")
        self._body = self.rfile.read(length)
        return self._body

    def _drain(self, length: int) -> None:
        """丢弃 length 字节请求体，最多读 DRAIN_LIMIT 字节。"""

        remaining = min(length, DRAIN_LIMIT_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)
        if length > DRAIN_LIMIT_BYTES:
            # 太大了，真的读不动：只能断开。
            self.close_connection = True

    def _read_json(self) -> Mapping[str, Any] | None:
        raw = self._read_body()
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ParameterError(f"请求体不是合法 JSON：{error}") from error
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise ParameterError("请求体必须是 JSON 对象")
        return payload

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        if response.filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{response.filename}"'
            )
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)


def create_server(host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
    """创建（但不启动）HTTP 服务。"""

    server = ThreadingHTTPServer((host, port), ToolpathLabHandler)
    server.daemon_threads = True
    return server


def serve_forever(host: str = "127.0.0.1", port: int = 8770) -> None:
    """阻塞式服务循环，Ctrl+C 退出。"""

    server = create_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
