"""导入模型：STL 解析、投影轮廓与 Z-map 高度场。"""

from __future__ import annotations

import struct
import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.mesh import (
    ModelLibrary,
    PICK_BOTTOM,
    PICK_TOP,
    HeightField,
    Mesh,
    build_height_field,
    convex_hull_2d,
    load_stl,
)


def binary_stl(triangles) -> bytes:
    """由三角形列表拼一个二进制 STL。"""

    header = b"toolpath-lab".ljust(80, b"\0")
    body = bytearray(struct.pack("<I", len(triangles)))
    for triangle in triangles:
        body += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vertex in triangle:
            body += struct.pack("<3f", *vertex)
        body += struct.pack("<H", 0)
    return header + bytes(body)


def ascii_stl(triangles) -> bytes:
    lines = ["solid test"]
    for triangle in triangles:
        lines.append("facet normal 0 0 1")
        lines.append("  outer loop")
        for vertex in triangle:
            lines.append("    vertex " + " ".join(str(value) for value in vertex))
        lines.append("  endloop")
        lines.append("endfacet")
    lines.append("endsolid test")
    return "\n".join(lines).encode("utf-8")


def square_plane(side: float = 20.0, *, slope: float = 0.0, height: float = 0.0) -> Mesh:
    """一个覆盖整块方形的平面网格，z = height + slope * x。"""

    half = side / 2.0

    def point(x: float, y: float):
        return (x, y, height + slope * x)

    triangles = [
        [point(-half, -half), point(half, -half), point(half, half)],
        [point(-half, -half), point(half, half), point(-half, half)],
    ]
    return Mesh(np.array(triangles, dtype=np.float64))


class StlParsingTests(unittest.TestCase):
    def test_binary_stl_round_trip(self) -> None:
        triangles = [
            [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)],
            [(0.0, 0.0, 5.0), (0.0, 10.0, 5.0), (10.0, 0.0, 5.0)],
        ]
        mesh = load_stl(binary_stl(triangles))
        self.assertEqual(mesh.triangle_count, 2)
        self.assertEqual(mesh.bounds, (0.0, 10.0, 0.0, 10.0, 0.0, 5.0))
        self.assertTrue(
            np.allclose(mesh.triangles[1], np.array(triangles[1], dtype=np.float64))
        )

    def test_ascii_stl_is_parsed(self) -> None:
        triangles = [[(0.0, 0.0, 0.0), (5.0, 0.0, 1.0), (0.0, 5.0, 2.0)]]
        mesh = load_stl(ascii_stl(triangles))
        self.assertEqual(mesh.triangle_count, 1)
        self.assertEqual(mesh.size_mm, (5.0, 5.0, 2.0))

    def test_unreadable_payload_is_rejected(self) -> None:
        for payload in (b"", b"hello world", b"solid but no facets here"):
            with self.subTest(payload=payload):
                with self.assertRaises(ParameterError):
                    load_stl(payload)

    def test_mesh_validates_its_triangles(self) -> None:
        with self.assertRaises(ParameterError):
            Mesh(np.zeros((0, 3, 3), dtype=np.float64))
        with self.assertRaises(ParameterError):
            Mesh(np.array([[[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [np.nan, 0.0, 0.0]]]))


class OutlineTests(unittest.TestCase):
    def test_box_outline_is_the_bounding_box(self) -> None:
        outline = square_plane(20.0).xy_outline("box")
        self.assertEqual(outline.tolist(), [[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0]])

    def test_hull_skips_points_inside_the_outline(self) -> None:
        points = np.array(
            [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [5.0, 5.0], [2.0, 7.0]]
        )
        hull = convex_hull_2d(points)
        self.assertEqual(hull.shape, (4, 2))
        self.assertNotIn([5.0, 5.0], hull.tolist())

    def test_hull_is_counter_clockwise(self) -> None:
        points = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [-5.0, 5.0]])
        hull = convex_hull_2d(points)
        x = hull[:, 0]
        y = hull[:, 1]
        area = 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)
        self.assertGreater(area, 0.0)

    def test_unknown_outline_mode_raises(self) -> None:
        with self.assertRaises(ParameterError):
            square_plane().xy_outline("circle")

    def test_degenerate_projection_raises(self) -> None:
        mesh = Mesh(np.array([[[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 2.0]]]))
        with self.assertRaises(ParameterError):
            mesh.xy_outline("hull")


class HeightFieldTests(unittest.TestCase):
    def test_flat_mesh_gives_a_constant_height(self) -> None:
        field, warnings = build_height_field(
            square_plane(20.0, height=7.0), resolution_mm=1.0
        )
        self.assertEqual(warnings, ())
        self.assertAlmostEqual(field.coverage, 1.0, places=6)
        heights = field.heights(np.array([[-9.0, -9.0], [0.0, 0.0], [9.5, 3.0]]))
        self.assertTrue(np.allclose(heights, 7.0, atol=1e-6))
        self.assertEqual(field.z_range_mm, (7.0, 7.0))

    def test_sloped_mesh_is_interpolated(self) -> None:
        field, _ = build_height_field(square_plane(20.0, slope=0.5), resolution_mm=1.0)
        heights = field.heights(np.array([[-10.0, 0.0], [0.0, 0.0], [10.0, 0.0]]))
        self.assertTrue(np.allclose(heights, [-5.0, 0.0, 5.0], atol=1e-6))

    def test_points_outside_the_grid_are_clamped(self) -> None:
        field, _ = build_height_field(square_plane(20.0, height=1.0), resolution_mm=2.0)
        heights = field.heights(np.array([[-500.0, -500.0], [500.0, 500.0]]))
        self.assertTrue(np.allclose(heights, 1.0))

    def test_pick_selects_the_top_or_the_bottom_face(self) -> None:
        top = square_plane(20.0, height=10.0)
        bottom = Mesh(top.triangles * np.array([1.0, 1.0, 0.0]))
        stacked = Mesh(np.vstack([top.triangles, bottom.triangles]))
        highest, _ = build_height_field(stacked, resolution_mm=1.0, pick=PICK_TOP)
        lowest, _ = build_height_field(stacked, resolution_mm=1.0, pick=PICK_BOTTOM)
        self.assertEqual(highest.z_range_mm, (10.0, 10.0))
        self.assertEqual(lowest.z_range_mm, (0.0, 0.0))

    def test_holes_are_filled_with_the_nearest_height(self) -> None:
        # 只有半个方形：包围盒的另一半没有三角形覆盖，覆盖度因此小于 1。
        half = Mesh(
            np.array(
                [
                    [[-10.0, -10.0, 3.0], [10.0, -10.0, 3.0], [-10.0, 10.0, 3.0]],
                ]
            )
        )
        field, warnings = build_height_field(half, resolution_mm=1.0)
        self.assertLess(field.coverage, 0.6)
        self.assertTrue(any("投影" in warning for warning in warnings))
        heights = field.heights(np.array([[9.0, 9.0], [-9.0, -9.0]]))
        self.assertTrue(np.all(np.isfinite(heights)))
        self.assertTrue(np.allclose(heights, 3.0, atol=1e-6))

    def test_resolution_is_relaxed_when_the_grid_would_be_too_large(self) -> None:
        field, warnings = build_height_field(
            square_plane(100.0), resolution_mm=0.01, max_nodes=10_000
        )
        self.assertLessEqual(field.node_count, 10_000)
        self.assertGreater(field.resolution_mm, 1.0)
        self.assertTrue(any("分辨率" in warning for warning in warnings))

    def test_unknown_pick_is_rejected(self) -> None:
        with self.assertRaises(ParameterError):
            build_height_field(square_plane(), pick="middle")

    def test_height_field_describes_itself(self) -> None:
        field = HeightField(
            xs=np.array([0.0, 1.0]),
            ys=np.array([0.0, 1.0]),
            zs=np.array([[0.0, 1.0], [1.0, 2.0]]),
        )
        described = field.describe()
        self.assertEqual(described["node_count"], 4)
        self.assertEqual(described["pick"], PICK_TOP)
        self.assertEqual(described["resolution_mm"], 1.0)


class MeshPayloadTests(unittest.TestCase):
    def test_payload_sends_every_triangle_by_default(self) -> None:
        model = ModelLibrary().add("plate.stl", b"", mesh=square_plane())
        payload = model.mesh_payload()
        self.assertEqual(payload["triangle_count"], 2)
        self.assertEqual(payload["display_triangle_count"], 2)
        self.assertFalse(payload["simplified"])
        self.assertEqual(len(payload["positions"]), 2 * 3 * 3)

    def test_payload_simplifies_huge_meshes(self) -> None:
        mesh = square_plane()
        big = Mesh(np.tile(mesh.triangles, (20, 1, 1)))
        model = ModelLibrary().add("big.stl", b"", mesh=big)
        payload = model.mesh_payload(max_triangles=8)
        self.assertEqual(payload["triangle_count"], 40)
        self.assertTrue(payload["simplified"])
        self.assertLess(payload["display_triangle_count"], 40)


class ModelLibraryTests(unittest.TestCase):
    def test_add_get_and_describe(self) -> None:
        library = ModelLibrary()
        model = library.add("plate.stl", b"", mesh=square_plane(20.0))
        self.assertTrue(model.id.startswith("m-"))
        self.assertEqual(library.get(model.id).name, "plate.stl")
        self.assertEqual(model.describe()["size_mm"], [20.0, 20.0, 0.0])
        self.assertEqual(len(library), 1)

    def test_unknown_model_raises_a_parameter_error(self) -> None:
        with self.assertRaises(ParameterError):
            ModelLibrary().get("m-nope")

    def test_maybe_treats_an_empty_id_as_no_model(self) -> None:
        library = ModelLibrary()
        self.assertIsNone(library.maybe(""))
        self.assertIsNone(library.maybe(None))
        model = library.add("plate.stl", b"", mesh=square_plane())
        self.assertIs(library.maybe(model.id), model)

    def test_remove_reports_whether_something_went(self) -> None:
        library = ModelLibrary()
        model = library.add("plate.stl", b"", mesh=square_plane())
        self.assertTrue(library.remove(model.id))
        self.assertFalse(library.remove(model.id))

    def test_oldest_models_are_evicted(self) -> None:
        library = ModelLibrary(max_models=2)
        first = library.add("a.stl", b"", mesh=square_plane())
        library.add("b.stl", b"", mesh=square_plane())
        library.add("c.stl", b"", mesh=square_plane())
        self.assertEqual(len(library), 2)
        with self.assertRaises(ParameterError):
            library.get(first.id)

    def test_height_fields_are_cached_per_model(self) -> None:
        model = ModelLibrary().add("plate.stl", b"", mesh=square_plane(20.0, height=4.0))
        first = model.height_field(1.0, PICK_TOP)
        self.assertIs(first, model.height_field(1.0, PICK_TOP))
        self.assertEqual(model.field_warnings(1.0, PICK_TOP), ())


if __name__ == "__main__":
    unittest.main()
