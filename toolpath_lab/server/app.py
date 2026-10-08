"""标准库 HTTP 服务：JSON 接口 + 静态前端。

不依赖任何 Web 框架——整个服务就是一个 http.server，路由一屏能读完。
默认只监听回环地址，并且只从 toolpath_lab/web 目录提供静态文件。

导入的模型（STL）保存在服务实例的内存模型库里：进程重启即清空，
所以没有额外的存储依赖，也不需要清理磁盘。

接口一览：

    GET    /api/health            健康检查
    GET    /api/catalog           能力目录（含已导入的模型列表）
    POST   /api/plan              生成刀路
    POST   /api/export/gcode      导出 NC 程序
    GET    /api/models            已导入的模型列表
    POST   /api/models            上传模型（{"name": "...", "data_base64": "..."}）
    GET    /api/models/{id}       模型详情（含三维显示用的三角形坐标）
    DELETE /api/models/{id}       删除模型
"""

from __future__ import annotations

import base64
import binascii
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
from toolpath_lab.core.mesh import ModelLibrary, model_library_payload
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.server.catalog import catalog_payload
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 32 * 1024 * 1024

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
    ".stl": "model/stl",
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


class ToolpathLabServer(ThreadingHTTPServer):
    """带模型库的 HTTP 服务：导入的模型随服务实例一起存在内存里。"""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self.models = ModelLibrary()


class ToolpathLabHandler(BaseHTTPRequestHandler):
    """接口路由 + 静态文件。"""

    server_version = f"ToolpathLab/{__version__}"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - 名字由 BaseHTTPRequestHandler 规定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[toolpath-lab] {self.address_string()} {format % args}", flush=True)

    # -- 分发 --------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
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

    @property
    def models(self) -> ModelLibrary:
        """当前服务实例的模型库。

        直接构造 BaseHTTPRequestHandler 的用法（例如自建 server）也能工作：
        第一次访问时补一个空的模型库。
        """

        library = getattr(self.server, "models", None)
        if library is None:
            library = ModelLibrary()
            self.server.models = library  # type: ignore[attr-defined]
        return library

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
            return json_response(catalog_payload(self.models))
        if path == "/api/plan" and method == "POST":
            result = execute_plan(
                PlanRequest.from_payload(self._read_json(), models=self.models)
            )
            return json_response(result.to_payload())
        if path == "/api/export/gcode" and method == "POST":
            return self._export_gcode(self._read_json())

        if path == "/api/models":
            if method == "GET":
                return json_response(model_library_payload(self.models))
            if method == "POST":
                return self._upload_model(self._read_json())
            return error_response("方法不允许", HTTPStatus.METHOD_NOT_ALLOWED)
        if path.startswith("/api/models/"):
            model_id = path[len("/api/models/"):]
            if method == "GET":
                return self._model_detail(model_id)
            if method == "DELETE":
                return self._delete_model(model_id)
            return error_response("方法不允许", HTTPStatus.METHOD_NOT_ALLOWED)

        return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)

    # -- 模型 --------------------------------------------------------------
    def _upload_model(self, payload: Mapping[str, Any] | None) -> Response:
        """上传一个 STL：{"name": "part.stl", "data_base64": "..."}。

        用 base64 塞进 JSON，而不是 multipart：解析只需两行，命令行与脚本也更好拼。
        """

        data = payload or {}
        name = str(data.get("name") or "model.stl").strip() or "model.stl"
        encoded = data.get("data_base64")
        if not isinstance(encoded, str) or not encoded.strip():
            raise ParameterError("上传模型需要 data_base64 字段（STL 文件的 base64 内容）")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ParameterError(f"data_base64 不是合法的 base64：{error}") from error
        if not raw:
            raise ParameterError("模型内容是空的")
        model = self.models.add(name, raw)
        return json_response({"ok": True, "model": model.describe()}, HTTPStatus.CREATED)

    def _model_detail(self, model_id: str) -> Response:
        model = self.models.get(model_id)
        return json_response({"ok": True, "model": {**model.describe(), **model.mesh_payload()}})

    def _delete_model(self, model_id: str) -> Response:
        if not self.models.remove(model_id):
            return error_response(f"没有这个模型 {model_id!r}", HTTPStatus.NOT_FOUND)
        return json_response({"ok": True, "deleted": model_id})

    def _export_gcode(self, payload: Mapping[str, Any] | None) -> Response:
        result = execute_plan(
            PlanRequest.from_payload(payload, models=self.models), with_timeline=False
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        content = toolpath_to_gcode(
            result.toolpath,
            program_name="TOOLPATH_LAB",
            description="\n".join(result.request.header_lines()),
        )
        return text_response(
            content,
            content_type="text/plain; charset=utf-8",
            filename=f"toolpath_{result.request.planner_id}_{stamp}.nc",
        )

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
    def _read_json(self) -> Mapping[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            raise ParameterError(f"请求体 {length} 字节，超过 {MAX_BODY_BYTES} 字节上限")
        raw = self.rfile.read(length)
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


def create_server(host: str = "127.0.0.1", port: int = 8770) -> ToolpathLabServer:
    """创建（但不启动）HTTP 服务；返回的服务带一个空的模型库。"""

    return ToolpathLabServer((host, port), ToolpathLabHandler)


def serve_forever(host: str = "127.0.0.1", port: int = 8770) -> None:
    """阻塞式服务循环，Ctrl+C 退出。"""

    server = create_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
