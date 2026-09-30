"""BRep 层（OCP）测试。

用 OCP **自己**生成测试实体再读写，所以不依赖仓库里任何外部模型文件，也不依赖
手写夹具 —— 顺带说明一下：``examples/sample_plate.step`` 是手写夹具生成的，
OpenCascade 认为它不是合法实体模型（读出来 0 个面），因此不能拿它测 OCP。

没装 OCP 时整组跳过。
"""

from __future__ import annotations

import tempfile
import unittest
from math import pi
from pathlib import Path

import numpy as np

from toolpath_lab.brep.backend import OCP_AVAILABLE

if OCP_AVAILABLE:
    from toolpath_lab.brep import (BrepFormatError, BrepModel, BrepSizeError,
                                   BrepUnsupportedError, MeshOptions, SliceOptions,
                                   import_model, import_model_bytes, load_brep,
                                   probe_model, slice_at, slice_range,
                                   to_tessellated_model)

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt


def _plate_with_hole(width: float = 100.0, depth: float = 80.0, height: float = 20.0,
                     hole_diameter: float = 30.0) -> str:
    """用 OCP 造一块带通孔的板，写成临时 STEP，返回路径。

    体积 = 长×宽×高 − πr²h（解析值），测试拿它当基准。
    """

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), width, depth, height).Shape()
    axis = gp_Ax2(gp_Pnt(width / 2, depth / 2, -1.0), gp_Dir(0, 0, 1))
    cylinder = BRepPrimAPI_MakeCylinder(axis, hole_diameter / 2, height + 2.0).Shape()
    solid = BRepAlgoAPI_Cut(box, cylinder).Shape()

    writer = STEPControl_Writer()
    writer.Transfer(solid, STEPControl_AsIs)
    path = Path(tempfile.mkdtemp(prefix="tplab-brep-")) / "plate.step"
    writer.Write(str(path))
    return str(path)


@unittest.skipUnless(OCP_AVAILABLE, "需要 OCP（pip install cadquery-ocp）")
class BrepModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = _plate_with_hole()

    def test_reads_step_and_reports_exact_geometry(self) -> None:
        model = load_brep(self.path)
        # 解析值：100*80*20 - pi*15^2*20
        expected = 100 * 80 * 20 - pi * 225 * 20
        self.assertAlmostEqual(model.volume_mm3(), expected, delta=expected * 1e-6)
        # 长方体 6 个面 + 通孔孔壁 1 个圆柱面
        self.assertEqual(model.face_count, 7)
        # 布尔运算 + STEP 往返会留下 ~2e-7mm 的容差（0.2 纳米），物理上无意义，按位比较要留余量
        for actual, expected_size in zip(model.size(), (100.0, 80.0, 20.0)):
            self.assertAlmostEqual(actual, expected_size, places=5)
        self.assertTrue(model.is_valid())

    def test_volume_is_exact_not_mesh_approximated(self) -> None:
        """BRep 体积是解析算的，不该有"多边形近似"那种千分之几的误差。"""

        model = load_brep(self.path)
        expected = 100 * 80 * 20 - pi * 225 * 20
        self.assertLess(abs(model.volume_mm3() - expected) / expected, 1e-9)

    def test_surface_area_matches_analytic(self) -> None:
        model = load_brep(self.path)
        expected = (2 * (100 * 80 - pi * 225)          # 上下两面各挖掉一个圆
                    + 2 * (100 * 20) + 2 * (80 * 20)   # 四个侧面
                    + 2 * pi * 15 * 20)                # 孔壁
        self.assertAlmostEqual(model.surface_area_mm2(), expected, delta=expected * 1e-6)

    def test_topology_counts(self) -> None:
        model = load_brep(self.path)
        counts = model.counts()
        self.assertEqual(counts["solids"], 1)
        self.assertEqual(counts["faces"], 7)
        self.assertGreater(counts["edges"], 10)

    def test_face_kinds_are_classified(self) -> None:
        model = load_brep(self.path)
        kinds = {face.kind for face in model.faces}
        self.assertIn("plane", kinds)
        self.assertIn("cylinder", kinds)
        # 长方体 6 个平面 + 1 个圆柱孔壁
        self.assertEqual(sum(1 for face in model.faces if face.is_planar), 6)
        self.assertEqual(sum(1 for face in model.faces if face.kind == "cylinder"), 1)

    def test_normalize_puts_part_in_machine_coordinates(self) -> None:
        model = load_brep(self.path, normalize=True)
        x0, y0, z0, x1, y1, z1 = model.bounds()
        self.assertAlmostEqual(x0, -50.0, places=6)
        self.assertAlmostEqual(x1, 50.0, places=6)
        self.assertAlmostEqual(y0, -40.0, places=6)
        self.assertAlmostEqual(z0, 0.0, places=6)
        self.assertAlmostEqual(z1, 20.0, places=6)

    def test_translation_keeps_volume(self) -> None:
        model = load_brep(self.path)
        moved = model.translated((10.0, -5.0, 3.0))
        self.assertAlmostEqual(moved.volume_mm3(), model.volume_mm3(), places=6)
        self.assertNotAlmostEqual(moved.bounds()[0], model.bounds()[0], places=6)

    def test_rejects_unsupported_suffix(self) -> None:
        with self.assertRaises(ValueError):
            load_brep(str(Path(self.path).with_suffix(".txt")))

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_brep("不存在的文件.step")


@unittest.skipUnless(OCP_AVAILABLE, "需要 OCP")
class TessellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = load_brep(_plate_with_hole())

    def test_tessellation_produces_mesh(self) -> None:
        result = to_tessellated_model(self.model, MeshOptions(linear_deflection=0.05))
        self.assertGreater(result.triangle_count, 20)
        self.assertEqual(result.positions.shape[1], 3)
        self.assertEqual(result.indices.shape[1], 3)
        self.assertEqual(len(result.face_of_triangle), result.triangle_count)
        self.assertGreater(result.vertex_count, 0)

    def test_finer_deflection_gives_more_triangles(self) -> None:
        coarse = to_tessellated_model(self.model, MeshOptions(linear_deflection=1.0))
        fine = to_tessellated_model(self.model, MeshOptions(linear_deflection=0.01))
        self.assertGreater(fine.triangle_count, coarse.triangle_count)

    def test_face_records_cover_every_triangle(self) -> None:
        """每个三角形都必须归属到某个面，且各面的区间首尾相接、不重不漏。"""

        result = to_tessellated_model(self.model)
        total = sum(face.triangle_count for face in result.faces)
        self.assertEqual(total, result.triangle_count)
        self.assertEqual(len(result.faces), self.model.face_count)
        expected_start = 0
        for face in result.faces:
            self.assertEqual(face.triangle_start, expected_start)
            expected_start += face.triangle_count

    def test_every_face_has_boundary_loops(self) -> None:
        """CAM 的加工区域直接吃 ``face.loops``，所以每个面都必须有边界环。"""

        result = to_tessellated_model(self.model)
        for face in result.faces:
            with self.subTest(face=face.id):
                self.assertGreaterEqual(face.loop_count, 1)
                self.assertTrue(face.loops)
                for loop in face.loops:
                    self.assertEqual(loop.shape[1], 3)
                    self.assertGreaterEqual(loop.shape[0], 3)

    def test_mesh_volume_approximates_brep_volume(self) -> None:
        """离散后的网格算体积应当逼近 BRep 的解析体积（差的就是多边形近似量）。"""

        result = to_tessellated_model(self.model, MeshOptions(linear_deflection=0.01))
        mesh_volume = _signed_volume(result.positions, result.indices)
        expected = self.model.volume_mm3()
        self.assertLess(abs(abs(mesh_volume) - expected) / expected, 0.005)

    def test_normalized_mesh_sits_on_the_machine_table(self) -> None:
        result = to_tessellated_model(self.model, normalize=True)
        x0, y0, z0, x1, y1, z1 = result.bounds()
        self.assertAlmostEqual(z0, 0.0, places=6)
        self.assertAlmostEqual(x0, -50.0, delta=0.2)
        self.assertAlmostEqual(y0, -40.0, delta=0.2)


@unittest.skipUnless(OCP_AVAILABLE, "需要 OCP")
class SectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = load_brep(_plate_with_hole(), normalize=True)

    def test_slice_through_the_hole_has_a_hole(self) -> None:
        """Z=10 穿过通孔：截面应当是 100x80 减去一个 D30 的圆。"""

        result = slice_at(self.model, 10.0)
        self.assertEqual(len(result.regions), 1)
        region = result.regions[0]
        self.assertEqual(len(region.holes), 1)
        self.assertAlmostEqual(region.area, 100 * 80 - pi * 225, delta=2.0)

    def test_slice_near_the_bottom_still_has_the_hole(self) -> None:
        """**通**孔的截面在任意高度都有孔，靠近底面也一样。"""

        result = slice_at(self.model, 0.5)
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(len(result.regions[0].holes), 1)
        self.assertAlmostEqual(result.area_mm2, 100 * 80 - pi * 225, delta=2.0)

    def test_slice_outside_the_model_is_empty(self) -> None:
        result = slice_at(self.model, 100.0)
        self.assertEqual(result.regions, ())
        self.assertAlmostEqual(result.area_mm2, 0.0, places=9)

    def test_slice_range_avoids_the_exact_surfaces(self) -> None:
        """分层高度必须严格落在模型内部。

        切平面正好压在上下表面上时，平面与面共面，截面可能只剩掠射的碎边
        （实测 Example2 在某层刚好切到型腔底面，就出现 1 条拼不上闭环的边）。
        ``slice_range`` 要主动避开这些高度。
        """

        x0, y0, z0, x1, y1, z1 = self.model.bounds()
        layers = slice_range(self.model, step_mm=5.0)
        for layer in layers:
            self.assertGreater(layer.z, z0 + 1e-6)
            self.assertLess(layer.z, z1 - 1e-6)

    def test_no_open_chains_in_normal_slices(self) -> None:
        """正常工作高度上不该有拼不起来的边（拼不上说明轮廓不完整）。"""

        for z in (2.5, 7.5, 12.5, 17.5):
            with self.subTest(z=z):
                self.assertEqual(slice_at(self.model, z).open_chains, 0)

    def test_slice_range_covers_expected_levels(self) -> None:
        layers = slice_range(self.model, step_mm=5.0, options=SliceOptions(deflection=0.05))
        self.assertEqual(len(layers), 3)          # Z = 5, 10, 15（0 与 20 是上下表面，不含）
        self.assertTrue(all(layer.area_mm2 > 0 for layer in layers))
        self.assertEqual([round(layer.z, 3) for layer in layers], [5.0, 10.0, 15.0])

    def test_stepover_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            slice_range(self.model, step_mm=0.0)

    def test_slice_payload_is_json_friendly(self) -> None:
        import json

        payload = slice_at(self.model, 10.0).to_payload()
        json.dumps(payload)     # 不抛异常即可
        self.assertIn("regions", payload)


@unittest.skipUnless(OCP_AVAILABLE, "需要 OCP（pip install cadquery-ocp）")
class ImporterTests(unittest.TestCase):
    """生产入口 ``import_model`` / ``import_model_bytes``（HTTP 上传走的就是它）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = _plate_with_hole()
        cls.data = Path(cls.path).read_bytes()

    def test_import_from_path(self) -> None:
        model = import_model(self.path)
        self.assertEqual(model.face_count, 7)
        self.assertGreater(model.triangle_count, 20)
        # 归一化后 Z 从 0 起
        self.assertAlmostEqual(model.bounds()[2], 0.0, places=6)

    def test_import_from_bytes(self) -> None:
        model = import_model_bytes(self.data, source_name="plate.step")
        self.assertEqual(model.face_count, 7)
        self.assertAlmostEqual(model.bounds()[2], 0.0, places=6)

    def test_volume_is_the_analytic_one(self) -> None:
        model = import_model(self.path)
        expected = 100 * 80 * 20 - pi * 225 * 20
        self.assertAlmostEqual(model.volume_mm3(), expected, delta=expected * 1e-3)

    def test_bytes_without_suffix_is_rejected(self) -> None:
        with self.assertRaises(BrepFormatError):
            import_model_bytes(self.data, source_name="model")

    def test_wrong_suffix_is_rejected(self) -> None:
        with self.assertRaises(BrepFormatError):
            import_model_bytes(b"whatever", source_name="model.txt")

    def test_empty_payload_is_rejected(self) -> None:
        with self.assertRaises(BrepFormatError):
            import_model_bytes(b"", source_name="plate.step")

    def test_garbage_is_rejected(self) -> None:
        with self.assertRaises((BrepFormatError, BrepUnsupportedError)):
            import_model_bytes(b"this is definitely not a STEP file", source_name="x.step")

    def test_oversize_payload_is_rejected_with_the_right_error(self) -> None:
        """体积超限要抛 :class:`BrepSizeError`，HTTP 层才能映射成 413。"""

        with self.assertRaises(BrepSizeError):
            import_model_bytes(self.data, source_name="plate.step", max_bytes=64)

    def test_probe_does_not_parse_geometry(self) -> None:
        result = probe_model(self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], Path(self.path).name)
        self.assertFalse(probe_model(self.path, max_bytes=8)["ok"])

    def test_iges_suffix_is_accepted_but_bad_content_still_fails(self) -> None:
        with self.assertRaises((BrepFormatError, BrepUnsupportedError)):
            import_model_bytes(b"not iges", source_name="x.igs")


def _signed_volume(positions: np.ndarray, indices: np.ndarray) -> float:
    """闭合网格的有向体积（散度定理）。"""

    if positions.size == 0 or indices.size == 0:
        return 0.0
    a = positions[indices[:, 0]]
    b = positions[indices[:, 1]]
    c = positions[indices[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


if __name__ == "__main__":
    unittest.main()
