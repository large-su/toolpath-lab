"""CAM 的 HTTP 接口：导入、毛坯、工序树、仿真、出程序与错误路径。

每个测试类用一个独立的数据目录与服务实例，因此不会污染用户目录，也不会互相影响。
请求辅助函数写成模块级函数（而不是方法），这样 ``setUpClass`` 里也能直接用。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from tests.fixtures import plate_with_pocket, simple_box
from toolpath_lab.server.app import create_server

PARAMETERS: dict[str, Any] = {
    "tool_diameter_mm": 10.0,
    "spindle_rpm": 3200.0,
    "feed_mm_per_min": 900.0,
    "stepover_mm": 5.0,
    "cut_depth_mm": 2.0,
    "stock_allowance_mm": 0.0,
    "finish_allowance_mm": 0.0,
    "safe_height_mm": 10.0,
    "cut_mode": "contour",
    "finish_pass": False,
}

_BOUNDARY = "----toolpathlabtest"


def http(base: str, method: str, path: str, *, payload: Any = None,
         raw: bytes | None = None, content_type: str | None = "application/json"
         ) -> tuple[int, bytes, dict[str, str]]:
    """发一个请求，把 HTTP 错误也当成正常返回（便于断言状态码）。"""

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


class ServerCase(unittest.TestCase):
    """自带服务与数据目录的测试基类。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = tempfile.TemporaryDirectory(prefix="tplab-api-")
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

    # 便捷包装
    def get(self, path: str):
        return get_json(self.base, path)

    def post(self, path: str, payload: Any = None):
        return post_json(self.base, path, payload)

    def send(self, method: str, path: str, **kwargs):
        return http(self.base, method, path, **kwargs)

    def import_plate(self):
        status, body = upload(self.base, plate_with_pocket().encode("latin-1"))
        self.assertEqual(status, 200, body)
        return body


def horizontal_faces(base: str):
    _, features = get_json(base, "/api/model/features")
    items = features["features"]
    top = [f for f in items if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6][0]
    floor = [f for f in items if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]
    return items, top, floor


class ImportTests(ServerCase):
    def test_import_step_via_multipart(self) -> None:
        payload = self.import_plate()
        part = payload["project"]["part"]
        self.assertEqual(part["size_mm"], [100.0, 80.0, 40.0])
        self.assertEqual(part["statistics"]["faces"], 11)
        self.assertTrue(part["mesh"]["indices"])
        self.assertTrue(part["faces"])

    def test_model_and_features_endpoints(self) -> None:
        self.import_plate()
        status, model = self.get("/api/model")
        self.assertEqual(status, 200)
        self.assertTrue(model["project"]["part"]["mesh"]["positions"])
        status, features = self.get("/api/model/features")
        self.assertEqual(status, 200)
        self.assertEqual(len(features["features"]), 11)
        horizontal = [f for f in features["features"] if f["horizontal"]]
        self.assertEqual(len(horizontal), 2)
        self.assertTrue(all(f["machinable"] for f in features["features"] if f["normal"][2] > 0))

    def test_catalog_exposes_cam_capabilities(self) -> None:
        status, catalog = self.get("/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in catalog["stock"]["shapes"]],
                         ["rectangular", "cylindrical"])
        ids = [item["id"] for item in catalog["cam"]["operations"]]
        self.assertIn("face_mill", ids)
        self.assertIn("pocket_mill", ids)
        keys = {item["key"] for item in catalog["cam"]["parameters"]}
        self.assertIn("spindle_rpm", keys)
        self.assertIn("cut_depth_mm", keys)
        self.assertIn(".step", catalog["import"]["suffixes"])
        # 原有基座目录依旧完整
        self.assertEqual([item["id"] for item in catalog["planners"]["list"]], ["raster"])

    def test_broken_step_is_rejected(self) -> None:
        status, body = upload(self.base, b"this is not a STEP file")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_truncated_step_is_rejected(self) -> None:
        status, body = upload(self.base, plate_with_pocket().encode("latin-1")[:400])
        self.assertIn(status, (400, 422))
        self.assertFalse(body["ok"])

    def test_non_multipart_upload_needs_a_json_hint(self) -> None:
        status, body, _ = self.send("POST", "/api/import/step", raw=b"raw bytes",
                                    content_type="application/octet-stream")
        self.assertEqual(status, 400)
        self.assertIn("multipart", json.loads(body)["error"])

    def test_json_content_import(self) -> None:
        status, body = self.post("/api/import/step", {"filename": "box.step",
                                                      "content": simple_box()})
        self.assertEqual(status, 200)
        self.assertEqual(body["project"]["part"]["size_mm"], [40.0, 30.0, 20.0])

    def test_requests_before_import_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tplab-empty-") as directory:
            server = create_server("127.0.0.1", 0, data_dir=Path(directory))
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{port}"
            try:
                status, health = get_json(base, "/api/health")
                self.assertTrue(health["ok"])
                self.assertFalse(health["workspace"])
                for path in ("/api/stock", "/api/model", "/api/operations"):
                    with self.subTest(path=path):
                        status, _ = get_json(base, path)
                        self.assertEqual(status, 400)
            finally:
                server.shutdown()
                server.server_close()


class StockApiTests(ServerCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        status, _ = upload(cls.base, plate_with_pocket().encode("latin-1"))
        assert status == 200

    def test_stock_defaults_to_rectangular(self) -> None:
        status, stock = self.get("/api/stock")
        self.assertEqual(status, 200)
        self.assertEqual(stock["id"], "rectangular")
        self.assertEqual(len(stock["mesh"]["positions"]), 8)
        self.assertEqual(len(stock["residual_mm"]), 3)
        self.assertEqual([item["id"] for item in stock["catalog"]],
                         ["rectangular", "cylindrical"])

    def test_stock_can_switch_to_cylindrical(self) -> None:
        status, stock = self.post("/api/stock", {
            "shape": "cylindrical",
            "parameters": {"offset_radial_mm": 2.0, "offset_z_mm": 1.0},
        })
        self.assertEqual(status, 200, stock)
        self.assertEqual(stock["id"], "cylindrical")
        size = stock["bounds"]["size"]
        self.assertAlmostEqual(size[0], size[1], places=4)
        self.assertGreater(len(stock["mesh"]["positions"]), 8)
        # 换回矩形，后面的测试与默认状态一致
        self.post("/api/stock", {"shape": "rectangular",
                                 "parameters": {"offset_x_mm": 2.0, "offset_y_mm": 2.0,
                                                "offset_z_mm": 1.0}})

    def test_stock_parameters_are_validated(self) -> None:
        status, body = self.post("/api/stock", {
            "shape": "rectangular",
            "parameters": {"offset_x_mm": -5.0},
        })
        self.assertEqual(status, 400)
        self.assertIn("offset_x_mm", body["error"])

    def test_unknown_stock_shape(self) -> None:
        status, _ = self.post("/api/stock", {"shape": "hexagon"})
        self.assertEqual(status, 400)


class OperationApiTests(ServerCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        status, _ = upload(cls.base, plate_with_pocket().encode("latin-1"))
        assert status == 200
        _, cls.top, cls.floor = horizontal_faces(cls.base)
        _, catalog = get_json(cls.base, "/api/catalog")
        cls.catalog = catalog

    def _create(self, kind: str = "pocket_mill", face: int | None = None, **overrides):
        parameters = dict(PARAMETERS)
        parameters.update(overrides)
        status, body = self.post("/api/operations", {
            "kind": kind,
            "faces": [face if face is not None else self.floor["face_id"]],
            "parameters": parameters,
        })
        self.assertEqual(status, 200, body)
        return body

    def test_create_generates_a_toolpath_immediately(self) -> None:
        body = self._create()
        operation = body["operation"]
        self.assertEqual(operation["kind"], "pocket_mill")
        self.assertEqual(operation["state"], "generated")
        self.assertTrue(operation["faces"])
        stats = body["result"]["toolpath"]["statistics"]
        self.assertGreater(stats["move_count"], 0)
        self.assertGreater(stats["cut_length_mm"], 0)
        self.assertTrue(body["result"]["regions"])

    def test_face_mill_on_the_top_face(self) -> None:
        body = self._create(kind="face_mill", face=self.top["face_id"])
        self.assertEqual(body["operation"]["kind"], "face_mill")
        self.assertGreater(body["result"]["statistics"]["move_count"], 0)

    def test_editing_parameters_then_regenerating_changes_the_toolpath(self) -> None:
        """改完参数再生成，刀路必须跟着变。

        这是界面上"编辑刚刚的工序、重新设置刀具与切削参数、重新生成刀路"那条路。
        前端会把面板里改过的参数先落盘再调 generate；只要这一步漏了，后端就会拿旧参数
        重算，刀路看起来"完全没反应"。
        """

        created = self._create(tool_diameter_mm=10.0, stepover_mm=5.0)
        operation_id = created["operation"]["id"]
        before = created["result"]["toolpath"]["statistics"]

        status, updated = self.post(f"/api/operations/{operation_id}", {
            "parameters": {"tool_diameter_mm": 20.0, "stepover_mm": 12.0},
        })
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["operation"]["parameters"]["tool_diameter_mm"], 20.0)
        # 参数一变就退回"待生成"，树上不能继续挂着旧刀路的统计
        self.assertEqual(updated["operation"]["state"], "draft")

        status, regenerated = self.post(f"/api/operations/{operation_id}/generate", {})
        self.assertEqual(status, 200, regenerated)
        after = regenerated["result"]["toolpath"]["statistics"]
        self.assertEqual(regenerated["result"]["tool"]["radius_mm"], 10.0)
        self.assertNotEqual(before["point_count"], after["point_count"])
        self.assertGreater(abs(before["cut_length_mm"] - after["cut_length_mm"]), 1.0)

    def test_operation_kind_can_be_changed(self) -> None:
        """加工类型可以改（平面铣 ↔ 型腔铣），改完同样退回待生成。"""

        created = self._create(kind="pocket_mill")
        operation_id = created["operation"]["id"]
        status, updated = self.post(f"/api/operations/{operation_id}", {"kind": "face_mill"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["operation"]["kind"], "face_mill")
        self.assertEqual(updated["operation"]["state"], "draft")
        status, regenerated = self.post(f"/api/operations/{operation_id}/generate", {})
        self.assertEqual(status, 200, regenerated)
        self.assertEqual(regenerated["result"]["request"]["kind"]
                         if "request" in regenerated["result"] else "face_mill", "face_mill")

    def test_contour_mill(self) -> None:
        body = self._create(kind="contour_mill", face=self.top["face_id"])
        self.assertIn("轮廓", body["operation"]["kind_label"])
        self.assertGreater(body["result"]["statistics"]["cut_length_mm"], 0)

    def test_tree_lists_operations_in_order(self) -> None:
        first = self._create(name="第一道")["operation"]
        second = self._create(name="第二道")["operation"]
        status, tree = self.get("/api/operations")
        self.assertEqual(status, 200)
        ids = [item["id"] for item in tree["operations"]]
        self.assertIn(first["id"], ids)
        self.assertIn(second["id"], ids)
        self.assertEqual([item["sequence"] for item in tree["operations"]],
                         list(range(len(tree["operations"]))))
        self.assertEqual(tree["count"], len(tree["operations"]))

    def test_update_parameters_regenerates(self) -> None:
        operation = self._create()["operation"]
        status, body = self.post(f"/api/operations/{operation['id']}",
                                 {"parameters": {"stepover_mm": 2.0}})
        self.assertEqual(status, 200)
        self.assertEqual(body["operation"]["parameters"]["stepover_mm"], 2.0)
        self.assertEqual(body["operation"]["state"], "draft")
        status, body = self.post(f"/api/operations/{operation['id']}/generate")
        self.assertEqual(status, 200)
        self.assertGreater(body["result"]["toolpath"]["statistics"]["move_count"], 0)

    def test_enable_disable_and_rename(self) -> None:
        operation = self._create()["operation"]
        _, body = self.post(f"/api/operations/{operation['id']}", {"enabled": False})
        self.assertFalse(body["operation"]["enabled"])
        _, body = self.post(f"/api/operations/{operation['id']}", {"name": "改名了"})
        self.assertEqual(body["operation"]["name"], "改名了")

    def test_move_resequences(self) -> None:
        first = self._create(name="A")["operation"]
        self._create(name="B")
        status, body = self.post(f"/api/operations/{first['id']}/move", {"sequence": 0})
        self.assertEqual(status, 200)
        self.assertEqual(body["operations"][0]["id"], first["id"])
        self.assertEqual([item["sequence"] for item in body["operations"]],
                         list(range(len(body["operations"]))))

    def test_duplicate_and_delete(self) -> None:
        operation = self._create(name="原始")["operation"]
        status, body = self.post(f"/api/operations/{operation['id']}/duplicate")
        self.assertEqual(status, 200)
        clone = body["operation"]
        self.assertNotEqual(clone["id"], operation["id"])
        self.assertNotEqual(clone["name"], operation["name"])
        status, _, _ = self.send("DELETE", f"/api/operations/{clone['id']}")
        self.assertEqual(status, 200)
        _, tree = self.get("/api/operations")
        self.assertNotIn(clone["id"], [item["id"] for item in tree["operations"]])

    def test_generate_all_skips_disabled(self) -> None:
        enabled = self._create(name="启用的")["operation"]
        # 复制一份后禁用，避免改动上面那道工序影响后续断言
        _, body = self.post(f"/api/operations/{enabled['id']}/duplicate")
        disabled = body["operation"]
        self.post(f"/api/operations/{disabled['id']}", {"enabled": False})
        status, body = self.post("/api/operations/generate", {})
        self.assertEqual(status, 200)
        results = {item["id"]: item for item in body["results"]}
        self.assertIn(enabled["id"], results)
        self.assertNotIn(disabled["id"], results)
        # 本类里其它测试也会留下工序，只看这两道的成败
        self.assertTrue(results[enabled["id"]]["ok"], results[enabled["id"]])

    def test_operation_without_faces_is_rejected(self) -> None:
        status, body = self.post("/api/operations", {"kind": "pocket_mill", "faces": []})
        self.assertEqual(status, 400)
        self.assertIn("面", body["error"])

    def test_unknown_kind_is_rejected(self) -> None:
        status, _ = self.post("/api/operations",
                              {"kind": "laser", "faces": [self.top["face_id"]]})
        self.assertEqual(status, 400)

    def test_unknown_face_is_unprocessable(self) -> None:
        status, _ = self.post("/api/operations",
                              {"kind": "pocket_mill", "faces": [999999]})
        self.assertEqual(status, 422)

    def test_downward_face_is_unprocessable(self) -> None:
        bottom = [f for f in horizontal_faces(self.base)[0]
                  if f["planar"] and f["normal"][2] < -0.9][0]
        status, body = self.post("/api/operations", {
            "kind": "pocket_mill", "faces": [bottom["face_id"]], "parameters": PARAMETERS,
        })
        self.assertEqual(status, 422)
        self.assertIn("朝上", body["error"])

    def test_unknown_operation_id(self) -> None:
        status, _ = self.post("/api/operations/nope/generate")
        self.assertEqual(status, 400)

    def test_templates_round_trip(self) -> None:
        status, body = self.post("/api/templates", {
            "name": "开粗模板", "kind": "pocket_mill", "parameters": {"stepover_mm": 3.5},
        })
        self.assertEqual(status, 200)
        template = body["template"]
        self.assertEqual(template["name"], "开粗模板")
        self.assertEqual(template["parameters"]["stepover_mm"], 3.5)
        _, tree = self.get("/api/operations")
        self.assertIn(template["id"], [item["id"] for item in tree["templates"]])
        status, _, _ = self.send("DELETE", f"/api/templates/{template['id']}")
        self.assertEqual(status, 200)
        _, tree = self.get("/api/operations")
        self.assertNotIn(template["id"], [item["id"] for item in tree["templates"]])

    def test_global_parameters_round_trip(self) -> None:
        status, body = self.post("/api/parameters", {
            "cam": {"stepover_mm": 4.5},
            "controller": {"program_number": 2024, "work_offset": "g55"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["cam"]["stepover_mm"], 4.5)
        self.assertEqual(body["controller"]["program_number"], 2024)
        _, fresh = self.get("/api/parameters")
        self.assertEqual(fresh["controller"]["work_offset"], "g55")


class SimulationAndExportTests(ServerCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        status, _ = upload(cls.base, plate_with_pocket().encode("latin-1"))
        assert status == 200
        _, cls.top, cls.floor = horizontal_faces(cls.base)
        status, body = post_json(cls.base, "/api/operations", {
            "kind": "pocket_mill", "faces": [cls.floor["face_id"]], "parameters": PARAMETERS,
        })
        assert status == 200, body
        cls.operation = body["operation"]

    def test_simulation_of_one_operation(self) -> None:
        status, body = self.post("/api/simulate", {
            "operation_id": self.operation["id"], "cell_mm": 0.8, "max_frames": 20,
        })
        self.assertEqual(status, 200, body)
        self.assertGreater(len(body["frames"]), 1)
        grid = body["grid"]
        self.assertEqual(grid["rows"] * grid["cols"], len(body["frames"][-1]["height"]))
        summary = body["summary"]
        self.assertGreater(summary["removed_volume_mm3"], 0)
        self.assertLessEqual(summary["removed_volume_mm3"], summary["initial_volume_mm3"])
        self.assertTrue(body["final_mesh"]["indices"])
        self.assertTrue(body["toolpath"]["moves"])

    def test_simulation_of_the_whole_job(self) -> None:
        status, body = self.post("/api/simulate", {"cell_mm": 1.0, "max_frames": 15})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "full_job")
        self.assertGreater(body["summary"]["removed_volume_mm3"], 0)

    def test_simulation_frames_are_monotonic(self) -> None:
        _, body = self.post("/api/simulate", {"cell_mm": 1.0, "max_frames": 25})
        removed = [frame["removed_mm3"] for frame in body["frames"]]
        self.assertEqual(removed, sorted(removed))

    def test_simulation_without_operations_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tplab-noops-") as directory:
            server = create_server("127.0.0.1", 0, data_dir=Path(directory))
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{port}"
            try:
                status, _ = upload(base, simple_box().encode("latin-1"), "box.step")
                self.assertEqual(status, 200)
                status, body = post_json(base, "/api/simulate", {})
                self.assertEqual(status, 400)
                self.assertIn("工序", body["error"])
            finally:
                server.shutdown()
                server.server_close()

    def test_nc_export_contains_a_real_program(self) -> None:
        status, body, headers = self.send("POST", "/api/export/nc", payload={})
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("O"))
        for token in ("G21", "G90", "G17", "G54", "M30", "S", "F", "G0", "G1"):
            self.assertIn(token, text)

    def test_nc_export_honours_controller_settings(self) -> None:
        self.post("/api/parameters", {"controller": {"program_number": 2024,
                                                     "work_offset": "g56"}})
        status, body, _ = self.send("POST", "/api/export/nc",
                                    payload={"operation_id": self.operation["id"]})
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("O2024"))
        self.assertIn("G56", text)
        self.post("/api/parameters", {"controller": {"program_number": 1000,
                                                     "work_offset": "g54"}})

    def test_nc_export_for_unknown_operation_fails(self) -> None:
        status, _ = self.post("/api/export/nc", {"operation_id": "missing"})
        self.assertEqual(status, 400)

    def test_base_plan_endpoint_still_works(self) -> None:
        status, body = self.post("/api/plan", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("toolpath", body)


class ProjectLifecycleTests(ServerCase):
    def test_project_is_persisted_and_can_be_reopened(self) -> None:
        self.import_plate()
        _, _, floor = horizontal_faces(self.base)
        _, body = self.post("/api/operations", {
            "kind": "pocket_mill", "faces": [floor["face_id"]], "parameters": PARAMETERS,
        })
        operation_id = body["operation"]["id"]

        status, projects = self.get("/api/projects")
        self.assertEqual(status, 200)
        current = projects["current"]
        self.assertIsNotNone(current)
        self.assertEqual(current["operations"], 1)

        self.send("DELETE", "/api/projects/current")
        status, _ = self.get("/api/stock")
        self.assertEqual(status, 400)

        project_id = projects["projects"][0]["id"]
        status, opened = self.post("/api/projects/open", {"id": project_id})
        self.assertEqual(status, 200)
        self.assertTrue(opened["project"]["part"]["mesh"]["positions"])
        _, tree = self.get("/api/operations")
        self.assertIn(operation_id, [item["id"] for item in tree["operations"]])
        self.assertEqual(tree["operations"][0]["parameters"]["stepover_mm"], 5.0)

    def test_delete_project(self) -> None:
        self.import_plate()
        _, projects = self.get("/api/projects")
        project_id = projects["projects"][0]["id"]
        status, body, _ = self.send("DELETE", f"/api/projects/{project_id}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        _, after = self.get("/api/projects")
        self.assertNotIn(project_id, [item["id"] for item in after["projects"]])


class KeepAliveTests(ServerCase):
    """同一条持久连接上连续发请求。

    这是前端真实的用法（浏览器复用连接），而上面所有测试用的 urllib 每次都会新建连接，
    所以漏读请求体这种 bug 在那种写法下永远暴露不出来。
    """

    def request(self, connection: HTTPConnection, method: str, path: str,
                payload: Any = None) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # 解析失败时把原文带出来，断言信息里能直接看到服务端到底回了什么
            return response.status, {"raw": raw.decode("utf-8", "replace")}

    def test_bodyless_route_does_not_desync_the_next_request(self) -> None:
        """生成刀路的 POST 只带 `{}`，路由里用不到它，但必须被读掉。

        否则残留的 `{}` 会被当成下一个请求的请求行，导入接口回 501，
        界面上就是"导入失败 / 模型不显示"。
        """

        self.import_plate()
        _, _, floor = horizontal_faces(self.base)
        _, created = self.post("/api/operations", {
            "kind": "pocket_mill", "faces": [floor["face_id"]], "parameters": PARAMETERS,
        })
        operation_id = created["operation"]["id"]

        host = self.base[len("http://"):]
        connection = HTTPConnection(host, timeout=240)
        try:
            # 1. 带 body 但路由不读的接口（生成刀路 / 批量生成 / 复制）
            for path in (f"/api/operations/{operation_id}/generate",
                         "/api/operations/generate",
                         f"/api/operations/{operation_id}/duplicate"):
                status, body = self.request(connection, "POST", path, {})
                self.assertEqual(status, 200, (path, body))

            # 2. 紧接着在**同一条连接**上导入模型：请求行必须还是完好的
            uploaded = plate_with_pocket().encode("latin-1")
            body = (
                f"--{_BOUNDARY}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="plate.step"\r\n'
                "Content-Type: application/step\r\n\r\n"
            ).encode("utf-8") + uploaded + f"\r\n--{_BOUNDARY}--\r\n".encode("utf-8")
            connection.request("POST", "/api/import/step", body=body, headers={
                "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
            })
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200, payload)
            self.assertTrue(payload["project"]["part"]["mesh"]["positions"])

            # 3. 连接还能继续用（没有因为兜底而被迫断开）
            status, health = self.request(connection, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
        finally:
            connection.close()

    def test_reused_connection_sees_each_request_own_json(self) -> None:
        """同一条连接上的两个请求必须各读自己的 body，不能命中上一个请求的 JSON 缓存。"""

        self.import_plate()
        host = self.base[len("http://"):]
        connection = HTTPConnection(host, timeout=240)
        try:
            status, first = self.request(connection, "POST", "/api/stock", {
                "shape": "rectangular",
                "parameters": {"offset_x_mm": 2.0, "offset_y_mm": 2.0, "offset_z_mm": 1.0},
            })
            self.assertEqual(status, 200, first)
            status, second = self.request(connection, "POST", "/api/stock", {
                "shape": "rectangular",
                "parameters": {"offset_x_mm": 9.0, "offset_y_mm": 9.0, "offset_z_mm": 9.0},
            })
            self.assertEqual(status, 200, second)
            self.assertNotEqual(first["bounds"]["size"], second["bounds"]["size"])
            self.assertAlmostEqual(second["residual_mm"][0], 9.0, places=4)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
