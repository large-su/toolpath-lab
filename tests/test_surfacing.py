"""3 轴曲面加工层（opencamlib + OCP/pyclipper）测试。

验收都对着**解析值**：平板与型腔的刀心高度、圆柱的等高线半径。
夹具几何由 OCP 生成（见 :mod:`tests.fixtures`），不用手写网格。

没装 opencamlib / OCP 时整组跳过。
"""

from __future__ import annotations

import unittest
from math import pi

import numpy as np

from toolpath_lab.brep.backend import OCP_AVAILABLE
from toolpath_lab.surfacing.backend import OCL_AVAILABLE

if OCL_AVAILABLE and OCP_AVAILABLE:
    from tests.fixtures import plate_with_pocket, plate_with_cylinder
    from toolpath_lab.brep.model import BrepModel
    from toolpath_lab.core.path import MoveKind
    from toolpath_lab.core.stock import Mesh
    from toolpath_lab.surfacing import (coerce_surface_parameters, mesh_to_stlsurf,
                                        parallel_toolpath, surface_parameters,
                                        waterline_toolpath)
    from toolpath_lab.surfacing.dropcutter import ParallelRequest
    from toolpath_lab.surfacing.mesh_input import make_cutter
    from toolpath_lab.surfacing.waterline import WaterlineRequest

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt


def _cylinder_model(radius: float = 20.0, height: float = 40.0) -> "BrepModel":
    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
                                     radius, height).Shape()
    return BrepModel(shape=shape, name="cyl").refresh()


@unittest.skipUnless(OCL_AVAILABLE and OCP_AVAILABLE, "需要 opencamlib 与 OCP")
class MeshInputTests(unittest.TestCase):
    def test_mesh_becomes_stlsurf(self) -> None:
        mesh = plate_with_pocket().mesh
        surf = mesh_to_stlsurf(mesh)
        self.assertEqual(surf.size(), mesh.triangle_count)

    def test_accepts_a_plain_mesh(self) -> None:
        positions = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], dtype=float)
        indices = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
        self.assertEqual(mesh_to_stlsurf(Mesh(positions, indices)).size(), 2)

    def test_negative_z_is_refused_with_an_actionable_message(self) -> None:
        """opencamlib 对 z<0 的区域一律返回"无接触"，必须在入口拦下。"""

        mesh = plate_with_pocket().mesh
        lowered = Mesh(mesh.positions - np.array([0.0, 0.0, 5.0]), mesh.indices)
        with self.assertRaises(ValueError) as context:
            mesh_to_stlsurf(lowered)
        self.assertIn("归一化", str(context.exception))

    def test_empty_mesh_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            mesh_to_stlsurf(Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)))

    def test_cutter_kinds(self) -> None:
        self.assertAlmostEqual(make_cutter("flat", 6.0, 40.0).getRadius(), 3.0)
        self.assertAlmostEqual(make_cutter("ball", 6.0, 40.0).getRadius(), 3.0)
        self.assertAlmostEqual(make_cutter("bull", 8.0, 40.0).getRadius(), 4.0)
        with self.assertRaises(ValueError):
            make_cutter("laser", 6.0, 40.0)


@unittest.skipUnless(OCL_AVAILABLE and OCP_AVAILABLE, "需要 opencamlib 与 OCP")
class ParallelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 100x80x40 的板，型腔底 z=25、开口朝上，顶面 z=40
        cls.mesh = plate_with_pocket().mesh

    def test_flat_areas_land_on_the_exact_heights(self) -> None:
        """落刀在水平面上的刀心高度必须**精确**等于该平面高度。"""

        result = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=20.0, sampling_mm=1.0, tool_diameter_mm=10.0, direction_deg=0.0))
        z = np.concatenate([item[:, 2] for item in result.passes])
        # 只可能落在两个水平面上
        levels = sorted({round(float(value), 3) for value in z})
        self.assertEqual(levels, [25.0, 40.0])

    def test_no_contact_points_are_dropped(self) -> None:
        """扫描线伸出工件之外，那些"没碰到"的点必须被丢掉，不能留在刀路里。

        否则刀心会掉到 0，刀路一头扎到工件底面。
        """

        result = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=20.0, sampling_mm=1.0, tool_diameter_mm=10.0))
        self.assertGreater(result.dropped_points, 0)
        z = np.concatenate([item[:, 2] for item in result.passes])
        self.assertGreaterEqual(float(z.min()), 25.0 - 1e-6)

    def test_samples_cover_the_scan_line(self) -> None:
        result = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=20.0, sampling_mm=2.0, tool_diameter_mm=10.0))
        self.assertGreater(result.pass_count, 3)
        for item in result.passes:
            self.assertGreaterEqual(item.shape[0], 2)
            self.assertTrue(np.all(np.isfinite(item)))

    def test_direction_rotates_the_scan_lines(self) -> None:
        along_x = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=10.0, sampling_mm=2.0, tool_diameter_mm=10.0, direction_deg=0.0))
        along_y = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=10.0, sampling_mm=2.0, tool_diameter_mm=10.0, direction_deg=90.0))
        x_span = np.ptp(along_x.passes[0][:, 0])
        y_span = np.ptp(along_y.passes[0][:, 1])
        self.assertGreater(x_span, y_span)          # 0°：沿 X 走
        self.assertLess(np.ptp(along_y.passes[0][:, 0]), np.ptp(along_y.passes[0][:, 1]))

    def test_one_way_costs_more_rapid_than_zigzag(self) -> None:
        kwargs = dict(stepover_mm=5.0, sampling_mm=2.0, tool_diameter_mm=10.0)
        zig = parallel_toolpath(self.mesh, ParallelRequest(cut_mode="zigzag", **kwargs))
        one = parallel_toolpath(self.mesh, ParallelRequest(cut_mode="one_way", **kwargs))
        self.assertLess(zig.toolpath.rapid_length_mm, one.toolpath.rapid_length_mm)
        self.assertAlmostEqual(zig.toolpath.cut_length_mm, one.toolpath.cut_length_mm,
                               delta=1.0)

    def test_stock_allowance_lifts_the_whole_path(self) -> None:
        base = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=20.0, sampling_mm=2.0, tool_diameter_mm=10.0))
        lifted = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=20.0, sampling_mm=2.0, tool_diameter_mm=10.0,
            stock_allowance_mm=0.5))
        self.assertAlmostEqual(float(np.concatenate([p[:, 2] for p in lifted.passes]).min()),
                               float(np.concatenate([p[:, 2] for p in base.passes]).min()) + 0.5,
                               places=6)

    def test_toolpath_has_rapid_and_cut_moves(self) -> None:
        result = parallel_toolpath(self.mesh, ParallelRequest(
            stepover_mm=20.0, sampling_mm=2.0, tool_diameter_mm=10.0))
        kinds = {m.kind for m in result.toolpath.moves}
        self.assertIn(MoveKind.CUT, kinds)
        self.assertIn(MoveKind.RAPID, kinds)
        self.assertGreater(result.toolpath.cut_length_mm, 0.0)

    def test_invalid_parameters_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            parallel_toolpath(self.mesh, ParallelRequest(stepover_mm=0.0))
        with self.assertRaises(ValueError):
            parallel_toolpath(self.mesh, ParallelRequest(sampling_mm=0.0))
        with self.assertRaises(ValueError):
            parallel_toolpath(self.mesh, ParallelRequest(), bounds=(0.0, 0.0, 0.0, 0.0))


@unittest.skipUnless(OCL_AVAILABLE and OCP_AVAILABLE, "需要 opencamlib 与 OCP")
class WaterlineTests(unittest.TestCase):
    def test_cylinder_contour_radius_is_exact(self) -> None:
        """圆柱等高铣：刀心半径必须**精确**等于 R + 刀半径。

        实测 opencamlib 的 AdaptiveWaterline 在同一个试件上给出 21.5（球刀）
        与夹杂飞点的 22.998（平底刀），所以等高走的是 OCP 切片 + pyclipper 偏置。
        """

        model = _cylinder_model(radius=20.0, height=40.0)
        result = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=5.0, tool_diameter_mm=6.0, z_top=35.0, z_bottom=5.0))
        # 35 → 5、层高 5：30/25/20/15/10 之外还必须补一层贴底（BUG-006），
        # 否则底部 [5, 10) 这一整段没有刀路。
        self.assertEqual(result.level_count, 6)
        self.assertAlmostEqual(result.levels[-1][0], 5.0, delta=1e-3)
        for z, loops in result.levels:
            self.assertEqual(len(loops), 1)
            radius = np.hypot(loops[0][:, 0], loops[0][:, 1])
            self.assertAlmostEqual(float(radius.min()), 23.0, delta=0.02)
            self.assertAlmostEqual(float(radius.max()), 23.0, delta=0.02)
            self.assertAlmostEqual(float(loops[0][:, 2].min()), z, places=6)

    def test_side_allowance_adds_to_the_radius(self) -> None:
        model = _cylinder_model()
        result = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=10.0, tool_diameter_mm=6.0, side_allowance_mm=1.0,
            z_top=35.0, z_bottom=5.0))
        radius = np.hypot(result.levels[0][1][0][:, 0], result.levels[0][1][0][:, 1])
        self.assertAlmostEqual(float(radius.mean()), 24.0, delta=0.02)

    def test_pocket_wall_contour_sits_inside_the_cavity(self) -> None:
        """型腔内壁的刀心必须落在空腔**里面**，离壁一个刀半径。"""

        from toolpath_lab.brep import load_brep
        from tests.fixtures import solid_bytes
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="tplab-wl-") as folder:
            path = Path(folder) / "plate.step"
            path.write_bytes(solid_bytes("plate"))
            model = load_brep(path, normalize=True)
        result = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=2.0, tool_diameter_mm=10.0, z_top=39.0, z_bottom=26.0))
        self.assertGreater(result.level_count, 3)
        # 型腔 60x40，刀半径 5 → 内壁刀心应当是 50x30 的环
        z, loops = result.levels[0]
        rectangles = [lp for lp in loops if np.ptp(lp[:, 0]) < 55.0]
        self.assertTrue(rectangles, "没有找到型腔内壁的刀心环")
        inner = rectangles[0]
        self.assertAlmostEqual(float(np.ptp(inner[:, 0])), 50.0, delta=0.2)
        self.assertAlmostEqual(float(np.ptp(inner[:, 1])), 30.0, delta=0.2)

    def test_levels_avoid_the_exact_top_and_bottom(self) -> None:
        model = _cylinder_model(height=40.0)
        result = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=5.0, tool_diameter_mm=6.0, z_top=40.0, z_bottom=0.0))
        for z, _ in result.levels:
            self.assertLess(z, 40.0)
            self.assertGreater(z, 0.0)

    def test_bottom_up_reverses_the_order(self) -> None:
        model = _cylinder_model()
        top_down = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=5.0, tool_diameter_mm=6.0, order="top_down"))
        bottom_up = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=5.0, tool_diameter_mm=6.0, order="bottom_up"))
        self.assertEqual([z for z, _ in top_down.levels],
                         list(reversed([z for z, _ in bottom_up.levels])))

    def test_each_contour_is_closed(self) -> None:
        model = _cylinder_model()
        result = waterline_toolpath(model, WaterlineRequest(step_down_mm=10.0,
                                                            tool_diameter_mm=6.0))
        for _, loops in result.levels:
            for loop in loops:
                self.assertTrue(np.allclose(loop[0], loop[-1]))

    def test_too_large_tool_erases_the_cavity_contour(self) -> None:
        """刀具比型腔还大时，型腔内壁的刀心环会消失（不是报错）。

        注意偏置方向是**外扩**：铣外轮廓时刀具再大也总能走一圈，
        所以"刀太大"只体现在**空腔**那侧 —— 孔被缩没了，内壁就没有刀路了。
        """

        from toolpath_lab.brep import load_brep
        from tests.fixtures import solid_bytes
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="tplab-wl-") as folder:
            path = Path(folder) / "plate.step"
            path.write_bytes(solid_bytes("plate"))
            model = load_brep(path, normalize=True)

        # 型腔 60x40：D10（半径 5）能进去，D80（半径 40）进不去
        small = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=2.0, tool_diameter_mm=10.0, z_top=39.0, z_bottom=26.0))
        big = waterline_toolpath(model, WaterlineRequest(
            step_down_mm=2.0, tool_diameter_mm=80.0, z_top=39.0, z_bottom=26.0))
        inner_small = [lp for lp in small.levels[0][1] if np.ptp(lp[:, 0]) < 55.0]
        inner_big = [lp for lp in big.levels[0][1] if np.ptp(lp[:, 0]) < 55.0]
        self.assertTrue(inner_small)
        self.assertFalse(inner_big)

    def test_invalid_step_down(self) -> None:
        with self.assertRaises(ValueError):
            waterline_toolpath(_cylinder_model(), WaterlineRequest(step_down_mm=0.0))

    def test_empty_height_range_is_reported(self) -> None:
        with self.assertRaises(ValueError):
            waterline_toolpath(_cylinder_model(), WaterlineRequest(z_top=10.0,
                                                                   z_bottom=10.0))


@unittest.skipUnless(OCL_AVAILABLE and OCP_AVAILABLE, "需要 opencamlib 与 OCP")
class SurfaceParameterTests(unittest.TestCase):
    def test_defaults_cover_both_strategies(self) -> None:
        values = coerce_surface_parameters({})
        for key in ("strategy", "cut_mode", "direction_deg", "stepover_mm",
                    "sampling_mm", "step_down_mm", "side_allowance_mm",
                    "tool_diameter_mm", "feed_mm_per_min", "safe_height_mm"):
            self.assertIn(key, values)
        self.assertEqual(values["strategy"], "parallel")

    def test_strategy_choices(self) -> None:
        spec = surface_parameters().spec("strategy")
        self.assertEqual([choice.value for choice in spec.choices], ["parallel", "waterline"])

    def test_out_of_range_is_rejected(self) -> None:
        from toolpath_lab.core.errors import ParameterError

        with self.assertRaises(ParameterError):
            coerce_surface_parameters({"stepover_mm": -1.0})


if __name__ == "__main__":
    unittest.main()
