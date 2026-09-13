"""毛坯切除仿真：Z-Map 材料去除、帧序列、导出网格与统计。

仿真最容易"看起来对但数字错"，因此这里断言的是**高度图的实际取值**与体积守恒：
刀走过的位置必须被削到刀底高度，没走到的地方必须保持毛坯顶面。
"""

from __future__ import annotations

import unittest

import numpy as np

from tests.fixtures import plate_with_pocket
from toolpath_lab.cam.service import CAMOperationRequest, execute_operation
from toolpath_lab.core.part import build_part
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.stock import build_stock
from toolpath_lab.core.tool import Tool
from toolpath_lab.simulation.cut_sim import (
    build_height_field,
    cut_move,
    simulate_toolpath,
)
from toolpath_lab.step.reader import read_step_bytes

BOX_PARAMETERS = {
    "tool_diameter_mm": 10.0,
    "feed_mm_per_min": 900.0,
    "stepover_mm": 5.0,
    "cut_depth_mm": 2.0,
    "stock_allowance_mm": 0.0,
    "finish_allowance_mm": 0.0,
    "safe_height_mm": 10.0,
}


def part_and_stock(offset_z: float = 2.0):
    part = build_part(read_step_bytes(plate_with_pocket().encode("latin-1"), source_name="plate"),
                      model_id="plate")
    stock = build_stock("rectangular", part,
                        {"offset_x_mm": 2.0, "offset_y_mm": 2.0, "offset_z_mm": offset_z})
    return part, stock


class HeightFieldTests(unittest.TestCase):
    def test_initial_height_is_the_stock_top(self) -> None:
        _, stock = part_and_stock()
        field = build_height_field(stock, cell_mm=0.5)
        self.assertAlmostEqual(float(field.height.min()), stock.bounds.z_max, places=6)
        self.assertAlmostEqual(float(field.height.max()), stock.bounds.z_max, places=6)
        self.assertAlmostEqual(field.volume_mm3(), stock.volume_mm3(), delta=stock.volume_mm3() * 0.02)

    def test_grid_is_sized_by_the_cell_size(self) -> None:
        _, stock = part_and_stock()
        field = build_height_field(stock, cell_mm=1.0)
        rows, cols = field.height.shape
        self.assertGreaterEqual(rows * 1.0, stock.bounds.size[0] - 1.0)
        self.assertLessEqual(rows * 1.0, stock.bounds.size[0] + 1.0)

    def test_cylindrical_stock_masks_outside_cells(self) -> None:
        part, _ = part_and_stock()
        stock = build_stock("cylindrical", part, {"offset_radial_mm": 2.0, "offset_z_mm": 2.0})
        field = build_height_field(stock, cell_mm=1.0)
        self.assertEqual(field.kind, "cylindrical")
        self.assertFalse(bool(field.active.all()))
        self.assertTrue(bool(field.active.any()))
        corners = [field.active[0, 0], field.active[0, -1], field.active[-1, 0], field.active[-1, -1]]
        self.assertFalse(any(bool(value) for value in corners))


class CutTests(unittest.TestCase):
    """单段切削：刀走哪里就削哪里。"""

    def setUp(self) -> None:
        _, self.stock = part_and_stock()
        self.field = build_height_field(self.stock, cell_mm=0.5)

    def _move(self, points, kind=MoveKind.CUT, feed=900.0):
        return Move(kind, np.asarray(points, dtype=np.float64), feed)

    def test_cutting_lowers_the_height_under_the_tool(self) -> None:
        removed = cut_move(self.field, self._move([[0.0, 0.0, 40.0], [20.0, 0.0, 40.0]]), 5.0)
        self.assertGreater(removed, 0.0)
        # 轨迹中点应被削到 40
        center = self.field.height[self.field.height.shape[0] // 2, self.field.height.shape[1] // 2]
        self.assertAlmostEqual(float(center), 40.0, delta=0.6)
        # 远离轨迹的角落保持毛坯顶面
        self.assertAlmostEqual(float(self.field.height[0, 0]), self.stock.bounds.z_max, places=3)

    def test_rapid_moves_do_not_cut(self) -> None:
        removed = cut_move(self.field, self._move([[0.0, 0.0, 40.0], [20.0, 20.0, 40.0]],
                                                  kind=MoveKind.RAPID), 5.0)
        self.assertEqual(removed, 0.0)
        self.assertAlmostEqual(float(self.field.height.min()), self.stock.bounds.z_max, places=6)

    def test_tool_radius_controls_the_groove_width(self) -> None:
        field = build_height_field(self.stock, cell_mm=0.5)
        # 沿 Y 方向切一刀，然后在它中点的 X 列上量"被切掉的格数"= 槽宽
        cut_move(field, self._move([[-40.0, 0.0, 40.0], [40.0, 0.0, 40.0]]), 2.5)
        xs = field.x0 + (np.arange(field.height.shape[0]) + 0.5) * field.cell_mm
        column = int(np.argmin(np.abs(xs)))
        cut_rows = np.nonzero(field.height[column] < self.stock.bounds.z_max - 0.5)[0]
        width = (cut_rows.max() - cut_rows.min() + 1) * field.cell_mm
        self.assertGreater(width, 4.0)
        self.assertLess(width, 6.5)

    def test_deeper_cut_removes_more(self) -> None:
        shallow = build_height_field(self.stock, cell_mm=0.5)
        deep = build_height_field(self.stock, cell_mm=0.5)
        first = cut_move(shallow, self._move([[-20.0, 0.0, 41.0], [20.0, 0.0, 41.0]]), 5.0)
        second = cut_move(deep, self._move([[-20.0, 0.0, 39.0], [20.0, 0.0, 39.0]]), 5.0)
        self.assertGreater(second, first)

    def test_volume_is_conserved(self) -> None:
        before = self.field.volume_mm3()
        removed = cut_move(self.field, self._move([[-20.0, 0.0, 40.0], [20.0, 0.0, 40.0]]), 5.0)
        after = self.field.volume_mm3()
        self.assertAlmostEqual(before - after, removed, delta=abs(removed) * 0.02 + 1.0)

    def test_mesh_export_has_triangles(self) -> None:
        cut_move(self.field, self._move([[-20.0, 0.0, 39.0], [20.0, 0.0, 39.0]]), 5.0)
        mesh = self.field.surface_mesh()
        self.assertGreater(mesh.triangle_count, 10)
        self.assertEqual(mesh.positions.shape[1], 3)
        # 网格的最高点不应超过毛坯顶面
        self.assertLessEqual(float(mesh.positions[:, 2].max()), self.stock.bounds.z_max + 1e-6)


class SimulationRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.part, cls.stock = part_and_stock()
        cls.floor = [f for f in cls.part.features
                     if f["horizontal"] and abs(f["plane"][3] - 25.0) < 1e-6][0]
        request = CAMOperationRequest.from_payload({
            "kind": "pocket_mill",
            "faces": [cls.floor["face_id"]],
            "cell_mm": 0.5,
            "stock": cls.stock,
            "parameters": {**BOX_PARAMETERS, "cut_mode": "contour", "finish_pass": False},
        }, cls.part)
        cls.result = execute_operation(request)

    def test_simulation_removes_material_and_frames_grow(self) -> None:
        simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                      cell_mm=0.6, max_frames=40)
        self.assertGreater(len(simulation.frames), 2)
        removed = [frame.removed_mm3 for frame in simulation.frames]
        self.assertEqual(removed, sorted(removed))
        self.assertGreater(removed[-1], 1000.0)

    def test_final_height_reaches_the_pocket_floor(self) -> None:
        simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                      cell_mm=0.6, max_frames=40)
        height = simulation.final.height
        grid = simulation.grid
        xs = grid["x0"] + (np.arange(grid["rows"]) + 0.5) * grid["cell_mm"]
        ys = grid["y0"] + (np.arange(grid["cols"]) + 0.5) * grid["cell_mm"]
        mask_x = np.abs(xs) < 10.0
        mask_y = np.abs(ys) < 5.0
        block = height[np.ix_(mask_x, mask_y)]
        self.assertLess(float(block.max()), 26.0)

    def test_untouched_material_stays_at_the_stock_top(self) -> None:
        simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                      cell_mm=0.6, max_frames=40)
        self.assertAlmostEqual(float(simulation.final.height[2, 2]),
                               self.stock.bounds.z_max, places=3)

    def test_summary_reports_volume_accounting(self) -> None:
        simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                      cell_mm=0.6, max_frames=40)
        summary = simulation.summary()
        self.assertAlmostEqual(
            summary["initial_volume_mm3"] - summary["remaining_volume_mm3"],
            summary["removed_volume_mm3"], delta=2.0,
        )
        self.assertGreaterEqual(summary["removed_ratio"], 0.0)
        self.assertLessEqual(summary["removed_ratio"], 1.0)
        self.assertEqual(summary["frame_count"], len(simulation.frames))

    def test_frames_carry_the_height_payload(self) -> None:
        simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                      cell_mm=0.8, max_frames=12)
        payload = simulation.frames[-1].to_payload()
        self.assertIn("height", payload)
        self.assertEqual(len(payload["height"]),
                         simulation.grid["rows"] * simulation.grid["cols"])
        self.assertIn("position", payload)

    def test_sample_spacing_controls_the_frame_count(self) -> None:
        coarse = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                   cell_mm=0.6, max_frames=8)
        fine = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                 cell_mm=0.6, max_frames=200)
        self.assertGreaterEqual(len(fine.frames), len(coarse.frames))

    def test_empty_toolpath_still_produces_one_frame(self) -> None:
        toolpath = Toolpath(
            moves=(Move(MoveKind.CUT, np.array([[0.0, 0.0, 41.0], [0.0, 0.0, 41.0]]), 900.0),),
            planner="test", planner_label="测试",
        )
        simulation = simulate_toolpath(toolpath, self.stock, tool_radius=5.0, cell_mm=1.0)
        self.assertGreaterEqual(len(simulation.frames), 1)


if __name__ == "__main__":
    unittest.main()
