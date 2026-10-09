"""刀具库：类型目录、几何换算与持久化。

分三层验证，和实现的分层一一对应：

* 领域层（``core.tool``）—— 类型目录、参数归一、几何换算；
* 存储层（``storage.tool_library``）—— 一个 JSON 文件的增删改查与容错；
* 与 CAM 的打通 —— 工序引用刀具 id 后，刀路真的按那把刀的直径算。

这些用例不需要 OCP：只有"与 CAM 打通"那一组用到零件夹具（它需要 OCP）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import ParameterKind
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.tool import (
    CORNER_KEY,
    DIAMETER_KEY,
    FLUTE_KEY,
    LENGTH_KEY,
    PITCH_KEY,
    TEETH_KEY,
    TIP_ANGLE_KEY,
    TOOL_TYPES,
    Tool,
    ToolKind,
    ToolRecord,
    ToolType,
    build_tool,
    normalize_tool_type,
    normalize_tool_values,
    tool_geometry_values,
    tool_library_catalog,
    tool_type_defaults,
    tool_type_parameters,
    tool_type_parameters_for,
)
from toolpath_lab.storage.tool_library import ToolRepository


class ToolTypeCatalogTests(unittest.TestCase):
    def test_catalog_publishes_every_type_with_parameters_and_defaults(self) -> None:
        catalog = tool_library_catalog()
        ids = [item["id"] for item in catalog["types"]]
        self.assertEqual(ids, [choice.value for choice in TOOL_TYPES])
        self.assertIn(ToolType.DRILL.value, ids)
        for entry in catalog["types"]:
            self.assertTrue(entry["parameters"], entry["id"])
            self.assertTrue(entry["defaults"], entry["id"])
            self.assertEqual(entry["defaults"]["kind"], entry["id"])

    def test_each_type_only_keeps_its_own_parameters(self) -> None:
        drill = {item.key for item in tool_type_parameters_for(ToolType.DRILL.value).specs}
        tap = {item.key for item in tool_type_parameters_for(ToolType.TAP.value).specs}
        mill = {item.key for item in tool_type_parameters_for(ToolType.FLAT_END_MILL.value).specs}
        self.assertIn(TIP_ANGLE_KEY, drill)
        self.assertNotIn(PITCH_KEY, drill)
        self.assertIn(PITCH_KEY, tap)
        self.assertNotIn(TIP_ANGLE_KEY, tap)
        self.assertIn(TEETH_KEY, mill)
        self.assertNotIn(CORNER_KEY, mill)

    def test_diameter_and_length_are_shared_by_every_type(self) -> None:
        for choice in TOOL_TYPES:
            keys = {item.key for item in tool_type_parameters_for(choice.value).specs}
            self.assertIn(DIAMETER_KEY, keys, choice.value)
            self.assertIn(LENGTH_KEY, keys, choice.value)
            self.assertIn(FLUTE_KEY, keys, choice.value)

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            normalize_tool_type("laser")
        with self.assertRaises(ParameterError):
            tool_type_defaults("laser")

    def test_declaration_is_a_parameter_set_with_choices(self) -> None:
        specs = tool_type_parameters()
        self.assertIs(specs.spec("kind").kind, ParameterKind.CHOICE)
        self.assertEqual(len(specs.spec("kind").choices), len(TOOL_TYPES))


class ToolValueNormalizationTests(unittest.TestCase):
    def test_defaults_fill_every_known_key(self) -> None:
        values = normalize_tool_values(ToolType.FLAT_END_MILL.value, {})
        for key in (DIAMETER_KEY, FLUTE_KEY, LENGTH_KEY, TEETH_KEY):
            self.assertIn(key, values)

    def test_corner_radius_is_clamped_to_the_tool_radius(self) -> None:
        values = normalize_tool_values(ToolType.BULL_NOSE_MILL.value,
                                       {DIAMETER_KEY: 12.0, CORNER_KEY: 99.0})
        self.assertAlmostEqual(values[CORNER_KEY], 6.0)

    def test_hidden_parameters_are_zeroed_for_every_other_type(self) -> None:
        """用不到的键归零，且归零后**仍然能通过校验**（否则读回来就炸）。"""

        for kind in TOOL_TYPES:
            values = normalize_tool_values(kind.value, {})
            # 再归一一次必须稳定（幂等），这一条挡住"存进去的值不合自己规格"的坑
            again = normalize_tool_values(kind.value, values)
            self.assertEqual(values, again, kind.value)

    def test_flute_longer_than_the_tool_is_pulled_back(self) -> None:
        values = normalize_tool_values(ToolType.FLAT_END_MILL.value,
                                       {FLUTE_KEY: 80.0, LENGTH_KEY: 40.0})
        self.assertAlmostEqual(values[FLUTE_KEY], 40.0)

    def test_values_from_another_type_are_dropped(self) -> None:
        values = normalize_tool_values(ToolType.DRILL.value, {PITCH_KEY: 1.5, DIAMETER_KEY: 8.0})
        self.assertEqual(values[PITCH_KEY], 0.0)
        self.assertAlmostEqual(values[DIAMETER_KEY], 8.0)

    def test_out_of_range_diameter_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            normalize_tool_values(ToolType.FLAT_END_MILL.value, {DIAMETER_KEY: 0.0})


class ToolGeometryTests(unittest.TestCase):
    @staticmethod
    def _record(kind: str, **values):
        return ToolRecord.create("测试刀", kind, values)

    def test_flat_end_mill_footprint_is_the_radius(self) -> None:
        tool = build_tool(self._record(ToolType.FLAT_END_MILL.value, diameter_mm=6.0))
        self.assertIs(tool.kind, ToolKind.FLAT)
        self.assertAlmostEqual(tool.footprint_radius_mm, 3.0)

    def test_ball_end_mill_touches_with_its_tip(self) -> None:
        tool = build_tool(self._record(ToolType.BALL_END_MILL.value, diameter_mm=8.0))
        self.assertIs(tool.kind, ToolKind.BALL)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        self.assertAlmostEqual(tool.corner_radius_mm, 4.0)

    def test_bull_nose_mill_uses_its_flat_bottom(self) -> None:
        tool = build_tool(self._record(
            ToolType.BULL_NOSE_MILL.value, diameter_mm=10.0, corner_radius_mm=2.5
        ))
        self.assertIs(tool.kind, ToolKind.BULL)
        self.assertAlmostEqual(tool.corner_radius_mm, 2.5)
        self.assertAlmostEqual(tool.footprint_radius_mm, 2.5)

    def test_drill_is_approximated_by_a_flat_cylinder(self) -> None:
        """钻头/丝锥在三轴刀路里按平底圆柱处理：直径是对的，顶角只进注释。"""

        tool = build_tool(self._record(
            ToolType.DRILL.value, diameter_mm=8.0, tip_angle_deg=118.0
        ))
        self.assertIs(tool.kind, ToolKind.FLAT)
        self.assertAlmostEqual(tool.diameter_mm, 8.0)
        self.assertAlmostEqual(tool.tip_angle_deg, 118.0)
        self.assertIn("tip_angle_deg", tool.describe())

    def test_flute_length_travels_with_the_tool(self) -> None:
        tool = build_tool(self._record(ToolType.FLAT_END_MILL.value, flute_length_mm=18.0))
        self.assertAlmostEqual(tool.flute_length_mm, 18.0)
        self.assertAlmostEqual(tool.describe()["flute_mm"], 18.0)

    def test_geometry_values_map_onto_cam_parameter_keys(self) -> None:
        record = self._record(ToolType.BALL_END_MILL.value,
                              diameter_mm=6.0, length_mm=55.0, flute_length_mm=20.0)
        values = tool_geometry_values(record)
        self.assertEqual(values["tool_kind"], "ball")
        self.assertAlmostEqual(values["tool_diameter_mm"], 6.0)
        self.assertAlmostEqual(values["tool_length_mm"], 55.0)
        self.assertAlmostEqual(values["tool_flute_mm"], 20.0)

    def test_corner_radius_is_rejected_on_non_bull_tools(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(kind=ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0, corner_radius=1.0)

    def test_invalid_geometry_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=0.0, length_mm=30.0)
        with self.assertRaises(ParameterError):
            Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=-1.0)


class ToolRecordTests(unittest.TestCase):
    def test_record_needs_a_name_and_a_safe_id(self) -> None:
        with self.assertRaises(ParameterError):
            ToolRecord(tool_id="ok-1", name="  ", kind=ToolType.DRILL.value)
        with self.assertRaises(ParameterError):
            ToolRecord(tool_id="../escape", name="刀", kind=ToolType.DRILL.value)

    def test_payload_carries_geometry_and_label(self) -> None:
        record = ToolRecord.create("D8 麻花钻", ToolType.DRILL.value, {DIAMETER_KEY: 8.0})
        payload = record.to_payload()
        self.assertEqual(payload["kind_label"], "钻头 Drill")
        self.assertIn("D8", payload["label"])
        self.assertEqual(payload["tool"]["kind"], "flat")
        self.assertAlmostEqual(payload["tool"]["diameter_mm"], 8.0)

    def test_updates_keep_the_id_and_re_normalize(self) -> None:
        record = ToolRecord.create("刀", ToolType.FLAT_END_MILL.value, {DIAMETER_KEY: 6.0})
        changed = record.with_updates(kind=ToolType.BULL_NOSE_MILL.value,
                                      values={"corner_radius_mm": 99.0})
        self.assertEqual(changed.tool_id, record.tool_id)
        self.assertEqual(changed.kind, ToolType.BULL_NOSE_MILL.value)
        self.assertAlmostEqual(changed.values[CORNER_KEY], 3.0)


class ToolRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tplab-tools-")
        self.root = Path(self.directory.name)
        self.repository = ToolRepository(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_empty_library_is_seeded_with_usable_tools(self) -> None:
        tools = self.repository.list()
        self.assertTrue(tools)
        self.assertIn("tool-flat-d10", {item.tool_id for item in tools})
        self.assertTrue((self.root / "tools.json").is_file())

    def test_seeding_can_be_turned_off(self) -> None:
        empty = ToolRepository(self.root / "empty", seed_defaults=False)
        self.assertEqual(empty.list(), [])

    def test_create_read_update_delete_round_trip(self) -> None:
        record = self.repository.create(
            "D12R2 圆鼻刀", ToolType.BULL_NOSE_MILL.value,
            {DIAMETER_KEY: 12.0, CORNER_KEY: 2.0, FLUTE_KEY: 30.0, LENGTH_KEY: 70.0},
        )
        self.assertTrue(self.repository.exists(record.tool_id))
        # 重新打开一个仓库实例：确实落到磁盘了
        reopened = ToolRepository(self.root)
        self.assertEqual(reopened.get(record.tool_id).name, "D12R2 圆鼻刀")

        updated = reopened.update(record.tool_id, name="D12R2 圆鼻刀 v2",
                                  values={CORNER_KEY: 1.5})
        self.assertEqual(updated.name, "D12R2 圆鼻刀 v2")
        self.assertAlmostEqual(updated.values[CORNER_KEY], 1.5)
        # id 与创建时间保留，更新时间往前走
        self.assertEqual(updated.tool_id, record.tool_id)
        self.assertEqual(updated.created_at, record.created_at)

        self.assertTrue(reopened.delete(record.tool_id))
        self.assertFalse(reopened.exists(record.tool_id))
        self.assertFalse(reopened.delete(record.tool_id))

    def test_changing_type_re_normalizes_the_values(self) -> None:
        record = self.repository.create("刀", ToolType.FLAT_END_MILL.value,
                                        {DIAMETER_KEY: 10.0})
        changed = self.repository.update(record.tool_id, kind=ToolType.BALL_END_MILL.value)
        self.assertEqual(changed.kind, ToolType.BALL_END_MILL.value)
        self.assertAlmostEqual(changed.values[CORNER_KEY], 0.0)

    def test_changing_to_bull_nose_keeps_a_sane_corner_radius(self) -> None:
        record = self.repository.create("刀", ToolType.FLAT_END_MILL.value,
                                        {DIAMETER_KEY: 10.0})
        changed = self.repository.update(record.tool_id, kind=ToolType.BULL_NOSE_MILL.value,
                                         values={CORNER_KEY: 50.0})
        self.assertAlmostEqual(changed.values[CORNER_KEY], 5.0)

    def test_names_may_repeat_but_ids_are_unique(self) -> None:
        first = self.repository.create("D6 平底刀", ToolType.FLAT_END_MILL.value)
        second = self.repository.create("D6 平底刀", ToolType.FLAT_END_MILL.value)
        self.assertNotEqual(first.tool_id, second.tool_id)
        self.assertEqual(self.repository.get(first.tool_id).name,
                         self.repository.get(second.tool_id).name)

    def test_duplicate_copies_the_parameters(self) -> None:
        source = self.repository.get("tool-flat-d10")
        clone = self.repository.duplicate(source.tool_id)
        self.assertNotEqual(clone.tool_id, source.tool_id)
        self.assertIn("副本", clone.name)
        self.assertEqual(clone.values, source.values)

    def test_missing_tool_raises_or_resolves_to_none(self) -> None:
        with self.assertRaises(ParameterError):
            self.repository.get("nope")
        self.assertIsNone(self.repository.find("nope"))
        self.assertIsNone(self.repository.resolve("nope"))

    def test_resolve_returns_usable_geometry(self) -> None:
        tool = self.repository.resolve("tool-flat-d10")
        self.assertIsNotNone(tool)
        self.assertAlmostEqual(tool.diameter_mm, 10.0)

    def test_payload_exposes_the_catalog_for_the_ui(self) -> None:
        payload = self.repository.payload()
        self.assertTrue(payload["tools"])
        self.assertEqual(len(payload["catalog"]["types"]), len(TOOL_TYPES))

    def test_broken_records_are_skipped_instead_of_breaking_the_library(self) -> None:
        path = self.root / "tools.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tools"].append({"id": "bad", "name": "", "kind": "flat_end_mill"})
        data["tools"].append({"id": "bad2", "name": "坏类型", "kind": "没有这种刀"})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        names = {item.name for item in self.repository.list()}
        self.assertNotIn("坏类型", names)
        self.assertIn("D6 平底刀", names)

    def test_unreadable_file_behaves_like_an_empty_library(self) -> None:
        (self.root / "tools.json").write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(self.repository.list(), [])

    def test_import_skips_duplicates(self) -> None:
        payload = self.repository.export_payload()
        self.assertEqual(self.repository.import_payload(payload), 0)
        payload["tools"].append({
            "id": "tool-flat-d16", "name": "D16 平底刀", "kind": ToolType.FLAT_END_MILL.value,
            "values": {DIAMETER_KEY: 16.0, LENGTH_KEY: 80.0},
        })
        self.assertEqual(self.repository.import_payload(payload), 1)
        self.assertTrue(self.repository.exists("tool-flat-d16"))

    def test_restore_defaults_only_adds_what_is_missing(self) -> None:
        self.repository.delete("tool-flat-d6")
        restored = {item.tool_id for item in self.repository.restore_defaults()}
        self.assertIn("tool-flat-d6", restored)
        self.assertEqual(self.repository.restore_defaults(), self.repository.list())

    def test_library_survives_a_restart(self) -> None:
        self.repository.create("留下来", ToolType.TAP.value, {DIAMETER_KEY: 6.0, PITCH_KEY: 1.0})
        again = ToolRepository(self.root)
        self.assertIn("留下来", {item.name for item in again.list()})


class ToolpathToolIntegrationTests(unittest.TestCase):
    """刀路计算读的是**刀具库里的那把刀**。"""

    def setUp(self) -> None:
        from tests.fixtures import plate_with_pocket

        self.part = plate_with_pocket(name="刀具集成")
        self.directory = tempfile.TemporaryDirectory(prefix="tplab-toolcam-")
        self.repository = ToolRepository(Path(self.directory.name) / "tools")
        # 加工面：挑一个朝上的平面（型腔凸台顶面），型腔铣沿它分层切
        self.face_id = next(
            face.id for face in self.part.mesh.faces
            if face.is_planar and face.plane is not None and face.plane[2] > 0.5
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _plan(self, payload_parameters):
        from toolpath_lab.cam.service import CAMOperationRequest, execute_operation

        request = CAMOperationRequest.from_payload(
            {
                "kind": "pocket_mill",
                "faces": [self.face_id],
                "parameters": payload_parameters,
            },
            self.part,
            tool_resolver=self.repository.resolve,
        )
        return request, execute_operation(request)

    def test_tool_id_overrides_the_client_side_diameter(self) -> None:
        """客户端送来的旧直径不能盖过刀库：以库为准。"""

        record = self.repository.create("D16 大刀", ToolType.FLAT_END_MILL.value,
                                        {DIAMETER_KEY: 16.0, LENGTH_KEY: 80.0})
        request, result = self._plan({
            "tool_id": record.tool_id,
            "tool_diameter_mm": 3.0,          # 故意送一个错的
            "stepover_ratio": 0.5,
        })
        self.assertAlmostEqual(request.tool.diameter_mm, 16.0)
        self.assertAlmostEqual(request.parameters["tool_diameter_mm"], 16.0)
        self.assertEqual(request.parameters["tool_id"], record.tool_id)
        self.assertIsInstance(result.toolpath, Toolpath)

    def test_unknown_tool_id_is_an_error_not_a_silent_fallback(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest

        with self.assertRaises(ParameterError):
            CAMOperationRequest.from_payload(
                {"kind": "pocket_mill", "faces": [self.face_id],
                 "parameters": {"tool_id": "does-not-exist"}},
                self.part,
                tool_resolver=self.repository.resolve,
            )

    def test_operation_without_tool_id_keeps_the_old_behaviour(self) -> None:
        from toolpath_lab.cam.service import CAMOperationRequest

        request = CAMOperationRequest.from_payload(
            {"kind": "pocket_mill", "faces": [self.face_id],
             "parameters": {"tool_diameter_mm": 9.0}},
            self.part,
        )
        self.assertAlmostEqual(request.tool.diameter_mm, 9.0)
        self.assertEqual(request.parameters.get("tool_id"), "")

    def test_ball_tool_reaches_the_request_as_a_ball(self) -> None:
        record = self.repository.create("球头", ToolType.BALL_END_MILL.value,
                                        {DIAMETER_KEY: 6.0})
        from toolpath_lab.cam.service import CAMOperationRequest

        request = CAMOperationRequest.from_payload(
            {"kind": "pocket_mill", "faces": [self.face_id],
             "parameters": {"tool_id": record.tool_id}},
            self.part,
            tool_resolver=self.repository.resolve,
        )
        self.assertIs(request.tool.kind, ToolKind.BALL)


if __name__ == "__main__":
    unittest.main()
