"""曲面加工（平行行切 / 等高铣）接入 CAM 链路后的接口测试。

这一层测的是**接线**，不是几何算法本身——几何算法在 ``tests/test_surfacing.py``
里已经单独测过，这里只关心：

* 能力目录有没有把两个新类型发布出去，参数声明是不是按类型分流的；
* 请求校验对"曲面工序可以不选面"和"2.5 轴工序必须选面"是否分得清；
* 工序树、生成、仿真、BRep 持久化能不能把曲面刀路当成普通刀路看待。

因此断言以"结构正确 + 数值合理"为主，不去复算具体的刀位点。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.fixtures import solid_and_part, solid_bytes
from toolpath_lab.core.errors import ParameterError, PlanningError
from toolpath_lab.server.workspace import Workspace
from toolpath_lab.storage.repository import ProjectRepository

from tests.test_cam_api import ServerCase, get_json, post_json, upload


def _plate(name: str = "plate"):
    """夹具零件：同时拿到 PartModel 与同坐标系的 BrepModel。"""

    part, brep, _ = solid_and_part("plate")
    part.name = name
    return part, brep


class CatalogTests(unittest.TestCase):
    """目录是界面的唯一来源，所以类型与参数必须先在这里对。"""

    def test_catalog_publishes_five_operation_kinds(self) -> None:
        from toolpath_lab.cam.service import planning_catalog

        catalog = planning_catalog()
        ids = [item["id"] for item in catalog["operations"]]
        self.assertEqual(ids, ["face_mill", "pocket_mill", "contour_mill",
                               "parallel_surface", "waterline"])

    def test_surface_operations_carry_own_parameters(self) -> None:
        from toolpath_lab.cam.service import planning_catalog

        catalog = planning_catalog()
        by_id = {item["id"]: item for item in catalog["operations"]}
        parallel = by_id["parallel_surface"]
        waterline = by_id["waterline"]

        self.assertTrue(parallel["surface"])
        self.assertTrue(waterline["surface"])
        # 平行行切有行距，等高铣有层高；各自只发布自己用得上的参数。
        self.assertIn("stepover_mm", {item["key"] for item in parallel["parameters"]})
        self.assertNotIn("step_down_mm", {item["key"] for item in parallel["parameters"]})
        self.assertIn("step_down_mm", {item["key"] for item in waterline["parameters"]})
        self.assertNotIn("stepover_mm", {item["key"] for item in waterline["parameters"]})

    def test_strategy_is_implied_by_kind_not_shown(self) -> None:
        from toolpath_lab.cam.service import planning_catalog

        catalog = planning_catalog()
        by_id = {item["id"]: item for item in catalog["operations"]}
        for kind, strategy in (("parallel_surface", "parallel"), ("waterline", "waterline")):
            entry = by_id[kind]
            self.assertEqual(entry["implied"], {"strategy": strategy})
            # strategy 是内部键：不出现在界面上，也不出现在默认值里
            self.assertNotIn("strategy", {item["key"] for item in entry["parameters"]})
            self.assertNotIn("strategy", entry["defaults"])

    def test_catalog_keeps_flat_surface_parameter_list(self) -> None:
        """脚本与测试要一份完整声明（含 strategy），界面用按类型分流的那份。"""

        from toolpath_lab.cam.service import planning_catalog

        catalog = planning_catalog()
        keys = {item["key"] for item in catalog["surface_parameters"]}
        self.assertIn("strategy", keys)
        self.assertEqual(catalog["surface_defaults"]["strategy"], "parallel")

    def test_legacy_operation_entries_still_have_no_per_kind_parameters(self) -> None:
        """2.5 轴工序继续用全局那份参数声明，行为不变。"""

        from toolpath_lab.cam.service import planning_catalog

        for entry in planning_catalog()["operations"][:3]:
            self.assertFalse(entry["surface"])
            self.assertNotIn("parameters", entry)
            self.assertNotIn("implied", entry)


class RequestTests(unittest.TestCase):
    """请求校验：曲面工序允许不选面，2.5 轴工序不允许。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.part, cls.brep = _plate()

    def test_pocket_requires_a_face(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest

        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload({"kind": "pocket_mill"}, self.part)

    def test_parallel_surface_allows_no_face(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest

        request = CAMOperationRequest.from_payload({"kind": "parallel_surface"}, self.part)
        self.assertEqual(request.face_ids, ())
        self.assertTrue(request.is_surface)
        self.assertEqual(request.parameters["strategy"], "parallel")
        self.assertEqual(request.tool.kind.value, "flat")

    def test_waterline_forces_its_own_strategy(self) -> None:
        """客户端就算送来 parallel，等高铣也只会按 waterline 走。"""

        from toolpath_lab.cam.service import CAMOperationRequest

        request = CAMOperationRequest.from_payload(
            {"kind": "waterline", "parameters": {"strategy": "parallel", "step_down_mm": 3.0}},
            self.part,
        )
        self.assertEqual(request.parameters["strategy"], "waterline")
        self.assertEqual(request.parameters["step_down_mm"], 3.0)

    def test_surface_tool_kind_selects_a_ball_cutter(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest

        request = CAMOperationRequest.from_payload(
            {"kind": "parallel_surface",
             "parameters": {"tool_kind": "ball", "tool_diameter_mm": 8.0}}, self.part,
        )
        self.assertEqual(request.tool.kind.value, "ball")
        self.assertEqual(request.tool.diameter_mm, 8.0)

    def test_surface_parameters_reject_out_of_range(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest

        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload(
                {"kind": "parallel_surface", "parameters": {"stepover_mm": 0.0}}, self.part,
            )

    def test_surface_parameter_keys_do_not_leak_into_25d(self) -> None:
        """2.5 轴请求里的曲面参数会被忽略，而不是报错。"""

        from toolpath_lab.cam.service import CAMOperationRequest

        request = CAMOperationRequest.from_payload(
            {"kind": "face_mill", "faces": [4], "parameters": {"stepover_mm": 5.0,
                                                               "tool_diameter_mm": 10.0,
                                                               "sampling_mm": 0.2,
                                                               "tool_kind": "ball"}},
            self.part,
        )
        self.assertNotIn("sampling_mm", request.parameters)
        self.assertNotIn("tool_kind", request.parameters)
        # 2.5 轴只出平底刀：栅格距离场就是按平底刀建的
        self.assertEqual(request.tool.kind.value, "flat")


class ExecutionTests(unittest.TestCase):
    """真正跑一遍：刀路结构、z 范围、错误提示。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.part, cls.brep = _plate()

    def test_parallel_surface_runs_without_faces(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

        request = CAMOperationRequest.from_payload(
            {"kind": "parallel_surface",
             "parameters": {"stepover_mm": 6.0, "sampling_mm": 1.0}}, self.part,
        )
        result = execute_operation(request)
        self.assertGreater(len(result.toolpath.moves), 0)
        self.assertGreater(result.toolpath.cut_length_mm, 0.0)
        self.assertEqual(result.regions, ())
        for move in result.toolpath.moves:
            self.assertGreaterEqual(float(move.points[:, 2].min()), -1e-6)

    def test_parallel_surface_restricted_to_one_face(self) -> None:
        """只选一个面时，刀路必须落在那个面的 XY 范围内。"""

        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

        top = _top_face(self.part)
        whole = execute_operation(CAMOperationRequest.from_payload(
            {"kind": "parallel_surface",
             "parameters": {"stepover_mm": 6.0, "sampling_mm": 1.0}}, self.part))
        one = execute_operation(CAMOperationRequest.from_payload(
            {"kind": "parallel_surface", "faces": [top],
             "parameters": {"stepover_mm": 6.0, "sampling_mm": 1.0}}, self.part))
        self.assertLess(one.toolpath.cut_length_mm, whole.toolpath.cut_length_mm)
        # 只加工顶面时，刀位 z 只会出现在顶面高度上
        zs = [float(move.points[:, 2].max()) for move in one.toolpath.moves
              if move.points.shape[0] and move.kind.value == "cut"]
        self.assertTrue(zs)
        self.assertTrue(all(abs(z - 40.0) < 1e-3 for z in zs), sorted(set(round(z, 3) for z in zs)))

    def test_waterline_without_brep_says_what_to_do(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

        request = CAMOperationRequest.from_payload({"kind": "waterline"}, self.part)
        with self.assertRaises(PlanningError) as caught:
            execute_operation(request)
        self.assertIn("BRep", str(caught.exception))

    def test_waterline_with_brep_produces_layers(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

        request = CAMOperationRequest.from_payload(
            {"kind": "waterline", "parameters": {"step_down_mm": 5.0}}, self.part)
        request = replace(request, brep=self.brep)
        result = execute_operation(request)
        self.assertGreater(len(result.toolpath.moves), 0)
        self.assertTrue(any("层" in note for note in result.warnings))

    def test_waterline_warns_about_non_flat_cutters(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

        request = CAMOperationRequest.from_payload(
            {"kind": "waterline", "parameters": {"tool_kind": "ball"}}, self.part)
        result = execute_operation(replace(request, brep=self.brep))
        self.assertTrue(any("平底刀" in note for note in result.warnings))

    def test_surface_toolpath_can_be_simulated(self) -> None:
        """曲面刀路就是普通刀路：仿真必须能吃下去。"""

        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation
        from toolpath_lab.core.stock import build_stock
        from toolpath_lab.simulation.cut_sim import simulate_toolpath

        request = CAMOperationRequest.from_payload(
            {"kind": "parallel_surface",
             "parameters": {"stepover_mm": 8.0, "sampling_mm": 1.0}}, self.part)
        result = execute_operation(request)
        stock = build_stock("rectangular", self.part,
                            {"offset_x_mm": 2, "offset_y_mm": 2, "offset_z_mm": 2})
        simulation = simulate_toolpath(result.toolpath, stock,
                                       tool_radius=request.tool.radius_mm, cell_mm=3.0)
        self.assertTrue(simulation.frames)
        self.assertGreater(simulation.removed_volume_mm3, 0.0)


class WorkspaceTests(unittest.TestCase):
    """工作空间：工序树、BRep 持久化。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tplab-surface-")
        self.workspace = Workspace(repository=ProjectRepository(Path(self._tmp.name)))
        self.project = self.workspace.import_step(solid_bytes("plate"),
                                                  filename="plate.step")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_keeps_the_brep(self) -> None:
        self.assertIsNotNone(self.workspace.brep)
        self.assertEqual(self.workspace.brep.face_count, 11)

    def test_import_saves_the_source_file(self) -> None:
        path = self.workspace.repository.model_path(self.project.project_id)
        self.assertIsNotNone(path)
        self.assertEqual(path.suffix, ".step")
        self.assertGreater(path.stat().st_size, 0)

    def test_add_surface_operation_without_faces(self) -> None:
        operation, result = self.workspace.add_operation(
            "parallel_surface", [], {"stepover_mm": 8.0, "sampling_mm": 1.0})
        self.assertEqual(operation.face_ids, ())
        self.assertIsNotNone(result)
        self.assertEqual(operation.state, "generated")
        self.assertGreater(operation.statistics["cut_length_mm"], 0.0)

    def test_add_25d_operation_without_faces_still_fails(self) -> None:
        with self.assertRaises(ParameterError):
            self.workspace.add_operation("pocket_mill", [])

    def test_surface_operation_gets_surface_defaults(self) -> None:
        """新增曲面工序时不能把 2.5 轴的参数当基线抄进来。"""

        operation, _ = self.workspace.add_operation("waterline", [])
        self.assertIn("step_down_mm", operation.parameters)
        self.assertIn("tool_kind", operation.parameters)
        self.assertNotIn("stepover_mm", operation.parameters)
        self.assertNotIn("spindle_rpm", operation.parameters)

    def test_switching_kind_refilters_parameters(self) -> None:
        """把 2.5 轴工序改成等高铣：旧类型的参数必须被摘掉。"""

        top = _top_face(self.workspace.project.part)
        operation, _ = self.workspace.add_operation("pocket_mill", [top])
        updated = self.workspace.update_operation(
            operation.operation_id, kind="waterline",
            parameters={**operation.parameters, "step_down_mm": 4.0})
        self.assertEqual(updated.kind, "waterline")
        self.assertIn("step_down_mm", updated.parameters)
        self.assertNotIn("stepover_mm", updated.parameters)
        self.assertNotIn("spindle_rpm", updated.parameters)

    def test_brep_survives_reopening_the_project(self) -> None:
        """重启后重新打开工程，等高铣必须还能跑——靠的是留存的原始模型文件。"""

        operation, _ = self.workspace.add_operation("waterline", [],
                                                    {"step_down_mm": 5.0})
        project_id = self.project.project_id

        reopened = Workspace(repository=ProjectRepository(Path(self._tmp.name)))
        reopened.open(project_id)
        self.assertIsNotNone(reopened.brep, "重新打开工程后 BRep 应该被读回来")
        result = reopened.generate(operation.operation_id)
        self.assertGreater(len(result.toolpath.moves), 0)

    def test_reopened_project_without_source_file_degrades_cleanly(self) -> None:
        """源文件丢了不是崩溃，而是等高铣给出可执行的提示。"""

        operation, _ = self.workspace.add_operation("waterline", [],
                                                    {"step_down_mm": 5.0})
        project_id = self.project.project_id
        for item in self.workspace.repository._dir(project_id).glob("source.*"):
            item.unlink()

        reopened = Workspace(repository=ProjectRepository(Path(self._tmp.name)))
        reopened.open(project_id)
        self.assertIsNone(reopened.brep)
        with self.assertRaises(PlanningError) as caught:
            reopened.generate(operation.operation_id)
        self.assertIn("重新导入", str(caught.exception))


class SurfaceApiTests(ServerCase):
    """HTTP 接口：目录、工序树、生成、仿真。"""

    def test_catalog_exposes_surface_operations(self) -> None:
        status, catalog = get_json(self.base, "/api/catalog")
        self.assertEqual(status, 200)
        ids = [item["id"] for item in catalog["cam"]["operations"]]
        self.assertIn("parallel_surface", ids)
        self.assertIn("waterline", ids)

    def test_create_and_generate_parallel_surface_over_http(self) -> None:
        upload(self.base, solid_bytes("plate"))
        status, body = post_json(self.base, "/api/operations", {
            "kind": "parallel_surface", "faces": [],
            "parameters": {"stepover_mm": 8.0, "sampling_mm": 1.0},
        })
        self.assertEqual(status, 200, body)
        operation = body["operation"]
        self.assertEqual(operation["kind"], "parallel_surface")
        self.assertEqual(operation["kind_label"], "平行行切")
        self.assertEqual(operation["faces"], [])
        self.assertEqual(operation["state"], "generated")
        self.assertGreater(body["result"]["toolpath"]["statistics"]["cut_length_mm"], 0.0)

    def test_create_and_generate_waterline_over_http(self) -> None:
        upload(self.base, solid_bytes("plate"))
        status, body = post_json(self.base, "/api/operations", {
            "kind": "waterline", "faces": [],
            "parameters": {"step_down_mm": 5.0},
        })
        self.assertEqual(status, 200, body)
        self.assertGreater(len(body["result"]["toolpath"]["moves"]), 0)

    def test_pocket_without_faces_is_rejected_over_http(self) -> None:
        upload(self.base, solid_bytes("plate"))
        status, body = post_json(self.base, "/api/operations",
                                 {"kind": "pocket_mill", "faces": []})
        self.assertEqual(status, 400, body)
        self.assertIn("加工面", body["error"])

    def test_simulate_surface_operation_over_http(self) -> None:
        upload(self.base, solid_bytes("plate"))
        _, created = post_json(self.base, "/api/operations", {
            "kind": "parallel_surface", "faces": [],
            "parameters": {"stepover_mm": 10.0, "sampling_mm": 1.5},
        })
        operation_id = created["operation"]["id"]
        status, body = post_json(self.base, "/api/simulate",
                                 {"operation_id": operation_id, "cell_mm": 4.0})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "parallel_surface")
        self.assertGreater(body["summary"]["removed_volume_mm3"], 0.0)
        self.assertTrue(body["frames"])

    def test_simulate_waterline_by_kind_over_http(self) -> None:
        """界面上"改参数立刻看仿真"走这条：只给类型与参数，BRep 由工作空间补。"""

        upload(self.base, solid_bytes("plate"))
        status, body = post_json(self.base, "/api/simulate", {
            "kind": "waterline", "faces": [],
            "parameters": {"step_down_mm": 6.0, "tool_diameter_mm": 8.0},
            "cell_mm": 4.0,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "waterline")
        self.assertTrue(body["frames"])

    def test_simulate_waterline_works_after_reopening_the_project(self) -> None:
        """重开工程后 BRep 从留存的源文件读回来，"改参数看仿真"这条路仍然通。"""

        upload(self.base, solid_bytes("plate"))
        _, listing = get_json(self.base, "/api/projects")
        project_id = listing["projects"][0]["id"]
        status, body = post_json(self.base, "/api/projects/open", {"id": project_id})
        self.assertEqual(status, 200, body)

        status, body = post_json(self.base, "/api/simulate", {
            "kind": "waterline", "faces": [],
            "parameters": {"step_down_mm": 8.0}, "cell_mm": 5.0,
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body["frames"])

    def test_nc_export_accepts_a_surface_operation(self) -> None:
        upload(self.base, solid_bytes("plate"))
        _, created = post_json(self.base, "/api/operations", {
            "kind": "parallel_surface", "faces": [],
            "parameters": {"stepover_mm": 10.0, "sampling_mm": 2.0},
        })
        operation_id = created["operation"]["id"]
        status, raw, headers = self.send("POST", "/api/export/nc",
                                         payload={"operation_id": operation_id})
        self.assertEqual(status, 200)
        program = raw.decode("utf-8")
        self.assertIn("G0", program)
        self.assertIn(".nc", headers.get("Content-Disposition", ""))

    def test_surface_operation_survives_project_reload(self) -> None:
        """存盘再打开：曲面工序的参数与刀路都还在。"""

        upload(self.base, solid_bytes("plate"))
        post_json(self.base, "/api/operations", {
            "kind": "waterline", "faces": [], "parameters": {"step_down_mm": 4.0},
        })
        _, listing = get_json(self.base, "/api/projects")
        project_id = listing["projects"][0]["id"]
        status, body = post_json(self.base, "/api/projects/open", {"id": project_id})
        self.assertEqual(status, 200, body)
        operations = body["project"]["tree"]["operations"]
        self.assertEqual(operations[0]["kind"], "waterline")
        self.assertEqual(operations[0]["kind_label"], "等高铣")
        self.assertEqual(operations[0]["parameters"]["step_down_mm"], 4.0)


def _top_face(part) -> int:
    for feature in part.features:
        if feature["horizontal"] and abs(feature["plane"][3] - 40.0) < 1e-6:
            return int(feature["face_id"])
    raise AssertionError("夹具里应该有 z=40 的顶面")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
