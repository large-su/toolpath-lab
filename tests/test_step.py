"""STEP 读取层：解析、几何解释、离散与错误处理。

测试用的 STEP 文件由 ``tests/fixtures.py`` 编程生成（四个形状），以及仓库随附的
``examples/sample_plate.step``。两者都是真实的 AP214 B-rep，因此这里断言的是**几何量**
（尺寸、面积、面数、拓扑），而不是"解析没崩"。
"""

from __future__ import annotations

import json
import unittest
from math import pi
from pathlib import Path

import numpy as np

from tests.fixtures import (
    cylinder_boss,
    disc_face_with_closed_circular_edge,
    plate_with_cylinder,
    plate_with_pocket,
    simple_box,
)
from toolpath_lab.step import freeform
from toolpath_lab.step.errors import StepFormatError, StepSizeError, StepUnsupportedError
from toolpath_lab.step.parser import as_ref, parse_step
from toolpath_lab.step.reader import read_step, read_step_bytes
from toolpath_lab.step.tessellate import _weld

#: 直径 30 的圆按 24 段离散出来的多边形面积
HOLE_AREA_24 = 0.5 * 24 * 15 * 15 * np.sin(2 * pi / 24)


class ParserTests(unittest.TestCase):
    """Part 21 语法层。"""

    def test_header_and_data_sections_are_parsed(self) -> None:
        step = parse_step(simple_box())
        self.assertGreater(len(step), 10)
        self.assertIn("FILE_DESCRIPTION", step.header)
        self.assertIn("AUTOMOTIVE_DESIGN", step.schemas[0])
        self.assertEqual(step.count("CARTESIAN_POINT"), 8)
        self.assertEqual(step.count("ADVANCED_FACE"), 6)
        self.assertEqual(step.count("MANIFOLD_SOLID_BREP"), 1)

    def test_rejects_non_step_text(self) -> None:
        for text in ("", "hello", "ISO-10303-21;", "<xml/>"):
            with self.subTest(text=text[:12]):
                with self.assertRaises(StepFormatError):
                    parse_step(text)

    def test_rejects_missing_data_section(self) -> None:
        with self.assertRaises(StepFormatError) as context:
            parse_step("ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n")
        self.assertIn("DATA", str(context.exception))

    def test_schema_name_containing_DATA_is_not_mistaken_for_the_section(self) -> None:
        # AP214 的 schema 名里含 "DATA"，早期的子串匹配会把整个实例表跳过
        step = parse_step(simple_box())
        self.assertTrue(any(entity.keyword == "ADVANCED_FACE" for entity in step.entities.values()))

    def test_syntax_errors_are_reported_as_warnings_not_fatal(self) -> None:
        text = simple_box().replace("#10=EDGE_CURVE('',#1,#9,#8,.T.);", "#10=BROKEN(;")
        step = parse_step(text)
        self.assertTrue(step.warnings)
        self.assertNotIn(10, step.entities)

    def test_entity_count_limit(self) -> None:
        with self.assertRaises(StepSizeError):
            parse_step(simple_box(), max_entities=20)

    def test_describe_reports_keyword_counts(self) -> None:
        described = parse_step(simple_box()).describe()
        self.assertGreater(described["entity_count"], 10)
        self.assertIn("ADVANCED_FACE", described["keywords"])

    def test_booleans_are_read_as_enums_not_references(self) -> None:
        # bool 是 int 的子类，早期会把 .T. 当成实例引用 #True
        step = parse_step(simple_box())
        face = next(entity for entity in step.by_keyword("ADVANCED_FACE"))
        self.assertEqual(str(face.arg(3)), "T")

    def test_derived_attribute_marker_is_parsed_and_keeps_positions(self) -> None:
        """``*``（派生属性）必须被解析成占位值，而不是让整条实例解析失败。

        真实导出器（SolidWorks / SwSTEP）会把 ``ORIENTED_EDGE`` 的两个派生属性写成
        ``*``。早先扫描器不认识 ``*``，于是**每一条 ORIENTED_EDGE 都被整条丢弃**，
        边的拓扑全没了 —— 结果是每个面都取不到边界环，整个模型报"所有面都无法离散"。
        """

        text = (
            "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((''),'2;1');\n"
            "FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));\nENDSEC;\nDATA;\n"
            "#1=ORIENTED_EDGE('NONE',*,*,#7,.T.);\n"
            "#2=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
            "#7=EDGE_CURVE('NONE',#8,#9,#5,.T.);\n"
            "ENDSEC;\nEND-ISO-10303-21;\n"
        )
        step = parse_step(text)
        self.assertEqual(step.warnings, [])
        # 实例必须都在表里（以前这两条都会被丢掉）
        self.assertIn(1, step.entities)
        self.assertIn(2, step.entities)
        # 关键：`*` 占住位置，真正的引用仍在第 4 个参数上，不能被前移
        edge = step.get(1)
        self.assertEqual(as_ref(edge.arg(3)), 7)
        self.assertEqual(str(edge.arg(4)), "T")
        # `*` 不能被误认成实例引用
        self.assertIsNone(as_ref(edge.arg(1)))
        self.assertIsNone(as_ref(edge.arg(2)))

    def test_fixture_uses_the_real_derived_marker(self) -> None:
        """固定装置的 ``ORIENTED_EDGE`` 必须写裸 ``*``，否则测试覆盖不到真实语法。"""

        text = simple_box()
        self.assertIn("ORIENTED_EDGE('',*,*,", text)
        # 带引号的 '*' 是个普通字符串，会让测试永远绕开真实语法
        self.assertNotIn("'*'", text)
        step = parse_step(text)
        self.assertFalse(step.warnings)
        # 6 个面 × 4 条边。修复前这里会是** 0 **——带 `*` 的实例被整条丢弃。
        self.assertEqual(step.count("ORIENTED_EDGE"), 6 * 4)
        # 每条边都能解出对应的 EDGE_CURVE，说明参数下标没错位
        for entity in step.by_keyword("ORIENTED_EDGE"):
            self.assertIsNotNone(step.get(as_ref(entity.arg(3))))

    def test_reversed_oriented_edges_describe_the_same_solid(self) -> None:
        """``ORIENTED_EDGE(..., .F.)`` 必须让整条边反向。

        真实导出器会把一部分边的几何定义成**反方向**，再靠 orientation=.F. 掰回环的走向。
        解析器以前只把 orientation 并进 same_sense、从不交换端点，于是环折线变成来回走的锯齿
        （A→B、B→C、C→B…），面积正好少一半、体积也跟着错 —— 但每张面照样"有面积"、
        网格照样"基本闭合"，所以从统计上完全看不出来。这里对比同一个实体的两种写法。
        """

        plain = read_step_bytes(simple_box().encode("utf-8"))
        mixed = read_step_bytes(simple_box(mixed_orientation=True).encode("utf-8"))

        box_volume = 40.0 * 30.0 * 20.0
        for mesh, label in ((plain, "常规写法"), (mixed, "反向边写法")):
            with self.subTest(style=label):
                self.assertAlmostEqual(mesh.volume_mm3(), box_volume, delta=box_volume * 0.001)
                self.assertAlmostEqual(mesh.surface_area_mm2(),
                                       2 * (40 * 30 + 40 * 20 + 30 * 20), delta=1.0)

    def test_every_line_edge_lies_between_its_vertices(self) -> None:
        """直线边的采样点必须落在两个端点附近。

        直线实体本身是无界的，而真实导出器还会把 ``VECTOR`` 的 magnitude 写成 1000。
        早先按 ``参数域 [0, magnitude]`` 采样，采样点会跑到 origin + magnitude²·dir
        （1e6 量级），整张面的边界被拉出巨大尖刺，面积/体积/渲染全错。
        """

        mesh = read_step_bytes(simple_box().encode("utf-8"))
        for face in mesh.faces:
            for loop in face.loops:
                points = np.asarray(loop, dtype=np.float64)
                # 长方体尺寸 40 × 30 × 20，任何边界点都不该跑到盒子外面去
                self.assertLessEqual(float(np.abs(points).max()), 25.0)

    def test_closed_circular_edge_is_a_full_circle(self) -> None:
        """单条闭合圆边围成的面必须是整张圆面，不能退化成零面积。

        圆柱端盖 / 圆孔底在真实文件里就是这个写法：一个 EDGE_LOOP、一条 ORIENTED_EDGE，
        它的 EDGE_CURVE 是一整圈 CIRCLE 且两个顶点是同一个点（接缝点）。
        按"起止角之间的弧"采样会得到跨度为 0 的一堆重合点，整张面被丢掉。
        """

        radius = 12.5
        mesh = read_step_bytes(disc_face_with_closed_circular_edge(radius).encode("utf-8"))
        self.assertEqual(len(mesh.faces), 1)
        expected = pi * radius ** 2
        # 圆按多边形离散，面积略小于解析值（约 0.3%），给 1% 余量
        self.assertAlmostEqual(mesh.faces[0].area_mm2, expected, delta=expected * 0.01)
        self.assertGreater(mesh.triangle_count, 8)


class TessellationTests(unittest.TestCase):
    """几何层 + 离散层：用解析值校验。"""

    def test_box_geometry_is_exact(self) -> None:
        model = read_step_bytes(simple_box().encode("latin-1"))
        self.assertEqual(model.face_count, 6)
        self.assertEqual(model.size(), (40.0, 30.0, 20.0))
        self.assertAlmostEqual(model.surface_area_mm2(), 2 * (40 * 30 + 40 * 20 + 30 * 20), places=6)
        self.assertAlmostEqual(model.volume_mm3(), 40 * 30 * 20, places=4)
        self.assertTrue(model.is_watertight())

    def test_plate_areas_match_the_analytic_solid(self) -> None:
        model = read_step_bytes(plate_with_pocket().encode("latin-1"))
        expected = (
            2 * (100 * 80)                    # 上下
            + 2 * (100 * 40) + 2 * (80 * 40)  # 四个侧面
            + 60 * 40                         # 型腔底面
            + 2 * (60 * 15) + 2 * (40 * 15)   # 型腔四壁
        )
        self.assertAlmostEqual(model.surface_area_mm2(), expected, delta=expected * 0.02)

    def test_pocket_floor_and_walls_are_present(self) -> None:
        model = read_step_bytes(plate_with_pocket().encode("latin-1"))
        floors = [face for face in model.faces if face.is_planar and face.plane
                  and face.plane[2] > 0.9 and abs(face.plane[3] - 25.0) < 1e-6]
        self.assertEqual(len(floors), 1)
        self.assertAlmostEqual(floors[0].area_mm2, 60 * 40, delta=1.0)

    def test_top_face_keeps_its_hole_loop(self) -> None:
        model = read_step_bytes(plate_with_pocket().encode("latin-1"))
        top = [face for face in model.faces if face.is_planar and face.plane
               and abs(face.plane[3] - 40.0) < 1e-6 and face.normal[2] > 0.9][0]
        # 顶面是"方框"：外轮廓 + 一个方孔，两条环都要留着给型腔铣用
        self.assertEqual(len(top.loops), 2)
        self.assertAlmostEqual(top.area_mm2, 100 * 80 - 60 * 40, delta=1.0)
        for loop in top.loops:
            self.assertEqual(loop.shape[1], 3)
            self.assertGreaterEqual(loop.shape[0], 4)

    def test_cylindrical_face_area_matches_the_prism(self) -> None:
        model = read_step_bytes(cylinder_boss(diameter=60.0, height=50.0).encode("latin-1"))
        self.assertEqual(model.face_normals_grouped().get("cylinder"), 1)
        face = [item for item in model.faces if item.surface_kind == "cylinder"][0]
        radius, height, segments = 30.0, 50.0, 32
        expected = 2.0 * segments * radius * np.sin(pi / segments) * height
        self.assertAlmostEqual(face.area_mm2, expected, delta=expected * 0.02)

    def test_through_hole_is_not_filled(self) -> None:
        model = read_step_bytes(plate_with_cylinder(hole_diameter=30.0).encode("latin-1"))
        tops = [face for face in model.faces
                if face.is_planar and face.plane and face.normal[2] > 0.9
                and abs(face.plane[3] - 40.0) < 1e-6]
        self.assertEqual(len(tops), 1)
        # 顶面 = 100×80 减去 Ø30 的孔（孔按 24 边形离散）
        self.assertAlmostEqual(tops[0].area_mm2, 100 * 80 - HOLE_AREA_24, delta=2.0)
        self.assertEqual(len(tops[0].loops), 2)
        # 孔壁是独立的圆柱面
        self.assertEqual(model.face_normals_grouped().get("cylinder"), 1)

    def test_step_file_on_disk_is_readable(self) -> None:
        path = Path(__file__).resolve().parent.parent / "examples" / "sample_plate.step"
        model = read_step(path)
        self.assertEqual(model.size(), (120.0, 90.0, 40.0))
        self.assertGreater(model.triangle_count, 3)

    def test_unsupported_geometry_raises_rather_than_crashing(self) -> None:
        # 只有线框、没有 B-rep 面的文件
        text = (
            "ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
            "#1=CARTESIAN_POINT('',(0.,0.,0.));\n#2=DIRECTION('',(0.,0.,1.));\n"
            "#3=LINE('',#1,#2);\nENDSEC;\nEND-ISO-10303-21;\n"
        )
        with self.assertRaises(StepUnsupportedError):
            read_step_bytes(text.encode("latin-1"))

    def test_empty_and_oversized_inputs(self) -> None:
        with self.assertRaises(StepFormatError):
            read_step_bytes(b"")
        with self.assertRaises(StepSizeError):
            read_step_bytes(simple_box().encode("latin-1"), max_bytes=64)

    def test_normalisation_puts_the_part_in_machine_coordinates(self) -> None:
        model = read_step_bytes(plate_with_pocket().encode("latin-1"))
        low = model.positions.min(axis=0)
        high = model.positions.max(axis=0)
        self.assertAlmostEqual(float(low[2]), 0.0, places=6)
        self.assertAlmostEqual(float(low[0] + high[0]) / 2.0, 0.0, places=6)
        self.assertAlmostEqual(float(low[1] + high[1]) / 2.0, 0.0, places=6)

    def test_statistics_payload_is_json_friendly(self) -> None:
        stats = read_step_bytes(plate_with_pocket().encode("latin-1")).statistics()
        json.dumps(stats)
        for key in ("vertices", "triangles", "faces", "area_mm2", "size_mm", "watertight"):
            self.assertIn(key, stats)

    def test_every_face_has_a_positive_area(self) -> None:
        for name, text in (("box", simple_box()), ("plate", plate_with_pocket()),
                           ("hole", plate_with_cylinder()), ("boss", cylinder_boss())):
            with self.subTest(name=name):
                model = read_step_bytes(text.encode("latin-1"))
                self.assertTrue(all(face.area_mm2 > 0 for face in model.faces))
                self.assertTrue(all(face.triangle_count > 0 for face in model.faces))


class GeometryHelperTests(unittest.TestCase):
    """几何小工具：B 样条求值、圆参数、焊接。"""

    def test_uniform_knots_are_clamped(self) -> None:
        knots = freeform.uniform_knots(5, 3)
        self.assertEqual(knots.shape[0], 5 + 3 + 1)
        self.assertEqual(float(knots[0]), 0.0)
        self.assertEqual(float(knots[-1]), 1.0)
        self.assertEqual(float(knots[3]), 0.0)

    def test_expand_knots_uses_multiplicities(self) -> None:
        knots = freeform.expand_knots([2, 1, 2], [0.0, 0.5, 1.0], control_count=4, degree=2)
        np.testing.assert_allclose(knots, [0.0, 0.0, 0.0, 0.5, 1.0, 1.0, 1.0])

    def test_expand_knots_forces_a_clamped_vector(self) -> None:
        # 不规范的文件（重复度不足）也要补成夹紧节点向量，否则曲线不经过端点
        knots = freeform.expand_knots([3, 1, 3], [0.0, 0.5, 1.0], control_count=5, degree=2)
        self.assertEqual(knots.shape[0], 5 + 2 + 1)
        self.assertEqual(float(knots[2]), 0.0)
        self.assertEqual(float(knots[-3]), 1.0)

    def test_curve_evaluation_reproduces_a_bezier_arc(self) -> None:
        control = np.array([[0.0, 0.0, 0.0], [1.0, 2.0, 0.0], [2.0, 0.0, 0.0]])
        weights = np.ones(3)
        knots = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0])
        values = freeform.curve_point_parameters(
            np.array([0.0, 0.5, 1.0]), control, weights, knots, 2
        )
        np.testing.assert_allclose(values[0], [0.0, 0.0, 0.0], atol=1e-9)
        np.testing.assert_allclose(values[1], [1.0, 1.0, 0.0], atol=1e-9)
        np.testing.assert_allclose(values[2], [2.0, 0.0, 0.0], atol=1e-9)

    def test_surface_grid_matches_manual_evaluation(self) -> None:
        control = np.array([
            [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            [[1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
        ])
        weights = np.ones((2, 2))
        knots = np.array([0.0, 0.0, 1.0, 1.0])
        grid = freeform.surface_grid(control, weights, knots, knots, 1, 1,
                                     np.array([0.0, 1.0]), np.array([0.0, 1.0]))
        self.assertEqual(grid.shape, (2, 2, 3))
        np.testing.assert_allclose(grid[0, 0], [0.0, 0.0, 0.0], atol=1e-9)
        np.testing.assert_allclose(grid[1, 1], [1.0, 1.0, 0.0], atol=1e-9)
        np.testing.assert_allclose(grid[0, 1], [0.0, 1.0, 0.0], atol=1e-9)

    def test_rational_surface_evaluation_stays_on_the_plane(self) -> None:
        # 权重不为 1 时（NURBS）也必须落在有理曲面上：平面上的 NURBS 仍是同一个平面
        control = np.array([
            [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            [[1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
        ])
        weights = np.array([[1.0, 2.0], [3.0, 4.0]])
        knots = np.array([0.0, 0.0, 1.0, 1.0])
        grid = freeform.surface_grid(control, weights, knots, knots, 1, 1,
                                     np.array([0.25, 0.5]), np.array([0.3]))
        np.testing.assert_allclose(grid[:, :, 2], 0.0, atol=1e-12)

    def test_circle_arc_parameters_use_the_requested_direction(self) -> None:
        from toolpath_lab.step.geometry import Circle
        circle = Circle(center=np.zeros(3), axis_x=np.array([1.0, 0.0, 0.0]),
                        axis_y=np.array([0.0, 1.0, 0.0]), radius=10.0)
        start = circle.point(0.0)
        end = circle.point(pi / 2)
        low, high = circle.arc_parameters(start, end, True)
        self.assertAlmostEqual(high - low, pi / 2, places=9)
        low, high = circle.arc_parameters(end, start, True)
        self.assertAlmostEqual(high - low, 3 * pi / 2, places=9)

    def test_weld_merges_coincident_vertices(self) -> None:
        points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        triangles = np.array([[0, 1, 2], [3, 1, 2]])
        welded, indices = _weld(points, triangles)
        self.assertEqual(welded.shape[0], 2)
        self.assertTrue(np.all(indices < 2))


if __name__ == "__main__":
    unittest.main()
