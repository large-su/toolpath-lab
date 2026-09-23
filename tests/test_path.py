"""运动段与刀路模型：播放、导出、统计共用的一份数据。"""

from __future__ import annotations

import unittest

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move


def _line(start, end, kind=MoveKind.CUT, feed=600.0):
    return Move(kind, np.array([start, end], dtype=np.float64), feed)


class MoveTests(unittest.TestCase):
    def test_length_and_duration(self) -> None:
        move = _line([0, 0, 0], [30, 40, 0])
        self.assertAlmostEqual(move.length_mm, 50.0)
        self.assertAlmostEqual(move.duration_s, 5.0)

    def test_requires_two_points(self) -> None:
        with self.assertRaises(ParameterError):
            Move(MoveKind.CUT, np.array([[0.0, 0.0, 0.0]]), 600.0)

    def test_requires_a_positive_feed(self) -> None:
        with self.assertRaises(ParameterError):
            _line([0, 0, 0], [1, 0, 0], feed=0.0)

    def test_coordinates_must_be_finite(self) -> None:
        with self.assertRaises(ParameterError):
            _line([0, 0, 0], [float("nan"), 0, 0])

    def test_points_are_immutable(self) -> None:
        move = _line([0, 0, 0], [1, 0, 0])
        with self.assertRaises(ValueError):
            move.points[0][0] = 5.0

    def test_cutting_flag(self) -> None:
        self.assertTrue(_line([0, 0, 0], [1, 0, 0]).is_cutting)
        self.assertFalse(_line([0, 0, 0], [1, 0, 0], MoveKind.RAPID).is_cutting)

    def test_payload_rounds_and_labels(self) -> None:
        payload = _line([0, 0, 0], [1.23456789, 0, 0]).to_payload()
        self.assertEqual(payload["kind"], "cut")
        self.assertEqual(payload["kind_label"], "切削进给")
        self.assertEqual(payload["points"][1][0], 1.2346)


class ToolpathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toolpath = Toolpath(
            moves=(
                Move(MoveKind.CUT, np.array([[0.0, 0, 0], [10.0, 0, 0]]), 600.0, pass_index=0),
                Move(MoveKind.LINK, np.array([[10.0, 0, 0], [10.0, 5, 0]]), 600.0),
                retract_move(
                    np.array([10.0, 5.0, 0.0]), np.array([0.0, 5.0, 0.0]), 5.0, 5000.0
                ),
            ),
            planner="raster",
            planner_label="栅格刀路",
        )

    def test_requires_at_least_one_move(self) -> None:
        with self.assertRaises(ParameterError):
            Toolpath(moves=())

    def test_lengths_are_split_by_kind(self) -> None:
        self.assertAlmostEqual(self.toolpath.cut_length_mm, 10.0)
        # 抬刀 5 + 横移 10 + 下刀 5
        self.assertAlmostEqual(self.toolpath.rapid_length_mm, 20.0)
        self.assertAlmostEqual(self.toolpath.total_length_mm, 35.0)

    def test_time_accounts_for_each_feed_rate(self) -> None:
        self.assertAlmostEqual(self.toolpath.cutting_time_s, 1.5)
        self.assertGreater(self.toolpath.estimated_time_s, self.toolpath.cutting_time_s)

    def test_pass_count_counts_distinct_indices(self) -> None:
        self.assertEqual(self.toolpath.pass_count, 1)

    def test_statistics_snapshot(self) -> None:
        statistics = self.toolpath.statistics()
        self.assertEqual(statistics["move_count"], 3)
        self.assertEqual(statistics["point_count"], 8)
        self.assertIn("estimated_time_s", statistics)

    def test_payload_includes_notes_and_moves(self) -> None:
        payload = self.toolpath.to_payload()
        self.assertEqual(payload["planner_label"], "栅格刀路")
        self.assertEqual(len(payload["moves"]), 3)


class RetractTests(unittest.TestCase):
    def test_retract_lifts_across_and_plunges(self) -> None:
        move = retract_move(
            np.array([0.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0]), 5.0, 5000.0
        )
        self.assertIs(move.kind, MoveKind.RAPID)
        self.assertEqual(move.points.shape, (4, 3))
        self.assertAlmostEqual(float(move.points[1][2]), 5.0)
        self.assertAlmostEqual(float(move.points[2][2]), 5.0)

    def test_retract_never_dips_below_the_endpoints(self) -> None:
        move = retract_move(
            np.array([0.0, 0.0, 1.0]), np.array([10.0, 0.0, 1.0]), 0.0, 5000.0
        )
        self.assertAlmostEqual(float(move.points[1][2]), 1.0)


if __name__ == "__main__":
    unittest.main()
