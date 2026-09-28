"""标准库 HTTP 服务：JSON 接口 + 静态前端。

不依赖任何 Web 框架——整个服务就是一个 http.server，路由一屏能读完。
默认只监听回环地址，并且只从 toolpath_lab/web 目录提供静态文件。

接口分三类：

**基座（原有）**
    ``/api/health``、``/api/catalog``、``/api/plan``、``/api/export/gcode``
**CAM（新增）**
    ``/api/projects*`` 工程、``/api/import/step`` 导入、``/api/stock`` 毛坯、
    ``/api/tools*`` 刀具库、``/api/operations*`` 工序树、``/api/simulate`` 切削仿真、
    ``/api/export/nc`` 出程序
**静态**
    前端资源（``/`` 到 ``/vendor/*``）
"""

from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass, replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from toolpath_lab import __version__
from toolpath_lab.core.errors import ParameterError, PlanningError, RegistryError
from toolpath_lab.core.part import PartModel
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.server.catalog import catalog_payload
from toolpath_lab.server.multipart import MAX_PART_BYTES, parse_multipart, part_json
from toolpath_lab.brep.errors import BrepFormatError, BrepSizeError, BrepUnsupportedError
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan
from toolpath_lab.server.workspace import Workspace
from toolpath_lab.simulation.cut_sim import simulate_toolpath
from toolpath_lab.storage.repository import ProjectRepository

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 4 * 1024 * 1024
#: 上传 STEP 时允许的请求体上限（比普通 JSON 大得多）。
MAX_UPLOAD_BYTES = MAX_PART_BYTES + 1024 * 1024
#: 出错时最多从 socket 里丢弃多少字节（避免被恶意的超大请求卡住）。
MAX_DISCARD_BYTES = 8 * 1024 * 1024

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

#: 工程数据目录：默认放在用户数据目录，可用环境变量覆盖（测试与打包用）。
DEFAULT_DATA_DIR_NAME = "ToolpathLab"


def default_data_dir() -> Path:
    override = os.environ.get("TOOLPATH_LAB_DATA")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / DEFAULT_DATA_DIR_NAME / "projects"
    return Path.home() / f".{DEFAULT_DATA_DIR_NAME.lower()}" / "projects"


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
    #: 由 :func:`create_server` 注入
    workspace: Workspace

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # 请求级状态：每个请求开始时由 handle_one_request 重置（handler 按连接复用）
        self._json_cache: Mapping[str, Any] | None = None
        #: 本次请求的 body 是否已经"读完或丢掉"（见 _read_body / _drain_body）
        self._body_consumed = False
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - 名字由 BaseHTTPRequestHandler 规定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def handle_one_request(self) -> None:
        # 关键：handler 实例是**按连接**复用的，不是按请求新建的。
        # 所以请求级状态必须在这里清空——否则第二个请求会拿到上一个请求的 JSON 缓存
        #（参数错位），或者以为 body 已经读过而漏读（连接错位）。
        self._json_cache = None
        self._body_consumed = False
        super().handle_one_request()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[toolpath-lab] {self.address_string()} {format % args}", flush=True)

    # -- 分发 --------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        try:
            response = self._route(method, path, query)
        except (ParameterError, RegistryError, ValueError, BrepFormatError) as error:
            response = error_response(str(error), HTTPStatus.BAD_REQUEST)
        except BrepSizeError as error:
            response = error_response(str(error), HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except (BrepUnsupportedError, PlanningError) as error:
            response = error_response(str(error), HTTPStatus.UNPROCESSABLE_ENTITY)
        except BrokenPipeError:  # pragma: no cover - 客户端提前断开
            return
        except Exception as error:  # pragma: no cover - 兜底
            traceback.print_exc()
            response = error_response(
                f"内部错误：{type(error).__name__}: {error}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        finally:
            # 不管走哪条分支、有没有抛异常，都必须把没读过的请求体吃掉：
            # HTTP/1.1 是持久连接，残留字节会被当成**下一个请求的请求行**。
            # 生成刀路的 POST 只带一个 `{}`（路由里用不到 body），一旦漏读，下一个请求
            # 就变成 `{}POST /api/import/step`，服务端回 501，用户看到的是"导入失败"。
            self._drain_body()
        self._send(response)

    def _route(self, method: str, path: str, query: Mapping[str, list[str]]) -> Response:
        if path.startswith("/api/"):
            return self._route_api(method, path, query)
        if method != "GET":
            return error_response("方法不允许", HTTPStatus.METHOD_NOT_ALLOWED)
        return self._serve_static(path)

    def _route_api(self, method: str, path: str, query: Mapping[str, list[str]]) -> Response:
        # -- 基座 ----------------------------------------------------------
        if path == "/api/health" and method == "GET":
            return json_response({
                "ok": True, "version": __version__, "service": "toolpath-lab",
                "workspace": self.workspace.project is not None,
            })
        if path == "/api/catalog" and method == "GET":
            return json_response(catalog_payload())
        if path == "/api/plan" and method == "POST":
            result = execute_plan(PlanRequest.from_payload(self._read_json()))
            return json_response(result.to_payload())
        if path == "/api/export/gcode" and method == "POST":
            return self._export_gcode(self._read_json())

        # -- 工程 ----------------------------------------------------------
        if path == "/api/projects" and method == "GET":
            return json_response({
                "ok": True,
                "projects": self.workspace.repository.index(),
                "current": None if self.workspace.project is None
                else self.workspace.project.summary(),
            })
        if path == "/api/projects/open" and method == "POST":
            payload = self._read_json() or {}
            project = self.workspace.open(str(payload.get("id") or ""))
            return json_response({"ok": True, "project": project.to_payload()})
        if path == "/api/projects/current" and method == "DELETE":
            self.workspace.close()
            return json_response({"ok": True})
        if path.startswith("/api/projects/") and method == "DELETE":
            project_id = path[len("/api/projects/"):]
            removed = self.workspace.repository.delete(unquote(project_id))
            if self.workspace.project is not None and self.workspace.project.project_id == project_id:
                self.workspace.close()
            return json_response({"ok": removed, "id": project_id})

        # -- 导入 ----------------------------------------------------------
        if path == "/api/import/step" and method == "POST":
            return self._import_step()

        # -- 模型与特征 ----------------------------------------------------
        if path == "/api/model" and method == "GET":
            project = self.workspace.require_project()
            return json_response({"ok": True, "project": project.to_payload()})
        if path == "/api/model/features" and method == "GET":
            from toolpath_lab.cam.boundary import planar_features

            project = self.workspace.require_project()
            return json_response({
                "ok": True,
                "features": planar_features(project.part),
                "faces": [face.to_payload() for face in project.part.mesh.faces],
            })

        # -- 毛坯 ----------------------------------------------------------
        if path == "/api/stock" and method == "GET":
            return json_response({"ok": True, **self.workspace.stock_payload()})
        if path == "/api/stock" and method == "POST":
            payload = self._read_json() or {}
            stock_id = str(payload.get("shape") or payload.get("id") or "rectangular")
            parameters = payload.get("parameters")
            if not isinstance(parameters, Mapping):
                parameters = {key: value for key, value in payload.items()
                              if key not in {"shape", "id"}}
            self.workspace.set_stock(stock_id, parameters)
            return json_response({"ok": True, **self.workspace.stock_payload()})

        # -- 参数 ----------------------------------------------------------
        if path == "/api/parameters" and method == "GET":
            return json_response({"ok": True, **self.workspace.parameters_payload()})
        if path == "/api/parameters" and method == "POST":
            payload = self._read_json() or {}
            cam = payload.get("cam") if isinstance(payload.get("cam"), Mapping) else None
            controller = payload.get("controller") if isinstance(payload.get("controller"), Mapping) else None
            self.workspace.set_parameters(cam=cam, controller=controller)
            return json_response({"ok": True, **self.workspace.parameters_payload()})

        # -- 刀具库 --------------------------------------------------------
        if path == "/api/tools" and method == "GET":
            return json_response({"ok": True, **self.workspace.tool_payload()})
        if path == "/api/tools" and method == "POST":
            payload = self._read_json() or {}
            values = payload.get("values")
            tool = self.workspace.create_tool(
                str(payload.get("name") or ""),
                str(payload.get("kind") or "flat_end_mill"),
                values if isinstance(values, Mapping) else {},
                note=str(payload.get("note") or ""),
            )
            return json_response({"ok": True, "tool": tool})
        if path == "/api/tools/restore" and method == "POST":
            self.workspace.require_tools().restore_defaults()
            return json_response({"ok": True, **self.workspace.tool_payload()})
        if path.startswith("/api/tools/"):
            return self._route_tool(method, path)

        # -- 工序树 --------------------------------------------------------
        if path == "/api/operations" and method == "GET":
            project = self.workspace.require_project()
            return json_response({"ok": True, **project.tree.to_payload()})
        if path == "/api/operations" and method == "POST":
            payload = self._read_json() or {}
            operation, result = self.workspace.add_operation(
                kind=str(payload.get("kind") or "pocket_mill"),
                face_ids=_face_ids(payload),
                parameters=payload.get("parameters") if isinstance(payload.get("parameters"), Mapping) else None,
                name=str(payload.get("name") or ""),
            )
            response: dict[str, Any] = {"ok": True, "operation": operation.to_payload()}
            if result is not None:
                response["result"] = result.to_payload()
            return json_response(response)
        if path == "/api/operations/generate" and method == "POST":
            return json_response({"ok": True, "results": self.workspace.generate_all()})
        if path.startswith("/api/operations/"):
            return self._route_operation(method, path)

        # -- 参数模板 ------------------------------------------------------
        if path == "/api/templates" and method == "POST":
            payload = self._read_json() or {}
            template = self.workspace.save_template(
                str(payload.get("name") or ""),
                str(payload.get("kind") or "pocket_mill"),
                payload.get("parameters") if isinstance(payload.get("parameters"), Mapping) else {},
            )
            return json_response({"ok": True, "template": template.to_payload()})
        if path.startswith("/api/templates/") and method == "DELETE":
            self.workspace.remove_template(unquote(path[len("/api/templates/"):]))
            return json_response({"ok": True})

        # -- 仿真 ----------------------------------------------------------
        if path == "/api/simulate" and method == "POST":
            return self._simulate()

        # -- 导出 ----------------------------------------------------------
        if path == "/api/export/nc" and method == "POST":
            return self._export_nc()

        return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)

    def _route_tool(self, method: str, path: str) -> Response:
        """``/api/tools/<id>`` 与 ``/api/tools/<id>/duplicate``。"""

        segments = [item for item in path[len("/api/tools/"):].split("/") if item]
        if not segments:
            return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)
        tool_id = unquote(segments[0])
        action = segments[1] if len(segments) > 1 else ""
        if method == "GET" and action == "":
            record = self.workspace.require_tools().get(tool_id)
            return json_response({
                "ok": True,
                "tool": record.to_payload(),
                "used_by": self.workspace.operations_using_tool(tool_id),
            })
        if method == "POST" and action == "duplicate":
            payload = self._read_json() or {}
            tool = self.workspace.duplicate_tool(tool_id, name=str(payload.get("name") or ""))
            return json_response({"ok": True, "tool": tool})
        if method == "POST" and action == "":
            payload = self._read_json() or {}
            changes: dict[str, Any] = {}
            if "name" in payload:
                changes["name"] = str(payload["name"])
            if "kind" in payload:
                changes["kind"] = str(payload["kind"])
            if "note" in payload:
                changes["note"] = str(payload["note"])
            if "values" in payload and isinstance(payload["values"], Mapping):
                changes["values"] = payload["values"]
            tool = self.workspace.update_tool(tool_id, **changes)
            return json_response({
                "ok": True, "tool": tool,
                "used_by": self.workspace.operations_using_tool(tool_id),
            })
        if method == "DELETE" and action == "":
            result = self.workspace.delete_tool(tool_id)
            return json_response({"ok": True, **result})
        return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)

    def _route_operation(self, method: str, path: str) -> Response:
        segments = [item for item in path[len("/api/operations/"):].split("/") if item]
        if not segments:
            return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)
        operation_id = unquote(segments[0])
        action = segments[1] if len(segments) > 1 else ""
        if method == "POST" and action == "generate":
            result = self.workspace.generate(operation_id)
            return json_response({"ok": True, "result": result.to_payload()})
        if method == "POST" and action == "duplicate":
            operation = self.workspace.duplicate_operation(operation_id)
            return json_response({"ok": True, "operation": operation.to_payload()})
        if method == "POST" and action == "move":
            payload = self._read_json() or {}
            ordered = self.workspace.move_operation(operation_id, int(payload.get("sequence") or 0))
            return json_response({"ok": True, "operations": [item.to_payload() for item in ordered]})
        if method == "POST" and action == "":
            payload = self._read_json() or {}
            changes: dict[str, Any] = {}
            if "name" in payload:
                changes["name"] = str(payload["name"])
            if "enabled" in payload:
                changes["enabled"] = bool(payload["enabled"])
            if "kind" in payload:
                changes["kind"] = str(payload["kind"])
            if "parameters" in payload and isinstance(payload["parameters"], Mapping):
                changes["parameters"] = payload["parameters"]
            if "faces" in payload or "face_ids" in payload:
                changes["face_ids"] = _face_ids(payload)
            operation = self.workspace.update_operation(operation_id, **changes)
            return json_response({"ok": True, "operation": operation.to_payload()})
        if method == "DELETE" and action == "":
            self.workspace.remove_operation(operation_id)
            return json_response({"ok": True})
        return error_response(f"未知接口 {path}", HTTPStatus.NOT_FOUND)

    # -- 具体动作 ----------------------------------------------------------
    def _import_step(self) -> Response:
        # 请求体按"上传上限"读一次；multipart 时它同时是文件内容，JSON 时由解析器缓存复用
        content_type = self.headers.get("Content-Type") or ""
        lowered = content_type.lower()
        if lowered.startswith("multipart/form-data"):
            body = self._read_body(MAX_UPLOAD_BYTES)
            parts = parse_multipart(body, content_type)
            uploaded = None
            for key in ("file", "step", "model", "upload"):
                if key in parts:
                    uploaded = parts[key]
                    break
            if uploaded is None:
                uploaded = next((item for item in parts.values() if item.filename), None)
            if uploaded is None:
                raise ParameterError("multipart 请求里没有文件部件")
            name = part_json(parts.get("payload")) or {}
            project = self.workspace.import_step(
                uploaded.data, filename=uploaded.filename, name=str(name.get("name") or "")
            )
        else:
            if lowered and "json" not in lowered:
                raise ParameterError(
                    "STEP 导入请使用 multipart/form-data 上传文件，"
                    "或用 JSON 传 content（STEP 文本/base64）"
                )
            payload = self._read_json() or {}
            raw = payload.get("content") or payload.get("data")
            if not isinstance(raw, str) or not raw.strip():
                raise ParameterError("请提供 STEP 内容（multipart 文件或 JSON 的 content 字段）")
            project = self.workspace.import_step(
                _decode_step_text(raw), filename=str(payload.get("filename") or "model.step"),
                name=str(payload.get("name") or ""),
            )
        return json_response({"ok": True, "project": project.to_payload()})

    def _simulate(self) -> Response:
        from toolpath_lab.core.path import Toolpath

        payload = self._read_json() or {}
        project = self.workspace.require_project()
        operation_id = payload.get("operation_id")
        if operation_id:
            result = self.workspace.results_for(str(operation_id))
            toolpath = result.toolpath
            tool_radius = result.request.tool.radius_mm
            parameters = result.request.parameters
            kind = result.request.kind
        elif payload.get("kind"):
            # 直接给加工类型与面：先规划再仿真（界面上"改参数立刻看仿真"走这条路）
            from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

            request = CAMOperationRequest.from_payload(
                payload, project.part, tool_resolver=self.workspace.resolve_tool
            )
            # 只补上工作空间才知道的东西（毛坯、BRep）：等高铣在仿真路径上同样要剖切 BRep。
            request = replace(request, stock=project.stock(), brep=self.workspace.brep)
            result = execute_operation(request)
            toolpath = result.toolpath
            tool_radius = result.request.tool.radius_mm
            parameters = result.request.parameters
            kind = result.request.kind
        else:
            # 不指定工序：把全部启用的工序按顺序拼成一条完整加工过程
            operations = project.tree.enabled_operations()
            if not operations:
                raise ParameterError("没有可仿真的工序：请先生成刀路")
            moves = []
            last_radius = None
            for operation in operations:
                generated = self.workspace.results_for(operation.operation_id)
                moves.extend(generated.toolpath.moves)
                last_radius = generated.request.tool.radius_mm
                parameters = generated.request.parameters
            toolpath = Toolpath(
                moves=tuple(moves), planner="full_job", planner_label="全部工序",
                notes=tuple(f"共 {len(operations)} 道工序"),
            )
            tool_radius = float(last_radius or 5.0)
            kind = "full_job"

        stock = project.stock()
        target_volume = None
        if bool(payload.get("compare_part", True)):
            target_volume = _part_volume(project.part)
        # 没显式给分辨率就按毛坯大小自适应，避免大零件上跑出几十万格的栅格
        from toolpath_lab.cam.boundary import adaptive_cell_mm

        requested_cell = payload.get("cell_mm")
        if requested_cell:
            cell_mm = float(requested_cell)
        else:
            bounds = stock.bounds
            cell_mm = adaptive_cell_mm(max(bounds.size[0], bounds.size[1]))
        simulation = simulate_toolpath(
            toolpath,
            stock,
            tool_radius=tool_radius,
            cell_mm=cell_mm,
            max_frames=int(payload.get("max_frames") or 120),
            sample_mm=float(payload.get("sample_mm") or 0.0),
            target_volume_mm3=target_volume,
        )
        frames = simulation.frames
        payload_out: dict[str, Any] = {
            "ok": True,
            "kind": kind,
            "tool": {"radius_mm": tool_radius, "diameter_mm": tool_radius * 2},
            "parameters": dict(parameters),
            "regions": [] if not operation_id else [dict(item) for item in result.regions],
            "toolpath": toolpath.to_payload(),
            "grid": simulation.grid,
            "stock": simulation.stock,
            "summary": simulation.summary(),
            "frames": [frame.to_payload() for frame in frames],
            "final_mesh": simulation.final.surface_mesh().to_payload(decimals=3),
        }
        return json_response(payload_out)

    def _export_nc(self) -> Response:
        from toolpath_lab.export import ProgramHeader, program_from_operations

        payload = self._read_json() or {}
        project = self.workspace.require_project()
        operation_id = payload.get("operation_id")
        controller = dict(project.controller_parameters)
        if isinstance(payload.get("controller"), Mapping):
            controller.update(payload["controller"])

        entries: list[tuple[str, Any, dict[str, Any]]] = []
        if operation_id:
            result = self.workspace.results_for(str(operation_id))
            operation = project.tree.get(str(operation_id))
            entries.append((operation.name, result.toolpath, dict(operation.parameters)))
            tool = result.request.tool
            spindle = operation.parameters.get("spindle_rpm", controller.get("spindle_rpm", 3000))
            direction = operation.parameters.get("spindle_direction", "cw")
            coolant = operation.parameters.get("coolant", "flood")
        else:
            for operation in project.tree.enabled_operations():
                result = self.workspace.results_for(operation.operation_id)
                entries.append((operation.name, result.toolpath, dict(operation.parameters)))
            if not entries:
                raise ParameterError("没有可导出的启用工序")
            tool = entries and self.workspace.results_for(
                project.tree.enabled_operations()[0].operation_id
            ).request.tool
            first = project.tree.enabled_operations()[0]
            spindle = first.parameters.get("spindle_rpm", 3000)
            direction = first.parameters.get("spindle_direction", "cw")
            coolant = first.parameters.get("coolant", "flood")

        header = ProgramHeader(
            program_name=project.name or "TOOLPATH_LAB",
            program_number=int(controller.get("program_number") or 1000),
            work_offset=str(controller.get("work_offset") or "g54").upper(),
            spindle_rpm=float(spindle),
            spindle_direction=str(direction),
            coolant=str(coolant),
            tool=tool,
            notes=(f"part: {project.part.name}", f"stock: {project.stock_id}"),
            output_comments=bool(controller.get("output_comments", True)),
        )
        content = program_from_operations(entries, header)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (project.name or "job"))
        return text_response(
            content,
            content_type="text/plain; charset=utf-8",
            filename=f"{safe_name}_{stamp}.nc",
        )

    def _export_gcode(self, payload: Mapping[str, Any] | None) -> Response:
        result = execute_plan(PlanRequest.from_payload(payload), with_timeline=False)
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
    def _read_body(self, limit: int) -> bytes:
        self._body_consumed = True
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ParameterError("请求体为空")
        if length > limit:
            # 超限时**不能**直接返回：请求体还留在 socket 里，HTTP/1.1 的持久连接
            # 会把剩下的字节当成下一个请求的开头（表现为客户端莫名超时）。
            # 要么读干净，要么明确关掉这条连接。
            self._discard_body(length)
            self.close_connection = True
            raise ParameterError(
                f"请求体 {length / 1048576:.1f} MB 超过上限 {limit / 1048576:.1f} MB"
            )
        return self.rfile.read(length)

    def _drain_body(self) -> None:
        """把本次请求还没读过的 body 读完丢掉，保证持久连接停在正确的位置。

        只有一部分路由需要 body（例如"生成刀路"的 POST 只发一个 `{}`），
        让每个处理函数都记得读一遍太容易漏；统一在这里兜底。
        """

        if self._body_consumed:
            return
        self._body_consumed = True
        if self.headers.get("Transfer-Encoding"):
            # 分块编码没有 Content-Length，没法安全跳过：断开连接比读串了强。
            self.close_connection = True
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return
        if length > MAX_DISCARD_BYTES:
            # 超大 body 不读，直接断开这条连接（关掉连接不会污染下一个请求）。
            self.close_connection = True
            return
        try:
            self._discard_body(length)
        except OSError:  # pragma: no cover - 客户端提前断开
            self.close_connection = True

    def _discard_body(self, length: int, chunk: int = 65536) -> None:
        """把请求体读完丢掉（受上限保护，避免被超大请求拖住）。"""

        remaining = min(int(length), MAX_DISCARD_BYTES)
        while remaining > 0:
            piece = self.rfile.read(min(chunk, remaining))
            if not piece:
                break
            remaining -= len(piece)

    def _read_json(self) -> Mapping[str, Any] | None:
        # 请求体只能读一次（读第二遍会拿到空字符串）。导入接口要先按"上传上限"读一遍
        # 再做 multipart / JSON 判断，因此这里把结果缓存下来：同一个请求里重复调用
        # 只会读一次 socket。
        if getattr(self, "_json_cache", None) is not None:
            return self._json_cache
        raw = self._read_body(MAX_BODY_BYTES)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ParameterError(f"请求体不是合法 JSON：{error}") from error
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise ParameterError("请求体必须是 JSON 对象")
        self._json_cache = payload
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


def _face_ids(payload: Mapping[str, Any]) -> list[int]:
    raw = payload.get("faces") or payload.get("face_ids") or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    return [int(item) for item in raw]


def _decode_step_text(text: str) -> bytes:
    """JSON 里的 STEP 文本：可能是 base64，也可能是直接粘贴的文本。"""

    import base64
    import binascii

    stripped = text.strip()
    if stripped.upper().startswith("ISO-10303-21") or "\n" in stripped and "#" in stripped:
        return stripped.encode("latin-1", errors="replace")
    try:
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        return stripped.encode("latin-1", errors="replace")


def _part_volume(part: PartModel) -> float | None:
    """零件体积（用于仿真后的对比提示）。"""

    volume = part.mesh.volume_mm3()
    if volume <= 0:
        return None
    return abs(volume)


def create_server(host: str = "127.0.0.1", port: int = 8770,
                  *, data_dir: str | Path | None = None) -> ThreadingHTTPServer:
    """创建（但不启动）HTTP 服务，并装配工作空间。"""

    repository = ProjectRepository(data_dir or default_data_dir())
    workspace = Workspace(repository=repository)
    # 启动时恢复最近一次打开的工程（如果有）
    try:
        items = repository.index()
        if items:
            latest = str(items[0].get("id"))
            if latest and repository.exists(latest):
                workspace.open(latest)
    except Exception:  # pragma: no cover - 恢复失败不影响启动
        traceback.print_exc()
    server = ThreadingHTTPServer((host, port), ToolpathLabHandler)
    server.daemon_threads = True
    server.workspace = workspace  # type: ignore[attr-defined]
    ToolpathLabHandler.workspace = workspace
    return server


def serve_forever(host: str = "127.0.0.1", port: int = 8770) -> None:
    """阻塞式服务循环，Ctrl+C 退出。"""

    server = create_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
