"""毛坯切除仿真：Z-Map 材料去除、帧序列、导出网格与统计。

仿真最容易"看起来对但数字错"，因此这里断言的是**高度图的实际取值**与体积守恒：
刀走过的位置必须被削到刀底高度，没走到的地方必须保持毛坯顶面。
"""

from __future__ import annotations

import unittest

import numpy as np

from tests.fixtures import plate_with_pocket
from toolpath_lab.cam.service import CAMOperationRequest, execute_operation
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.stock import build_stock
from toolpath_lab.core.tool import Tool
from toolpath_lab.simulation.cut_sim import (
    _slice_polyline,
    build_height_field,
    cut_move,
    simulate_toolpath,
)

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
    part = plate_with_pocket(name="plate")
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

    def test_vertical_plunge_cuts_to_the_bottom_in_one_call(self) -> None:
        """竖直下刀（XY 不动、Z 下降）一次调用就切到最低点。

        斜插/竖直段上"取最近点的 Z"是不够的：最近点是起点 Z，一次调用只会削掉
        顶皮。新公式取"格点在刀盘内的参数区间两端 Z 的最小值"，整段切才成立
        （帧边界拆段整段扫掠的前提）。
        """

        removed = cut_move(self.field, self._move([[0.0, 0.0, 41.0], [0.0, 0.0, 30.0]]), 5.0)
        self.assertGreater(removed, 0.0)
        center = self.field.height[self.field.height.shape[0] // 2,
                                    self.field.height.shape[1] // 2]
        # 30 是目标高度，0.02 是 _cut_segment 的安全容差
        self.assertAlmostEqual(float(center), 30.0 - 0.02, delta=0.1)

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


    def test_splitting_a_segment_does_not_change_the_cut(self) -> None:
        """按任意方式拆段切削，结果与整段切一致（帧边界拆段的正确性根基）。

        切削按帧边界拆成整段一次扫掠，靠的是"格点取到线段的最近点插值 Z"与拆分
        无关：拆开后每格的最近点仍落在某一小段上，Z 相同、取 min 也相同。
        这里用**斜插**（Z 沿线性下降）验证——它正是投影参数会随拆分变化的情形。
        生产代码用的 `_slice_polyline` 也一并被覆盖。
        """

        whole = build_height_field(self.stock, cell_mm=0.5)
        split = build_height_field(self.stock, cell_mm=0.5)
        points = np.array([[-30.0, 0.0, 40.0], [30.0, 0.0, 25.0]])
        cut_move(whole, self._move(points), 5.0)
        cumulative = np.array([float(np.linalg.norm(points[1] - points[0]))])
        # 拆成 17 段不等间距（端点用插值），对应帧边界不落在折线顶点上的情况
        for from_t, to_t in zip(np.linspace(0.0, 1.0, 18)[:-1],
                                np.linspace(0.0, 1.0, 18)[1:]):
            sub = _slice_polyline(points, cumulative,
                                  from_t * cumulative[0], to_t * cumulative[0])
            cut_move(split, self._move(sub), 5.0)
        np.testing.assert_allclose(split.height, whole.height, atol=1e-9)


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

    def test_frames_carry_the_mileage_anchor(self) -> None:
        """帧要带刀路里程：前端播放靠它把"帧 ↔ 刀路子段"对齐做连续扫掠插值。

        首帧在 0、末帧必须落在刀路终点（否则拖到底显示不出最终状态）、
        中间严格单调递增（拖动/回放定位的二分查找依赖它）。
        """

        simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                      cell_mm=0.6, max_frames=40)
        miles = [frame.travelled_mm for frame in simulation.frames]
        self.assertEqual(miles[0], 0.0)
        self.assertAlmostEqual(miles[-1], self.result.toolpath.total_length_mm, delta=1e-6)
        self.assertEqual(miles, sorted(miles))
        self.assertTrue(all(b - a > 0.0 for a, b in zip(miles, miles[1:])),
                        "相邻帧的里程必须严格递增")
        self.assertIn("travelled_mm", simulation.frames[-1].to_payload(with_height=False))

    def test_max_frames_is_a_hard_cap(self) -> None:
        """``max_frames`` 是**上限**：含终点帧在内不能超。

        采样网格原来按 ``总长 / max_frames`` 铺，末尾再补一帧刀路终点，
        于是导出恒为 ``max_frames + 1`` —— "上限"语义被突破，payload 也白涨一份。
        """

        for cap in (2, 3, 8, 40):
            simulation = simulate_toolpath(self.result.toolpath, self.stock, tool_radius=5.0,
                                          cell_mm=0.6, max_frames=cap)
            self.assertLessEqual(len(simulation.frames), cap,
                                 f"max_frames={cap} 被突破：{len(simulation.frames)}")
            self.assertAlmostEqual(simulation.frames[-1].travelled_mm,
                                   self.result.toolpath.total_length_mm, delta=1e-6)

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

    def test_multi_point_cut_produces_frames_along_its_whole_length(self) -> None:
        """回归：一条切削里塞很多点时，帧要铺满整条刀路，而不是全挤在起点。

        里程原来按"当前折线段的长度 / 整条运动的步数"累加，等于每个子步只前进
        ``segment/steps``；一条 100mm、201 个点的切削段因此只走了 0.5mm 就"结束"，
        整个仿真只出得 1 帧 —— 用户看到的就是"曲面加工动画完全不动"。
        """

        points = np.column_stack([
            np.linspace(-50.0, 50.0, 201), np.zeros(201), np.full(201, 41.0),
        ])
        toolpath = Toolpath(
            moves=(Move(MoveKind.CUT, points, 800.0),), planner="test", planner_label="测试",
        )
        simulation = simulate_toolpath(toolpath, self.stock, tool_radius=3.0,
                                       cell_mm=1.0, max_frames=10)
        self.assertGreaterEqual(len(simulation.frames), 8)
        # 最后一帧必须接近刀路终点，而不是停在起点附近
        self.assertGreater(float(simulation.frames[-1].position[0]), 30.0)
        # 帧的 X 位置单调前进
        xs = [float(frame.position[0]) for frame in simulation.frames]
        self.assertEqual(xs, sorted(xs))
        # 时间按"进给 / 弧长"累积：10 帧走完约 90mm，不可能只有零点几秒
        self.assertGreater(simulation.frames[-1].time_s, 5.0)

    def test_frame_time_matches_feed_over_arc_length(self) -> None:
        """多段运动的累计时间要与 Toolpath 自己算的工时一致。"""

        points = np.column_stack([
            np.linspace(-50.0, 50.0, 201), np.zeros(201), np.full(201, 41.0),
        ])
        toolpath = Toolpath(
            moves=(Move(MoveKind.CUT, points, 800.0),), planner="test", planner_label="测试",
        )
        simulation = simulate_toolpath(toolpath, self.stock, tool_radius=3.0,
                                       cell_mm=1.0, max_frames=1000)
        expected = 100.0 / 800.0 * 60.0
        self.assertLessEqual(simulation.frames[-1].time_s, expected + 1e-6)
        self.assertGreater(simulation.frames[-1].time_s, expected * 0.9)


if __name__ == "__main__":
    unittest.main()
