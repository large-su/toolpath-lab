"""时间参数化与 NC 导出。"""

from __future__ import annotations

import unittest

from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan
from toolpath_lab.simulation import build_timeline


def _toolpath(mode: str = "one_way"):
    return run_plan(
        planner_id="raster",
        tool=Tool(ToolKind.FLAT, diameter_mm=10.0, length_mm=30.0),
        region=build_region("square", {"side_mm": 60.0}),
        parameters={"mode": mode, "stepover_mm": 15.0, "feed_mm_per_min": 600.0},
    ).toolpath


class TimelineTests(unittest.TestCase):
    def test_duration_matches_the_toolpath_estimate(self) -> None:
        toolpath = _toolpath()
        timeline = build_timeline(toolpath)
        self.assertAlmostEqual(timeline.duration_s / toolpath.estimated_time_s, 1.0, places=3)

    def test_times_start_at_zero_and_never_decrease(self) -> None:
        timeline = build_timeline(_toolpath())
        self.assertAlmostEqual(float(timeline.times_s[0]), 0.0)
        differences = timeline.times_s[1:] - timeline.times_s[:-1]
        self.assertTrue(bool((differences >= -1e-9).all()))

    def test_samples_are_capped(self) -> None:
        toolpath = _toolpath()
        timeline = build_timeline(toolpath, max_samples=50)
        self.assertLessEqual(timeline.sample_count, 50 + 2 * len(toolpath.moves))

    def test_positions_stay_on_the_machining_plane_or_above_it(self) -> None:
        timeline = build_timeline(_toolpath())
        self.assertGreaterEqual(float(timeline.positions[:, 2].min()), -1e-9)
        self.assertLessEqual(float(timeline.positions[:, 2].max()), 5.0 + 1e-9)

    def test_state_interpolates_between_samples(self) -> None:
        toolpath = _toolpath()
        timeline = build_timeline(toolpath)
        start = timeline.state_at(0.0)
        end = timeline.state_at(timeline.duration_s)
        self.assertAlmostEqual(start.progress, 0.0)
        self.assertAlmostEqual(end.progress, 1.0)
        first_point = toolpath.moves[0].points[0]
        self.assertTrue(bool((abs(start.position - first_point) < 1e-6).all()))
        middle = timeline.state_at(timeline.duration_s * 0.5)
        self.assertGreater(middle.progress, 0.4)
        self.assertIn(middle.kind, {"cut", "link", "rapid"})

    def test_kind_runs_cover_every_sample(self) -> None:
        timeline = build_timeline(_toolpath())
        payload = timeline.to_payload()
        self.assertEqual(len(payload["times"]), timeline.sample_count)
        self.assertEqual(len(payload["positions"]), timeline.sample_count)
        self.assertLessEqual(max(start for start, _ in payload["kind_runs"]), timeline.sample_count - 1)

    def test_rapid_moves_are_slower_to_play_than_cutting(self) -> None:
        timeline = build_timeline(_toolpath("one_way"))
        kinds = set(int(code) for _, code in timeline.to_payload()["kind_runs"])
        self.assertIn(2, kinds)


class GcodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toolpath = _toolpath()
        self.lines = [
            line for line in toolpath_to_gcode(
                self.toolpath, program_name="TEST", description="unit test"
            ).splitlines() if line
        ]

    def test_header_and_footer(self) -> None:
        self.assertEqual(self.lines[0], "(TEST)")
        self.assertIn("(unit test)", self.lines)
        self.assertIn("G21 (mm)", self.lines)
        self.assertIn("G90 (absolute)", self.lines)
        self.assertEqual(self.lines[-1], "M30")

    def test_every_point_becomes_a_motion(self) -> None:
        motions = [line for line in self.lines if line.startswith(("G0 ", "G1 "))]
        self.assertEqual(len(motions), self.toolpath.point_count)

    def test_rapid_and_feed_moves_are_distinguished(self) -> None:
        self.assertTrue(any(line.startswith("G0 ") for line in self.lines))
        self.assertTrue(any(line.startswith("G1 ") for line in self.lines))
        self.assertTrue(any(line.startswith("F") for line in self.lines))

    def test_feed_is_written_before_each_cutting_block(self) -> None:
        feeds = [line for line in self.lines if line.startswith("F")]
        self.assertLess(len(feeds), len(self.lines) // 2)
        self.assertGreaterEqual(len(feeds), 1)

    def test_notes_are_written_as_comments(self) -> None:
        self.assertTrue(any("往复" in line or "单向" in line for line in self.lines))


if __name__ == "__main__":
    unittest.main()
