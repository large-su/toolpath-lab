"""刀具库的 HTTP 接口：增删改查、与 CAM 工序的打通。

服务实例与数据目录都是每个类独立的，所以刀库（``<data_dir>/tools/tools.json``）
不会互相污染，也不会碰到用户目录里真正的刀库。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tests.fixtures import solid_bytes
from toolpath_lab.server.app import create_server

_BOUNDARY = "----toolpathlabtools"


def http(base: str, method: str, path: str, *, payload: Any = None,
         raw: bytes | None = None, content_type: str | None = "application/json"
         ) -> tuple[int, bytes, dict[str, str]]:
    data = raw
    if data is None and payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if content_type and data is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def get_json(base: str, path: str) -> tuple[int, dict]:
    status, body, _ = http(base, "GET", path)
    return status, json.loads(body)


def post_json(base: str, path: str, payload: Any = None) -> tuple[int, dict]:
    status, body, _ = http(base, "POST", path, payload=payload if payload is not None else {})
    return status, json.loads(body)


def upload(base: str, content: bytes, filename: str = "plate.step") -> tuple[int, dict]:
    body = (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/step\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{_BOUNDARY}--\r\n".encode("utf-8")
    status, raw, _ = http(base, "POST", "/api/import/step", raw=body,
                          content_type=f"multipart/form-data; boundary={_BOUNDARY}")
    return status, json.loads(raw)


class ToolServerCase(unittest.TestCase):
    """自带服务与数据目录的测试基类。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = tempfile.TemporaryDirectory(prefix="tplab-tools-api-")
        cls.server = create_server("127.0.0.1", 0, data_dir=Path(cls.data_dir.name))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.data_dir.cleanup()

    def get(self, path: str):
        return get_json(self.base, path)

    def post(self, path: str, payload: Any = None):
        return post_json(self.base, path, payload)

    def send(self, method: str, path: str, **kwargs):
        """发任意方法的请求；返回 ``(status, body_bytes, headers)``。"""

        return http(self.base, method, path, **kwargs)

    def send_json(self, method: str, path: str, **kwargs):
        """同上，但把响应体解成 JSON。"""

        status, body, _ = http(self.base, method, path, **kwargs)
        return status, json.loads(body)

class ToolLibraryApiTests(ToolServerCase):
    def test_list_returns_seeded_tools_and_the_type_catalog(self) -> None:
        status, body = self.get("/api/tools")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["tools"])
        types = [item["id"] for item in body["catalog"]["types"]]
        self.assertIn("flat_end_mill", types)
        self.assertIn("drill", types)
        flat = next(item for item in body["tools"] if item["id"] == "tool-flat-d10")
        self.assertIn("平底刀", flat["kind_label"])
        self.assertAlmostEqual(flat["tool"]["diameter_mm"], 10.0)

    def test_catalog_publishes_the_same_tool_types(self) -> None:
        status, catalog = self.get("/api/catalog")
        self.assertEqual(status, 200)
        self.assertIn("tool_library", catalog)
        ids = [item["id"] for item in catalog["tool_library"]["types"]]
        _, tools = self.get("/api/tools")
        self.assertEqual(ids, [item["id"] for item in tools["catalog"]["types"]])

    def test_create_then_read_back(self) -> None:
        status, body = self.post("/api/tools", {
            "name": "D12R2 圆鼻刀",
            "kind": "bull_nose_mill",
            "values": {"diameter_mm": 12.0, "corner_radius_mm": 99.0,
                       "flute_length_mm": 30.0, "length_mm": 70.0},
            "note": "开粗后的过渡刀",
        })
        self.assertEqual(status, 200, body)
        tool_id = body["tool"]["id"]
        # 圆角半径被限到刀具半径
        self.assertAlmostEqual(body["tool"]["values"]["corner_radius_mm"], 6.0)
        self.assertEqual(body["tool"]["note"], "开粗后的过渡刀")

        status, detail = self.get(f"/api/tools/{tool_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["tool"]["name"], "D12R2 圆鼻刀")
        self.assertEqual(detail["used_by"], [])

        # 重启服务（同一个数据目录）后刀具还在
        again = create_server("127.0.0.1", 0, data_dir=Path(self.data_dir.name))
        try:
            self.assertTrue(again.workspace.require_tools().exists(tool_id))
        finally:
            again.server_close()

    def test_duplicate_then_update_then_delete(self) -> None:
        status, body = self.post("/api/tools/tool-flat-d10/duplicate", {"name": "D10 精铣刀"})
        self.assertEqual(status, 200, body)
        clone_id = body["tool"]["id"]
        self.assertNotEqual(clone_id, "tool-flat-d10")

        status, body = self.post(f"/api/tools/{clone_id}",
                                 {"name": "D10 精铣刀 v2", "values": {"diameter_mm": 9.5}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["tool"]["name"], "D10 精铣刀 v2")
        self.assertAlmostEqual(body["tool"]["values"]["diameter_mm"], 9.5)

        status, body = self.send_json("DELETE", f"/api/tools/{clone_id}")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        status, _ = self.get(f"/api/tools/{clone_id}")
        self.assertEqual(status, 400)

    def test_missing_name_is_rejected(self) -> None:
        status, body = self.post("/api/tools", {"kind": "drill", "values": {}})
        self.assertEqual(status, 400)
        self.assertIn("名称", body["error"])

    def test_unknown_type_is_rejected(self) -> None:
        status, body = self.post("/api/tools", {"name": "激光刀", "kind": "laser"})
        self.assertEqual(status, 400)
        self.assertIn("刀具类型", body["error"])

    def test_out_of_range_parameter_is_rejected(self) -> None:
        status, body = self.post("/api/tools", {
            "name": "怪刀", "kind": "flat_end_mill", "values": {"diameter_mm": 0.01},
        })
        self.assertEqual(status, 400)
        self.assertIn("diameter_mm", body["error"])

    def test_restore_defaults_brings_back_a_deleted_tool(self) -> None:
        self.send_json("DELETE", "/api/tools/tool-flat-d6")
        status, _ = self.get("/api/tools/tool-flat-d6")
        self.assertEqual(status, 400)
        status, body = self.post("/api/tools/restore", {})
        self.assertEqual(status, 200)
        self.assertIn("tool-flat-d6", {item["id"] for item in body["tools"]})


class ToolOperationIntegrationTests(ToolServerCase):
    """在 CAM 刀路编程里选用刀具库的刀：刀路与仿真都按那把刀算。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        status, _ = upload(cls.base, solid_bytes("plate"))
        assert status == 200
        _, features = get_json(cls.base, "/api/model/features")
        cls.floor = [item for item in features["features"]
                     if item["horizontal"] and abs(item["plane"][3] - 25.0) < 1e-6][0]
        _, cls.tools = get_json(cls.base, "/api/tools")

    def _add_operation(self, tool_id: str, **parameters):
        payload = {
            "kind": "pocket_mill",
            "faces": [self.floor["face_id"]],
            "parameters": {"tool_id": tool_id, "stepover_ratio": 0.6, **parameters},
        }
        return self.post("/api/operations", payload)

    def test_operation_uses_the_library_tool_geometry(self) -> None:
        status, body = self._add_operation("tool-flat-d10")
        self.assertEqual(status, 200, body)
        operation = body["operation"]
        self.assertEqual(operation["parameters"]["tool_id"], "tool-flat-d10")
        self.assertAlmostEqual(operation["parameters"]["tool_diameter_mm"], 10.0)
        result = body["result"]
        self.assertAlmostEqual(result["tool"]["diameter_mm"], 10.0)
        self.assertAlmostEqual(result["tool"]["radius_mm"], 5.0)
        self.assertGreater(result["toolpath"]["statistics"]["cut_length_mm"], 0)

    def test_client_side_diameter_cannot_override_the_library_tool(self) -> None:
        """以刀具库为准：界面传的旧直径只当噪声丢掉。"""

        status, body = self._add_operation("tool-flat-d10", tool_diameter_mm=1.0)
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["operation"]["parameters"]["tool_diameter_mm"], 10.0)
        self.assertAlmostEqual(body["result"]["tool"]["diameter_mm"], 10.0)

    def test_unknown_tool_id_is_rejected_before_planning(self) -> None:
        status, body = self._add_operation("tool-does-not-exist")
        self.assertEqual(status, 400)
        self.assertIn("刀具库", body["error"])

    def test_editing_a_tool_changes_the_next_regenerated_toolpath(self) -> None:
        """改刀具直径 → 用同一道工序重新生成，刀路必须跟着变。"""

        status, body = self._add_operation("tool-flat-d6")
        self.assertEqual(status, 200, body)
        operation_id = body["operation"]["id"]
        before = body["result"]["tool"]["diameter_mm"]

        status, body = self.post("/api/tools/tool-flat-d6", {"values": {"diameter_mm": 3.0}})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["tool"]["values"]["diameter_mm"], 3.0)
        # 用了这把刀的工序会被点名（界面上删除/改名前的提示）
        self.assertIn(operation_id, {item["id"] for item in body["used_by"]})

        status, body = self.post(f"/api/operations/{operation_id}/generate", {})
        self.assertEqual(status, 200, body)
        after = body["result"]["tool"]["diameter_mm"]
        self.assertAlmostEqual(before, 6.0)
        self.assertAlmostEqual(after, 3.0)
        # 工序里存的刀具几何也跟着刀具库更新了（重新打开工程后显示的就是这一把）
        status, tree = self.get("/api/operations")
        stored = next(item for item in tree["operations"] if item["id"] == operation_id)
        self.assertAlmostEqual(stored["parameters"]["tool_diameter_mm"], 3.0)

    def test_deleting_a_tool_makes_the_operation_report_a_clear_error(self) -> None:
        status, body = self.post("/api/tools", {
            "name": "临时刀", "kind": "flat_end_mill", "values": {"diameter_mm": 8.0},
        })
        self.assertEqual(status, 200, body)
        tool_id = body["tool"]["id"]
        status, body = self._add_operation(tool_id)
        self.assertEqual(status, 200, body)
        operation_id = body["operation"]["id"]

        status, body = self.send_json("DELETE", f"/api/tools/{tool_id}")
        self.assertEqual(status, 200, body)
        self.assertIn(operation_id, {item["id"] for item in body["used_by"]})

        status, body = self.post(f"/api/operations/{operation_id}/generate", {})
        self.assertEqual(status, 400)
        self.assertIn("刀具库", body["error"])

    def test_operation_keeps_its_tool_when_other_parameters_change(self) -> None:
        status, body = self._add_operation("tool-flat-d10")
        self.assertEqual(status, 200, body)
        operation_id = body["operation"]["id"]
        status, body = self.post(f"/api/operations/{operation_id}",
                                 {"parameters": {"stepover_ratio": 0.3}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["operation"]["parameters"]["tool_id"], "tool-flat-d10")
        self.assertAlmostEqual(body["operation"]["parameters"]["tool_diameter_mm"], 10.0)

    def test_simulation_reports_the_library_tool(self) -> None:
        status, body = self._add_operation("tool-flat-d10")
        self.assertEqual(status, 200, body)
        operation_id = body["operation"]["id"]
        status, body = self.post("/api/simulate", {"operation_id": operation_id,
                                                   "max_frames": 6})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["tool"]["diameter_mm"], 10.0)

    def test_nc_program_carries_the_library_tool_diameter(self) -> None:
        status, body = self._add_operation("tool-flat-d10")
        self.assertEqual(status, 200, body)
        operation_id = body["operation"]["id"]
        status, raw, _ = self.send("POST", "/api/export/nc",
                                   payload={"operation_id": operation_id})
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn("D10", text.replace("D10.0", "D10"))

    def test_project_reopen_keeps_the_tool_reference(self) -> None:
        status, body = self._add_operation("tool-flat-d10")
        self.assertEqual(status, 200, body)
        operation_id = body["operation"]["id"]
        status, projects = self.get("/api/projects")
        self.assertEqual(status, 200)
        project_id = projects["current"]["id"]

        status, _ = self.send_json("DELETE", "/api/projects/current")
        self.assertEqual(status, 200)
        status, body = self.post("/api/projects/open", {"id": project_id})
        self.assertEqual(status, 200, body)
        restored = [item for item in body["project"]["tree"]["operations"]
                    if item["id"] == operation_id]
        self.assertTrue(restored)
        self.assertEqual(restored[0]["parameters"]["tool_id"], "tool-flat-d10")

        # 重新打开后还能用这把刀生成刀路
        status, body = self.post(f"/api/operations/{operation_id}/generate", {})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["result"]["tool"]["diameter_mm"], 10.0)


class ToolLibraryPersistenceTests(ToolServerCase):
    def test_library_file_lives_next_to_the_projects(self) -> None:
        path = Path(self.data_dir.name) / "tools" / "tools.json"
        self.assertTrue(path.is_file(), path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], 1)
        self.assertTrue(payload["tools"])


if __name__ == "__main__":
    unittest.main()
