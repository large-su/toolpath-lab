"""工程持久化：JSON 文件仓库的读写、网格往返与容错。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests.fixtures import plate_with_pocket
from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.operation import Operation, ParameterTemplate
from toolpath_lab.core.part import build_part
from toolpath_lab.step.reader import read_step_bytes
from toolpath_lab.storage.repository import Project, ProjectRepository


def make_project(payload: str = plate_with_pocket()) -> Project:
    model = read_step_bytes(payload.encode("latin-1"), source_name="plate")
    part = build_part(model, model_id="p1", name="板件")
    project = Project(project_id=ProjectRepository.new_id("test"), name="测试工程", part=part)
    project.stock_parameters = {"offset_x_mm": 3.0, "offset_y_mm": 3.0, "offset_z_mm": 1.5}
    project.cam_parameters = {"stepover_mm": 4.0}
    return project


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tplab-test-")
        self.repository = ProjectRepository(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_save_creates_files_and_index(self) -> None:
        project = make_project()
        self.repository.save(project)
        directory = Path(self.directory.name) / project.project_id
        self.assertTrue((directory / "project.json").is_file())
        self.assertTrue((directory / "mesh.npz").is_file())
        index = self.repository.index()
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["id"], project.project_id)

    def test_round_trip_preserves_geometry(self) -> None:
        project = make_project()
        project.tree.add(Operation(operation_id="op1", name="型腔铣", kind="pocket_mill",
                                   parameters={"stepover_mm": 3.0}, face_ids=(197,)))
        project.tree.update("op1", state="generated")
        self.repository.save(project)

        loaded = self.repository.load(project.project_id)
        self.assertEqual(loaded.name, project.name)
        self.assertEqual(loaded.part.name, project.part.name)
        self.assertEqual(loaded.part.face_count, project.part.face_count)
        self.assertEqual(loaded.part.size, project.part.size)
        np.testing.assert_allclose(loaded.part.mesh.positions, project.part.mesh.positions)
        np.testing.assert_array_equal(loaded.part.mesh.indices, project.part.mesh.indices)
        self.assertAlmostEqual(loaded.part.mesh.surface_area_mm2(),
                               project.part.mesh.surface_area_mm2(), places=6)

    def test_round_trip_preserves_the_operation_tree(self) -> None:
        project = make_project()
        for index in range(3):
            project.tree.add(Operation(operation_id=f"op{index}", name=f"工序{index}",
                                       kind="pocket_mill", parameters={"stepover_mm": 2.0 + index},
                                       face_ids=(197,)))
        project.tree.move("op2", 0)
        project.tree.update("op0", enabled=False)
        project.tree.add_template(ParameterTemplate(template_id="t1", name="开粗",
                                                    kind="pocket_mill",
                                                    parameters={"stepover_mm": 4.0}))
        self.repository.save(project)

        loaded = self.repository.load(project.project_id)
        self.assertEqual([item.operation_id for item in loaded.tree.ordered()],
                         [item.operation_id for item in project.tree.ordered()])
        self.assertEqual([item.sequence for item in loaded.tree.ordered()], [0, 1, 2])
        self.assertEqual(loaded.tree.get("op0").enabled, False)
        self.assertEqual(loaded.tree.template("t1").parameters["stepover_mm"], 4.0)
        self.assertEqual(loaded.stock_parameters["offset_x_mm"], 3.0)
        self.assertEqual(loaded.cam_parameters["stepover_mm"], 4.0)

    def test_faces_and_features_survive(self) -> None:
        project = make_project()
        self.repository.save(project)
        loaded = self.repository.load(project.project_id)
        self.assertEqual(len(loaded.part.features), project.part.face_count)
        top = [f for f in loaded.part.features
               if f["horizontal"] and abs(f["plane"][3] - 40.0) < 1e-6]
        self.assertEqual(len(top), 1)
        # 面的边界环是型腔铣的输入，必须一起存下来
        record = loaded.part.face(top[0]["face_id"])
        self.assertIsNotNone(record)
        self.assertGreaterEqual(len(record.loops), 1)

    def test_delete_removes_files_and_index_entry(self) -> None:
        project = make_project()
        self.repository.save(project)
        self.assertTrue(self.repository.exists(project.project_id))
        self.assertTrue(self.repository.delete(project.project_id))
        self.assertFalse(self.repository.exists(project.project_id))
        self.assertEqual(self.repository.index(), [])
        self.assertFalse(self.repository.delete(project.project_id))

    def test_index_is_sorted_by_update_time(self) -> None:
        first = self.repository.save(make_project())
        second = self.repository.save(make_project())
        index = self.repository.index()
        self.assertEqual(index[0]["id"], second.project_id)
        self.assertIn(first.project_id, [item["id"] for item in index])

    def test_illegal_ids_are_rejected(self) -> None:
        for bad in ("../escape", "a/b", "", "x" * 80, "name with space"):
            with self.subTest(bad=bad):
                with self.assertRaises(ParameterError):
                    self.repository.load(bad)

    def test_missing_project_raises(self) -> None:
        with self.assertRaises(ParameterError):
            self.repository.load("does-not-exist")

    def test_corrupt_index_is_tolerated(self) -> None:
        (Path(self.directory.name) / "index.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.repository.index(), [])
        project = make_project()
        self.repository.save(project)
        self.assertEqual(len(self.repository.index()), 1)

    def test_project_payload_is_json_serialisable(self) -> None:
        project = make_project()
        text = json.dumps(project.to_payload(include_mesh=False), ensure_ascii=False)
        self.assertIn("测试工程", text)
        summary = project.summary()
        json.dumps(summary)
        self.assertEqual(summary["part"]["faces"], project.part.face_count)

    def test_stock_is_rebuilt_from_the_part(self) -> None:
        project = make_project()
        project.stock_id = "cylindrical"
        stock = project.stock()
        self.assertEqual(stock.id, "cylindrical")
        self.assertAlmostEqual(stock.bounds.size[0], stock.bounds.size[1], places=6)


if __name__ == "__main__":
    unittest.main()
