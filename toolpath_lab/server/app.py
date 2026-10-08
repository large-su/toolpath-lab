"""Standard library HTTP service: JSON API plus the static front end.

No web framework at all -- the whole service is one http.server with a route table that fits on one
screen. It listens on the loopback address only and serves static files only from toolpath_lab/web.
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
from toolpath_lab.export import toolpath_to_csv, toolpath_to_gcode
from toolpath_lab.importers import parse_dxf
from toolpath_lab.server.catalog import catalog_payload
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 4 * 1024 * 1024

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
    """A prepared HTTP response."""

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
    """API routes plus static files."""

    server_version = f"ToolpathLab/{__version__}"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[toolpath-lab] {self.address_string()} {format % args}", flush=True)

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            response = self._route(method, path)
        except (ParameterError, RegistryError, ValueError) as error:
            response = error_response(str(error), HTTPStatus.BAD_REQUEST)
        except PlanningError as error:
            response = error_response(str(error), HTTPStatus.UNPROCESSABLE_ENTITY)
        except BrokenPipeError:  # pragma: no cover - the client went away early
            return
        except Exception as error:  # pragma: no cover - last resort
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
            return self._export_gcode(self._read_json())
        if path == "/api/export/csv" and method == "POST":
            return self._export_csv(self._read_json())
        if path == "/api/import/dxf" and method == "POST":
            return self._import_dxf()
        return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)

    def _export_gcode(self, payload: Mapping[str, Any] | None) -> Response:
        result = execute_plan(PlanRequest.from_payload(payload), with_timeline=False)
        content = toolpath_to_gcode(
            result.toolpath,
            program_name="TOOLPATH_LAB",
            description="\n".join(result.request.header_lines()),
            provenance=result.result_lines(),
        )
        return text_response(
            content,
            content_type="text/plain; charset=utf-8",
            filename=self._export_filename(result.request, "nc"),
        )

    def _export_csv(self, payload: Mapping[str, Any] | None) -> Response:
        result = execute_plan(PlanRequest.from_payload(payload), with_timeline=False)
        return text_response(
            toolpath_to_csv(
                result.toolpath,
                provenance=[*result.request.header_lines(), *result.result_lines()],
            ),
            content_type="text/csv; charset=utf-8",
            filename=self._export_filename(result.request, "csv"),
        )

    def _import_dxf(self) -> Response:
        """Read a DXF drawing from the request body and answer with its outlines.

        The body is either the raw DXF text (what a file upload sends) or JSON with a "text" field
        (what a script sends). Import options (`IMPORT_PARAMETERS`, currently the chord tolerance for
        arcs) come from the same JSON object, or from the query string for the raw-text form, and are
        validated by the very declaration `/api/catalog` publishes. Nothing is stored: the caller gets
        the outlines back and passes the one it wants to /api/plan, which keeps the service stateless.
        A drawing with no usable outline is a 400 carrying the parser's own explanation.
        """

        raw = self._read_text()
        if raw is None:
            raise ParameterError("请求体是空的，请把 DXF 文件内容放在请求体里")
        values: dict[str, Any] = {}
        query = parse_qs(urlsplit(self.path).query)
        values.update({key: item[-1] for key, item in query.items() if key in _IMPORT_KEYS})
        if raw.lstrip().startswith("{"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, Mapping) and "text" in payload:
                raw = str(payload["text"])
                nested = payload.get("parameters")
                if isinstance(nested, Mapping):
                    values.update({key: value for key, value in nested.items()
                                   if key in _IMPORT_KEYS})
                values.update({key: value for key, value in payload.items()
                               if key in _IMPORT_KEYS})
        options = IMPORT_PARAMETERS.coerce(values)
        result = parse_dxf(raw, arc_tolerance_mm=float(options["arc_tolerance_mm"]))
        if not result.outlines:
            raise ParameterError("；".join(result.warnings) or "没有读到可用的闭合轮廓")
        return json_response(
            {
                "ok": True,
                "outlines": [
                    {
                        "points": [[round(x, 4), round(y, 4)] for x, y in outline.points],
                        "closed": outline.closed,
                        "layer": outline.layer,
                        "point_count": len(outline.points),
                    }
                    for outline in result.outlines
                ],
                "skipped": list(result.skipped),
                "approximated": list(result.approximated),
                "parameters": options,
                "warnings": list(result.warnings),
            }
        )

    @staticmethod
    def _export_filename(request: PlanRequest, suffix: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"toolpath_{request.planner_id}_{stamp}.{suffix}"

    # -- static files ------------------------------------------------------
    def _serve_static(self, path: str) -> Response:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        if relative == "favicon.ico":
            # Browsers and some tools request /favicon.ico directly; answer with the app's PNG icon.
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

    # -- helpers -----------------------------------------------------------
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

    def _read_text(self) -> str | None:
        """The request body as UTF-8 text (used by the DXF import, which is not JSON)."""

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            raise ParameterError(f"请求体 {length} 字节，超过 {MAX_BODY_BYTES} 字节上限")
        try:
            return self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ParameterError(f"请求体不是合法的 UTF-8 文本：{error}") from error

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
    """Create (but do not start) the HTTP server."""

    server = ThreadingHTTPServer((host, port), ToolpathLabHandler)
    server.daemon_threads = True
    return server


def serve_forever(host: str = "127.0.0.1", port: int = 8770) -> None:
    """Blocking serve loop, exits on Ctrl+C."""

    server = create_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
