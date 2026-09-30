"""材料切除仿真：高度场、刀底几何、帧序列与载荷解码。"""

from __future__ import annotations

import json
import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.planning import run_plan
from toolpath_lab.simulation import (
    MAX_GRID_CELLS,
    StockSettings,
    build_height_field,
    resample_points,
    simulate_material_removal,
)


def flat_tool(diameter_mm: float = 10.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter_mm, length_mm=30.0)


def straight_toolpath(
    y_mm: float = 0.0, z_mm: float = 0.0, feed: float = 600.0, half_length_mm: float = 20.0
) -> Toolpath:
    """一条沿 X 的直线切削，默认从 -20 走到 20。"""

    points = np.array(
        [[-half_length_mm, y_mm, z_mm], [half_length_mm, y_mm, z_mm]], dtype=np.float64
    )
    return Toolpath(moves=(Move(MoveKind.CUT, points, feed, pass_index=0),), planner="test")


def decode_frames(payload: dict) -> np.ndarray:
    """按载荷声明的规则还原高度场（与前端 viewport.js 的实现保持一致）。"""

    rows = payload["rows"]
    columns = payload["columns"]
    step = payload["encoding"]["step_mm"]
    offset = payload["encoding"]["offset_mm"]
    state = np.zeros((rows, columns), dtype=np.int64)
    state += int(round((payload["stock_top_mm"] - offset) / step))
    frames = []
    for runs in payload["frames"]:
        for row, start, packed in runs:
            values = np.frombuffer(packed.encode("latin-1"), dtype="<u2")
            assert start + values.size <= columns, "游程超出行宽"
            state[row, start:start + values.size] = values
        frames.append(state.astype(np.float64) * step + offset)
    return np.array(frames)


class HeightFieldTests(unittest.TestCase):
    def test_grid_covers_the_swept_area_with_the_margin(self) -> None:
        field = build_height_field(
            straight_toolpath(), flat_tool(10.0), StockSettings(margin_mm=2.0, resolution_mm=1.0)
        )
        # 刀心到 ±20，刀具半径 5，再加 2 mm 余量。
        self.assertAlmostEqual(float(field.x_mm[0]), -27.0)
        self.assertAlmostEqual(float(field.x_mm[-1]), 27.0)
        self.assertAlmostEqual(float(field.y_mm[0]), -7.0)
        self.assertAlmostEqual(float(field.y_mm[-1]), 7.0)

    def test_uncut_field_is_flat_at_the_stock_top(self) -> None:
        field = build_height_field(straight_toolpath(), flat_tool(), StockSettings(top_mm=3.0))
        self.assertTrue(bool((field.removed_mm == 0.0).all()))
        self.assertTrue(bool((field.top_mm == 3.0).all()))
        self.assertAlmostEqual(field.max_cut_depth_mm, 0.0)
        self.assertAlmostEqual(field.machined_ratio, 0.0)

    def test_grid_resolution_is_capped(self) -> None:
        # 请求 0.01 mm 的精度，网格数必须被夹到上限之内。
        field = build_height_field(
            straight_toolpath(), flat_tool(), StockSettings(resolution_mm=0.01)
        )
        self.assertLessEqual(max(field.columns, field.rows), MAX_GRID_CELLS + 1)

    def test_zero_top_leaves_a_visible_lip(self) -> None:
        # 上表面余量填 0 时不能退化成"什么都没切"，否则界面上看不到效果。
        field = build_height_field(straight_toolpath(), flat_tool(), StockSettings(top_mm=0.0))
        self.assertGreater(field.stock_top_mm, 0.0)


class StockSettingsTests(unittest.TestCase):
    def test_defaults_are_complete(self) -> None:
        settings = StockSettings.from_parameters(None)
        self.assertGreater(settings.depth_mm, 0.0)
        self.assertGreater(settings.frame_budget, 1)

    def test_rejects_non_positive_thickness(self) -> None:
        with self.assertRaises(ParameterError):
            StockSettings(depth_mm=0.0)

    def test_rejects_out_of_range_frame_budget(self) -> None:
        with self.assertRaises(ParameterError):
            StockSettings(frame_budget=1)

    def test_partial_override_keeps_other_defaults(self) -> None:
        settings = StockSettings.from_parameters({"resolution_mm": 2.5})
        self.assertAlmostEqual(settings.resolution_mm, 2.5)
        self.assertAlmostEqual(settings.depth_mm, StockSettings().depth_mm)


class CarveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = flat_tool(10.0)
        self.settings = StockSettings(top_mm=1.0, resolution_mm=0.5, margin_mm=1.0)

    def _result(self, toolpath: Toolpath):
        return simulate_material_removal(toolpath, self.tool, self.settings)

    def test_groove_is_as_wide_as_the_tool(self) -> None:
        result = self._result(straight_toolpath())
        # 取中间那一列（x 远离两端），看被切到的行数对应 10 mm 的刀径。
        column = result.columns // 2
        cut_rows = np.flatnonzero(result.final_removed_mm[:, column] > 1e-6)
        span = float(result.y_mm[cut_rows[-1]] - result.y_mm[cut_rows[0]])
        self.assertGreaterEqual(span, 9.0)
        self.assertLessEqual(span, 10.0 + 0.5 + 1e-9)

    def test_flat_tool_cuts_the_whole_disc_to_the_same_depth(self) -> None:
        result = self._result(straight_toolpath())
        column = result.columns // 2
        cut = result.final_removed_mm[:, column]
        cut = cut[cut > 1e-6]
        # 平底刀：半径以内的材料被均匀切到 z = 0，也就是切掉整个上表面余量。
        self.assertAlmostEqual(float(cut.max()), self.settings.top_mm, places=6)
        self.assertAlmostEqual(float(cut.min()), self.settings.top_mm, places=6)

    def test_material_outside_the_sweep_is_untouched(self) -> None:
        result = self._result(straight_toolpath())
        column = result.columns // 2
        removed = result.final_removed_mm[:, column]
        untouched = removed < 1e-6
        self.assertTrue(bool(untouched.any()))
        # 未切到的单元必须停在毛坯顶面。
        self.assertTrue(bool((result.stock_top_mm - removed[untouched] == result.stock_top_mm).all()))

    def test_cutting_deeper_removes_more(self) -> None:
        shallow = self._result(straight_toolpath(z_mm=0.0))
        deep = self._result(straight_toolpath(z_mm=-3.0))
        self.assertGreater(deep.max_cut_depth_mm, shallow.max_cut_depth_mm)
        self.assertGreater(deep.removed_volume_mm3, shallow.removed_volume_mm3)

    def test_material_is_never_removed_below_the_floor(self) -> None:
        settings = StockSettings(top_mm=1.0, depth_mm=2.0, resolution_mm=0.5)
        toolpath = straight_toolpath(z_mm=-50.0)
        result = simulate_material_removal(toolpath, self.tool, settings)
        self.assertAlmostEqual(result.floor_mm, -2.0)
        self.assertLessEqual(result.max_cut_depth_mm, 3.0 + 1e-9)  # 上表面余量 + 厚度
        self.assertGreaterEqual(float(result.frame_at(0.0).min()), 0.0)
        top = result.stock_top_mm - result.final_removed_mm
        self.assertGreaterEqual(float(top.min()), result.floor_mm - 1e-9)

    def test_removal_is_monotone_in_time(self) -> None:
        result = self._result(straight_toolpath())
        differences = result.frames_mm[1:] - result.frames_mm[:-1]
        self.assertTrue(bool((differences >= -1e-12).all()))
        self.assertTrue(bool((result.frames_mm >= -1e-12).all()))

    def test_cutting_and_linking_are_sampled_but_rapids_are_not_cutting(self) -> None:
        region = build_region("square", {"side_mm": 40.0})
        outcome = run_plan(
            planner_id="raster",
            tool=self.tool,
            region=region,
            parameters={"mode": "one_way", "stepover_mm": 10.0, "feed_mm_per_min": 600.0},
        )
        result = simulate_material_removal(outcome.toolpath, self.tool, self.settings)
        # 单向模式的快移都在安全高度，不会切到工件；刀路确实动了料。
        self.assertGreater(result.cut_cells, 0)
        self.assertGreater(result.machined_ratio, 0.2)

    def test_measurements_are_consistent(self) -> None:
        result = self._result(straight_toolpath())
        cell_area = float(result.x_mm[1] - result.x_mm[0]) * float(result.y_mm[1] - result.y_mm[0])
        expected = float(result.final_removed_mm.sum()) * cell_area
        self.assertAlmostEqual(result.removed_volume_mm3, expected, places=6)
        self.assertAlmostEqual(result.duration_s, 40.0 / 600.0 * 60.0, places=6)

    def test_simulation_never_touches_the_toolpath(self) -> None:
        toolpath = straight_toolpath()
        before = np.vstack([move.points for move in toolpath.moves]).copy()
        self._result(toolpath)
        after = np.vstack([move.points for move in toolpath.moves])
        self.assertTrue(bool((before == after).all()))


class BallToolTests(unittest.TestCase):
    def test_ball_tool_leaves_a_round_bottom(self) -> None:
        tool = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=30.0)
        self.assertAlmostEqual(tool.footprint_radius_mm, 0.0)
        # 球头刀沿 Y 的切削宽度就是刀径（10 mm），走刀长度要明显大于它，
        # 才能在刀路中段看到一个不受端头影响的完整圆弧截面。
        # 注意别把刀路拉太长：网格数有上限，拉长会把分辨率夹粗，弧面采样误差随之变大。
        result = simulate_material_removal(
            straight_toolpath(z_mm=-1.0, half_length_mm=40.0),
            tool,
            StockSettings(top_mm=1.0, resolution_mm=0.25),
        )
        column = result.columns // 2
        removed = result.final_removed_mm[:, column]
        cut = removed[removed > 1e-6]
        self.assertGreater(cut.size, 3)
        # 球头刀中心最深、两侧抬升，所以同一列的切深是连续变化的圆弧，而不是一个常数。
        self.assertGreater(float(np.unique(np.round(cut, 4)).size), 3)
        self.assertLess(float(cut.min()), float(cut.max()))
        # 刀心正下方切到"毛坯顶面 - 刀尖 Z" = 1 - (-1) = 2 mm，两侧按球面抬升。
        # 网格行不一定正好落在 y = 0，所以给一个网格量级的容差。
        self.assertAlmostEqual(float(cut.max()), 2.0, places=2)
        # 各点落在以刀尖为圆心的球面上：切深差的圆弧关系 d = sqrt(R² - (R - Δ)²)。
        radius = tool.radius_mm
        depths = cut.max() - cut
        distances = np.sqrt(np.maximum(radius**2 - (radius - depths) ** 2, 0.0))
        rows = np.flatnonzero(removed > 1e-6)
        # 网格列不一定正好落在 x = 0，实际距离按列坐标修正；粗网格下弧线采样误差同量级。
        offsets = np.hypot(result.x_mm[column], result.y_mm[rows])
        self.assertLess(float(np.abs(distances - offsets).max()), 0.15)


class FrameTests(unittest.TestCase):
    def setUp(self) -> None:
        region = build_region("square", {"side_mm": 60.0})
        self.toolpath = run_plan(
            planner_id="raster",
            tool=flat_tool(10.0),
            region=region,
            parameters={"mode": "zigzag", "stepover_mm": 15.0, "feed_mm_per_min": 600.0},
        ).toolpath
        self.settings = StockSettings(resolution_mm=1.0, frame_budget=12)

    def test_frame_times_are_sorted_and_within_the_duration(self) -> None:
        result = simulate_material_removal(self.toolpath, flat_tool(10.0), self.settings)
        self.assertAlmostEqual(float(result.times_s[0]), result.duration_s / 12, places=6)
        self.assertTrue(bool((np.diff(result.times_s) > -1e-9).all()))
        self.assertLessEqual(float(result.times_s[-1]), result.duration_s + 1e-9)
        self.assertAlmostEqual(float(result.times_s[-1]), result.duration_s, places=6)

    def test_frames_progress_instead_of_jumping_per_pass(self) -> None:
        result = simulate_material_removal(self.toolpath, flat_tool(10.0), self.settings)
        counts = [int((frame > 1e-6).sum()) for frame in result.frames_mm]
        self.assertGreater(len({count for count in counts}), 4)  # 不是"一刀一跳"
        self.assertTrue(all(b >= a for a, b in zip(counts, counts[1:])))

    def test_frame_at_interpolates_between_frames(self) -> None:
        result = simulate_material_removal(self.toolpath, flat_tool(10.0), self.settings)
        start = result.frame_at(0.0)
        middle = result.frame_at(result.duration_s * 0.5)
        end = result.frame_at(result.duration_s)
        self.assertAlmostEqual(float(start.max()), 0.0)
        self.assertAlmostEqual(float(np.abs(end - result.final_removed_mm).max()), 0.0)
        self.assertGreaterEqual(float(middle.sum()), float(start.sum()))
        self.assertLessEqual(float(middle.sum()), float(end.sum()))

    def test_frame_budget_is_respected(self) -> None:
        for budget in (4, 8, 24):
            with self.subTest(budget=budget):
                result = simulate_material_removal(
                    self.toolpath, flat_tool(10.0), StockSettings(frame_budget=budget)
                )
                self.assertLessEqual(result.frame_count, budget)

    def test_short_toolpath_still_produces_frames(self) -> None:
        toolpath = straight_toolpath()
        result = simulate_material_removal(toolpath, flat_tool(10.0), self.settings)
        self.assertGreaterEqual(result.frame_count, 1)
        self.assertTrue(bool(np.isfinite(result.times_s).all()))


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        region = build_region("square", {"side_mm": 60.0})
        self.result = simulate_material_removal(
            run_plan(
                planner_id="raster",
                tool=flat_tool(10.0),
                region=region,
                parameters={"mode": "zigzag", "stepover_mm": 15.0, "feed_mm_per_min": 600.0},
            ).toolpath,
            flat_tool(10.0),
            StockSettings(resolution_mm=1.0, frame_budget=8),
        )
        self.payload = self.result.to_payload()

    def test_payload_shape(self) -> None:
        payload = self.payload
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["encoding"]["type"], "uint16")
        self.assertEqual(payload["encoding"]["endian"], "little")
        self.assertEqual(payload["encoding"]["offset_mm"], 0.0)
        self.assertEqual(len(payload["times"]), len(payload["frames"]))
        self.assertEqual(len(payload["times"]), payload["statistics"]["frame_count"])
        self.assertEqual(payload["statistics"]["keyframe_count"], self.result.frame_count)
        self.assertEqual(len(payload["times"]), payload["statistics"]["keyframe_count"] + 1)
        self.assertEqual(payload["columns"], self.result.columns)
        self.assertEqual(payload["rows"], self.result.rows)
        self.assertEqual(len(payload["floor_vertices"]), 5)

    def test_first_frame_is_the_untouched_stock(self) -> None:
        payload = self.payload
        self.assertEqual(payload["times"][0], 0.0)
        self.assertEqual(payload["frames"][0], [])
        decoded = decode_frames(payload)
        self.assertAlmostEqual(float(decoded[0].min()), payload["stock_top_mm"])
        self.assertAlmostEqual(float(decoded[0].max()), payload["stock_top_mm"])

    def test_frames_decode_to_the_simulation(self) -> None:
        decoded = decode_frames(self.payload)
        expected = self.payload["stock_top_mm"] - np.vstack(
            [np.zeros_like(self.result.frames_mm[:1]), self.result.frames_mm]
        )
        # 量化步长 0.01 mm，解码误差不应超过半个步长。
        self.assertLessEqual(float(np.abs(decoded - expected).max()), 0.0051)

    def test_last_frame_reaches_the_floor(self) -> None:
        decoded = decode_frames(self.payload)
        self.assertGreaterEqual(float(decoded[-1].min()), self.payload["floor_mm"] - 1e-6)
        self.assertLessEqual(float(decoded[-1].max()), self.payload["stock_top_mm"] + 1e-6)

    def test_runs_stay_inside_their_row(self) -> None:
        columns = self.payload["columns"]
        for runs in self.payload["frames"]:
            for row, start, packed in runs:
                values = np.frombuffer(packed.encode("latin-1"), dtype="<u2")
                self.assertLess(row, self.payload["rows"])
                self.assertLessEqual(start + values.size, columns)

    def test_payload_is_json_serialisable_and_compact(self) -> None:
        text = json.dumps(self.payload, ensure_ascii=False)
        size = len(text.encode("utf-8"))
        # 增量编码的目的就是让"每帧一张全量网格"不会把响应撑爆。
        full_grid_bytes = self.result.columns * self.result.rows * 2 * len(self.payload["frames"])
        self.assertLess(size, full_grid_bytes)

    def test_statistics_agree_with_the_frames(self) -> None:
        statistics = self.payload["statistics"]
        self.assertAlmostEqual(statistics["duration_s"], self.result.duration_s, places=4)
        self.assertAlmostEqual(
            statistics["max_cut_depth_mm"], self.result.max_cut_depth_mm, places=4
        )
        self.assertEqual(statistics["cut_cells"], self.result.cut_cells)
        self.assertAlmostEqual(
            statistics["removed_volume_mm3"], self.result.removed_volume_mm3, places=2
        )


class SamplingTests(unittest.TestCase):
    def test_resample_keeps_the_endpoints_and_the_step(self) -> None:
        points = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]], dtype=np.float64)
        sampled = resample_points(points, 1.0)
        self.assertAlmostEqual(float(sampled[0, 0]), 0.0)
        self.assertAlmostEqual(float(sampled[-1, 0]), 10.0)
        steps = np.linalg.norm(np.diff(sampled, axis=0), axis=1)
        self.assertLessEqual(float(steps.max()), 1.0 + 1e-9)

    def test_resample_handles_a_zero_length_move(self) -> None:
        points = np.array([[3.0, 4.0, 0.0], [3.0, 4.0, 0.0]], dtype=np.float64)
        sampled = resample_points(points, 1.0)
        self.assertEqual(sampled.shape[0], 1)

    def test_resample_follows_the_polyline(self) -> None:
        points = np.array(
            [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0]], dtype=np.float64
        )
        sampled = resample_points(points, 2.0)
        self.assertAlmostEqual(float(sampled[-1, 1]), 10.0)
        # 总长 20、步长 2，点数应当接近 11，而不是沿某一条边重采样。
        self.assertGreaterEqual(sampled.shape[0], 10)
        self.assertLessEqual(sampled.shape[0], 13)
        # 每个采样点都必须落在这条折线上：要么 y = 0，要么 x = 10。
        on_polyline = (np.abs(sampled[:, 1]) < 1e-9) | (np.abs(sampled[:, 0] - 10.0) < 1e-9)
        self.assertTrue(bool(on_polyline.all()))


if __name__ == "__main__":
    unittest.main()
