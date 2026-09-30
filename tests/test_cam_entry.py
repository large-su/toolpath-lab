"""进刀 / 退刀 / 层间过渡的几何断言（型腔铣 · UG/NX「非切削移动」的对应物）。

断言的都是**可判定**的量：坡度是否不超过斜插角、进刀段是否首尾相接、末点是否正好
落在本层第一刀的起点、退刀是否沿切线离开、斜降会不会进岛——不依赖精确点数
（点数随栅格、半径与圈数变，绑死它只会让测试跟着实现抖）。

坡度类测试统一用 20°：3° 时 2 mm 层深要 38 mm 的水平距离，40 mm 的腔根本装不下，
测出来的会是"放不下"而不是"这档方式对不对"。
"""

from __future__ import annotations

import unittest

import numpy as np

from tests.test_cam import BASE_PARAMETERS, ISLAND, SQUARE, context_for
from toolpath_lab.cam.boundary import build_region, offset_outline_polygons
from toolpath_lab.cam.entry import (ENTRY_LABELS, Feasible, build_depart,
                                    build_entry, build_transition)
from toolpath_lab.cam.parameters import cam_parameters
from toolpath_lab.cam.pocket_mill import plan_pocket_mill
from toolpath_lab.core.path import MoveKind

#: 斜插角度（度）：run = 深度 / tan20° ≈ 2.7 × 深度，40 mm 的腔装得下。
FAST_ANGLE_DEG = 20.0
#: 斜坡测试的目标点（岛外侧的开阔地带，螺旋/圆弧都放得下）
TARGET = np.array([12.0, 12.0, 8.0])
#: 沿 +x 切削
TANGENT = np.array([1.0, 0.0])


def _slope_segments(points: np.ndarray) -> np.ndarray:
    """逐段坡度 |dz| / 水平距离（水平段记 0）。"""

    delta = np.diff(np.asarray(points, dtype=float), axis=0)
    horizontal = np.hypot(delta[:, 0], delta[:, 1])
    slope = np.zeros(delta.shape[0], dtype=float)
    moving = horizontal > 1e-9
    slope[moving] = np.abs(delta[moving, 2]) / horizontal[moving]
    return slope


def _assert_contiguous(test: unittest.TestCase, toolpath) -> None:
    """相邻运动段必须首尾相接（进刀/退刀的点列是拼进去的，最容易在这里断）。"""

    previous = None
    for move in toolpath.moves:
        points = np.asarray(move.points, dtype=float)
        if previous is not None:
            np.testing.assert_allclose(
                points[0], previous, atol=1e-9,
                err_msg=f"{move.label} 的起点与上一段终点不重合")
        previous = points[-1]


class EntryGeometryTests(unittest.TestCase):
    """直接调几何层：给定切入点与目标，断言坡度、切向与落点。"""

    def setUp(self) -> None:
        self.region = build_region(SQUARE, [ISLAND], top_z=10.0, floor_z=0.0,
                                   cell_mm=0.4)
        self.context = context_for(self.region, ramp_angle_deg=FAST_ANGLE_DEG)
        self.angle = self.context.ramp_angle_rad
        self.feasible = Feasible(
            self.region,
            self.region.offset_mask(self.context.tool_radius + self.context.stock_allowance))

    def _entry(self, method: str, **kwargs):
        values = dict(z_engage=10.0, tangent=TANGENT, method=method,
                      angle_rad=self.angle, radius_mm=self.context.entry_radius)
        values.update(kwargs)
        return build_entry(self.feasible, TARGET, **values)

    def test_ramp_entry_is_one_line_at_the_exact_angle(self) -> None:
        """斜插：一条直线，水平距离 = 深度 / tan(斜插角)，坡度正好等于斜插角。"""

        built = self._entry("ramp")
        self.assertIsNotNone(built, "开阔地带应当能放下斜插")
        start, points, used = built
        self.assertEqual(used, "ramp")
        self.assertEqual(points.shape[0], 2)
        np.testing.assert_allclose(points[-1], TARGET, atol=1e-9)
        self.assertTrue(self.feasible.contains(start))
        horizontal = float(np.hypot(*(points[-1, :2] - points[0, :2])))
        self.assertAlmostEqual(horizontal, 2.0 / np.tan(self.angle), places=6)
        self.assertAlmostEqual(_slope_segments(points).max(), np.tan(self.angle), places=6)
        self.assertTrue(np.all(np.diff(points[:, 2]) <= 1e-12), "进刀只能往下走")

    def test_helix_entry_descends_in_turns_then_lands_on_the_target(self) -> None:
        """螺旋：分若干圈下降，每段坡度都 ≤ 斜插角，末点正好是切入点。"""

        built = self._entry("helix")
        self.assertIsNotNone(built, "开阔地带应当能放下螺旋")
        start, points, used = built
        self.assertEqual(used, "helix")
        np.testing.assert_allclose(points[-1], TARGET, atol=1e-9)
        # 单调下降；末尾从圆上收进切点的那一小段是平的（同在目标高度）
        self.assertTrue(np.all(np.diff(points[:, 2]) <= 1e-12), "进刀不得回升")
        self.assertGreater(int((np.diff(points[:, 2]) < -1e-12).sum()), 10,
                           "至少绕了几圈在降")
        self.assertLessEqual(_slope_segments(points).max(),
                             np.tan(self.angle) + 1e-6)
        # 至少绕出去过一整圈（按路径长度算，不是首尾净位移）
        path = float(np.hypot(np.diff(points[:, 0]),
                              np.diff(points[:, 1])).sum())
        self.assertGreater(path, 3.0)
        self.assertTrue(self.feasible.contains(start))

    def test_arc_entry_arrives_along_the_cut_tangent(self) -> None:
        """切向圆弧：末段方向与刀轨切向重合——这是"切向进刀"的定义。"""

        built = self._entry("arc")
        self.assertIsNotNone(built, "开阔地带应当能放下圆弧进刀")
        start, points, used = built
        self.assertEqual(used, "arc")
        np.testing.assert_allclose(points[-1], TARGET, atol=1e-9)
        self.assertTrue(np.all(np.diff(points[:, 2]) <= 1e-12), "进刀只能往下走")
        self.assertLessEqual(_slope_segments(points).max(),
                             np.tan(self.angle) + 1e-6)
        direction = points[-1, :2] - points[-2, :2]
        direction = direction / np.hypot(*direction)
        self.assertGreater(float(np.dot(direction, TANGENT)), 0.98,
                           "末段必须沿切线方向进入切入点")
        self.assertTrue(self.feasible.contains(start))

    def test_entry_method_gives_up_when_nothing_fits(self) -> None:
        """半径比整个腔还大时该档返回 None：调用方据此降级（auto 换档 / 手动报警）。"""

        self.assertIsNone(self._entry("arc", radius_mm=200.0))
        self.assertIsNone(self._entry("helix", radius_mm=200.0))
        # 同样放不下时 auto 退到下一档（斜插），仍然拿得到进刀
        built = self._entry("auto", radius_mm=200.0)
        self.assertIsNotNone(built)
        self.assertEqual(built[2], "ramp")

    def test_depart_leaves_along_the_tangent_and_rises(self) -> None:
        """切向退刀：起点就是末点、只升不降、离开方向贴着切线。"""

        end = np.array([12.0, 12.0, 8.0])
        points = build_depart(self.feasible, end, TANGENT, angle_rad=self.angle,
                              radius_mm=self.context.entry_radius)
        self.assertIsNotNone(points, "开阔地带应当能甩出退刀圆弧")
        np.testing.assert_allclose(points[0], end, atol=1e-9)
        self.assertTrue(np.all(np.diff(points[:, 2]) >= -1e-12), "退刀只能往上走")
        direction = points[1, :2] - points[0, :2]
        direction = direction / np.hypot(*direction)
        self.assertGreater(float(np.dot(direction, TANGENT)), 0.98,
                           "离开方向必须与刀轨切向一致")
        arc_length = float(np.hypot(np.diff(points[:, 0]), np.diff(points[:, 1])).sum())
        self.assertLessEqual(float(points[-1, 2] - points[0, 2]),
                             arc_length * np.tan(self.angle) + 1e-6,
                             "退刀段只升到斜插角允许的高度，其余交给快速抬刀")


class LevelTransitionGeometryTests(unittest.TestCase):
    """层间连接：受控斜降 / 沿壁斜降都必须把坡度压在斜插角以内，且不进岛。"""

    def setUp(self) -> None:
        self.region = build_region(SQUARE, [ISLAND], top_z=10.0, floor_z=0.0,
                                   cell_mm=0.4)
        self.context = context_for(self.region, ramp_angle_deg=FAST_ANGLE_DEG)
        self.angle = self.context.ramp_angle_rad
        self.feasible = Feasible(
            self.region,
            self.region.offset_mask(self.context.tool_radius + self.context.stock_allowance))
        self.previous = np.array([-12.0, -12.0, 10.0])
        self.target = np.array([-9.0, -12.0, 8.0])

    def test_ramp_transition_extends_then_descends_at_the_angle(self) -> None:
        """受控斜降：先在层高上平移补足水平距离，再按斜插角下降（直线太陡才需要）。"""

        built = build_transition(self.feasible, self.previous, self.target,
                                 method="ramp", angle_rad=self.angle)
        self.assertIsNotNone(built, "直线坡度超过斜插角时应当延长")
        label, points = built
        self.assertEqual(label, "层内转移（受控斜降）")
        np.testing.assert_allclose(points[0], self.previous, atol=1e-9)
        np.testing.assert_allclose(points[-1], self.target, atol=1e-9)
        slopes = _slope_segments(points)
        self.assertLessEqual(float(slopes.max()), np.tan(self.angle) + 1e-6)
        self.assertTrue(np.all(np.diff(points[:, 2]) <= 1e-12), "斜降不得回升")
        deltas = np.diff(points, axis=0)
        run = np.hypot(deltas[:, 0], deltas[:, 1])
        self.assertTrue(bool(((np.abs(deltas[:, 2]) <= 1e-12) & (run > 1e-9)).any()),
                        "应当有一段在层高上平移补足水平距离")
        self.assertTrue(bool((deltas[:, 2] < -1e-12).any()), "应当真的降了层")
        self.assertAlmostEqual(float(slopes.max()), np.tan(self.angle), places=6,
                               msg="斜降段的坡度应当正好等于斜插角")

    def test_ramp_transition_falls_back_when_the_straight_line_is_shallow(self) -> None:
        """直线本来就够平缓时不改造它（保持老行为，省一次校验）。"""

        shallow = np.array([-12.0, -12.0, 10.0])
        far = np.array([12.0, -12.0, 8.0])
        straight = float(np.hypot(*(far[:2] - shallow[:2])))
        self.assertGreaterEqual(straight, 2.0 / np.tan(self.angle))
        self.assertIsNone(build_transition(self.feasible, shallow, far, method="ramp",
                                           angle_rad=self.angle))

    def test_wall_ramp_walks_the_ring_out_of_the_island(self) -> None:
        """沿壁斜降：绕腔壁环走，坡度被绕行长度摊平，且整条不进岛。"""

        offset = self.context.tool_radius + self.context.stock_allowance
        loops = [np.vstack([p, p[:1]]) for p in offset_outline_polygons(self.region, offset)
                 if p.shape[0] >= 3]
        ring = max(loops, key=lambda loop: float(
            np.hypot(np.diff(loop[:, 0]), np.diff(loop[:, 1])).sum()))
        built = build_transition(self.feasible, np.array([-12.0, -12.0, 10.0]),
                                 np.array([12.0, -12.0, 8.0]),
                                 method="wall_ramp", angle_rad=self.angle,
                                 wall_ring=ring)
        self.assertIsNotNone(built, "沿壁斜降应当在环上绕过去")
        label, points = built
        self.assertEqual(label, "层内转移（沿壁斜降）")
        np.testing.assert_allclose(points[0], [-12.0, -12.0, 10.0], atol=1e-9)
        np.testing.assert_allclose(points[-1], [12.0, -12.0, 8.0], atol=1e-9)
        self.assertLessEqual(float(_slope_segments(points).max()),
                             np.tan(self.angle) + 1e-6)
        self.assertTrue(np.all(np.diff(points[:, 2]) <= 1e-12))
        inside_island = (np.abs(points[:, 0]) < 5.0) & (np.abs(points[:, 1]) < 5.0)
        self.assertFalse(bool(inside_island.any()), "沿壁斜降不得穿进岛屿")

    def test_wall_ramp_spreads_the_drop_by_length_not_by_point(self) -> None:
        """腔壁环边长不均时高差按**弦长**摊——0.1 mm 的碎段不会吃掉整条的高差。

        按点的序号均分高差时，这条 0.1 mm 的短边会拿走 2/3 里的一份，坡度冲到
        6.7（设定 20° 才 0.36），机床照单执行就是硬拐。
        """

        ring = np.array([[-14.7, -14.7], [0.0, -14.7], [0.1, -14.7],
                         [14.7, -14.7], [14.7, 14.7], [-14.7, 14.7]])
        built = build_transition(self.feasible, np.array([-10.0, -12.0, 10.0]),
                                 np.array([10.0, -12.0, 8.0]),
                                 method="wall_ramp", angle_rad=self.angle,
                                 wall_ring=ring)
        self.assertIsNotNone(built)
        _, points = built
        self.assertLessEqual(float(_slope_segments(points).max()),
                             np.tan(self.angle) + 1e-9,
                             "短边分到的高差必须与它的长度成比例")

    def test_direct_never_rewrites_the_straight_line(self) -> None:
        """直接连接：几何层不生成任何点，调用方照旧用两点直线。"""

        self.assertIsNone(build_transition(self.feasible, self.previous, self.target,
                                           method="direct", angle_rad=self.angle))
        self.assertIsNone(build_transition(self.feasible, self.previous, self.target,
                                           method="safe", angle_rad=self.angle))
        # 没有降层（同层平移）也不该造点
        same = np.array([-9.0, -12.0, 8.0])
        self.assertIsNone(build_transition(self.feasible, same, same, method="ramp",
                                           angle_rad=self.angle))


class PocketEntryTests(unittest.TestCase):
    """整条型腔铣刀路端到端：进刀怎么下、层间怎么连、退刀怎么走。"""

    def _plan(self, *, island: bool = False, **overrides):
        """默认不带岛：带岛时机壁转移本来就频繁回退抬刀（与本特性无关）。"""

        region = build_region(SQUARE, [ISLAND] if island else [], top_z=10.0,
                              floor_z=0.0, cell_mm=0.4)
        context = context_for(region, **overrides)
        return plan_pocket_mill(context), region, context

    @staticmethod
    def _entries(toolpath) -> list:
        return [move for move in toolpath.moves
                if move.kind is MoveKind.CUT and "下刀" in move.label]

    @staticmethod
    def _first_ring(toolpath):
        return next(move for move in toolpath.moves
                    if move.kind is MoveKind.CUT and "条刀轨" in move.label)

    def test_auto_entry_is_a_slope_limited_descent_not_a_plunge(self) -> None:
        """默认「自动」：整条刀路只下一次刀，且这一刀是带斜率的（不再垂直直插）。"""

        toolpath, region, _ = self._plan()
        entries = self._entries(toolpath)
        self.assertEqual(len(entries), 1, "整条刀路只允许出现一次下刀")
        entry = np.asarray(entries[0].points, dtype=float)
        self.assertTrue(
            any(name in entries[0].label for name in ("切向圆弧", "螺旋", "斜插")),
            f"自动进刀不该退化成垂直直插：{entries[0].label}")
        # 首尾：从材料顶面开始，末点正好是第一刀的起点
        self.assertAlmostEqual(float(entry[0, 2]), float(region.top_z), places=6)
        np.testing.assert_allclose(entry[-1], self._first_ring(toolpath).points[0],
                                   atol=1e-9)
        # 下降不回抽、坡度不超过斜插角
        self.assertTrue(np.all(np.diff(entry[:, 2]) <= 1e-12))
        self.assertLessEqual(float(_slope_segments(entry).max()),
                             np.tan(np.radians(3.0)) + 1e-6)
        _assert_contiguous(self, toolpath)

    def test_each_entry_method_lands_on_the_first_ring(self) -> None:
        """四档手动方式各自的落点与形状（斜插=两段直线、垂直=原地直插）。"""

        for method, token in (("arc", "切向圆弧"), ("helix", "螺旋"),
                              ("ramp", "斜插"), ("plunge", "垂直")):
            with self.subTest(method=method):
                toolpath, _, _ = self._plan(entry_method=method,
                                            ramp_angle_deg=FAST_ANGLE_DEG)
                entries = self._entries(toolpath)
                self.assertEqual(len(entries), 1)
                points = np.asarray(entries[0].points, dtype=float)
                np.testing.assert_allclose(points[-1],
                                           self._first_ring(toolpath).points[0],
                                           atol=1e-9,
                                           err_msg=f"{method} 没落在第一刀的起点上")
                horizontal = float(np.hypot(np.diff(points[:, 0]).sum(),
                                            np.diff(points[:, 1]).sum()))
                if method == "plunge":
                    self.assertIn("下刀 Z", entries[0].label,
                                  "垂直档应当保持原来的原地直插标签")
                    self.assertEqual(points.shape[0], 2)
                    self.assertAlmostEqual(horizontal, 0.0, places=9,
                                           msg="垂直下刀的 XY 不能动")
                else:
                    self.assertIn(token, entries[0].label)
                    self.assertGreater(horizontal, 0.5,
                                       f"{method} 没有产生任何水平移动")
                _assert_contiguous(self, toolpath)

    def test_manual_entry_method_that_cannot_fit_warns_and_plunges(self) -> None:
        """手动指定的档放不下时：报警 + 垂直兜底，刀路照样生成。

        用一个 24 mm 的窄腔（刀 D10，可行区只剩 13.4 mm）：四档候选半径最小
        12.5 mm 的圆也放不下，连"沿切向挪开"都救不了。
        """

        small = np.array([[-12.0, -12.0], [12.0, -12.0],
                          [12.0, 12.0], [-12.0, 12.0]])
        region = build_region(small, [], top_z=10.0, floor_z=0.0, cell_mm=0.4)
        context = context_for(region, entry_method="arc", entry_radius_mm=100.0)
        toolpath = plan_pocket_mill(context)
        entries = self._entries(toolpath)
        self.assertEqual(len(entries), 1)
        self.assertIn("下刀 Z", entries[0].label)
        self.assertTrue(any("放不下" in message for message in context.warnings),
                        f"应当报警，实际：{context.warnings}")
        self.assertGreater(toolpath.cut_length_mm, 0.0)

    def test_default_transition_keeps_levels_chained_without_a_lift(self) -> None:
        """默认「受控斜降」：下刀之后不再出现抬到安全面的移动。"""

        toolpath, _, _ = self._plan()
        entry_index = next(index for index, move in enumerate(toolpath.moves)
                           if move.kind is MoveKind.CUT and "下刀" in move.label)
        for move in toolpath.moves[entry_index + 1:]:
            z = np.asarray(move.points, dtype=float)[:, 2]
            self.assertLessEqual(float(z.max()), 10.0 + 1e-6,
                                 f"{move.label} 抬出了层高")
        descs = [move for move in toolpath.moves
                 if move.kind is MoveKind.LINK and np.ptp(
                     np.asarray(move.points, dtype=float)[:, 2]) > 1e-9]
        self.assertGreaterEqual(len(descs), 4, "5 层至少要 4 段带高度变化的层间连接")

    def test_direct_transition_labels_every_link_the_old_way(self) -> None:
        """「直接连接」：连接段标签保持老样式，不出现斜降字样。"""

        toolpath, _, _ = self._plan(level_transition="direct")
        links = [move for move in toolpath.moves if move.kind is MoveKind.LINK]
        self.assertTrue(links)
        for move in links:
            if "层内转移" in (move.label or ""):
                self.assertEqual(move.label, "层内转移")

    def test_wall_ramp_transition_is_used_and_stays_out_of_the_island(self) -> None:
        """「沿壁斜降」：真的走到了环上，且连接段不进岛、坡度受控。"""

        toolpath, _, _ = self._plan(island=True, level_transition="wall_ramp",
                                    ramp_angle_deg=FAST_ANGLE_DEG)
        walls = [move for move in toolpath.moves
                 if move.kind is MoveKind.LINK and "沿壁斜降" in (move.label or "")]
        self.assertTrue(walls, "沿壁斜降应当至少出现一次")
        angle = np.tan(np.radians(FAST_ANGLE_DEG))
        for move in walls:
            points = np.asarray(move.points, dtype=float)
            self.assertLessEqual(float(_slope_segments(points).max()),
                                 angle + 1e-6, f"{move.label} 坡度超标")
            inside = (np.abs(points[:, 0]) < 5.0) & (np.abs(points[:, 1]) < 5.0)
            self.assertFalse(bool(inside.any()), f"{move.label} 穿进了岛屿")
        _assert_contiguous(self, toolpath)

    def test_safe_transition_lifts_and_re_engages_every_level(self) -> None:
        """「抬刀转移」：每层之间都抬到安全面再重新进刀。"""

        toolpath, _, _ = self._plan(level_transition="safe")
        entries = self._entries(toolpath)
        self.assertGreaterEqual(len(entries), 5, "抬刀转移意味着每层都要重新进刀")
        safe_height = 10.0 + BASE_PARAMETERS["safe_height_mm"]
        lifted = [move for move in toolpath.moves
                  if move.kind is MoveKind.RAPID
                  and float(np.asarray(move.points, dtype=float)[:, 2].max()) >= safe_height - 1e-6]
        self.assertTrue(lifted, "应当抬到安全面")

    def test_tangential_retract_departs_after_the_last_cut(self) -> None:
        """「切向圆弧」退刀：最后一刀之后先甩出去，再由收尾抬到安全面。"""

        toolpath, _, _ = self._plan(retract_method="arc")
        self._assert_retract_after_last_cut(toolpath)
        _assert_contiguous(self, toolpath)

    def test_default_retract_is_a_plain_lift(self) -> None:
        """默认「直接抬刀」：不产生额外的退刀段。"""

        toolpath, _, _ = self._plan()
        self._assert_retract_after_last_cut(toolpath)

    def _assert_retract_after_last_cut(self, toolpath) -> None:
        cut_labels = [move.label for move in toolpath.moves
                      if move.kind is MoveKind.CUT and "下刀" not in (move.label or "")]
        self.assertTrue(cut_labels)
        index = max(index for index, move in enumerate(toolpath.moves)
                    if move.kind is MoveKind.CUT)
        following = toolpath.moves[index + 1:]
        if not following:
            return  # 已经在安全面上（不会发生：收尾会补抬刀）
        retract = following[0]
        self.assertIn("退刀", retract.label or "")
        if "切向圆弧" not in (retract.label or ""):
            return  # 直接抬刀：就是收尾那条抬刀
        previous_cut = np.asarray(toolpath.moves[index].points, dtype=float)
        direction = previous_cut[-1, :2] - previous_cut[-2, :2]
        direction = direction / np.hypot(*direction)
        points = np.asarray(retract.points, dtype=float)
        leave = points[1, :2] - points[0, :2]
        leave = leave / np.hypot(*leave)
        self.assertGreater(float(np.dot(leave, direction)), 0.98,
                           "退刀的第一步必须沿刀轨切向离开")
        self.assertTrue(np.all(np.diff(points[:, 2]) >= -1e-12), "退刀不得下降")


class EntryParameterTests(unittest.TestCase):
    """新参数必须由声明驱动地出现在目录里（界面与校验都靠它）。"""

    def test_five_specs_are_declared_with_the_expected_defaults(self) -> None:
        specs = {spec.key: spec for spec in cam_parameters()}
        self.assertEqual(specs["entry_method"].default, "auto")
        self.assertEqual(specs["retract_method"].default, "lift")
        self.assertEqual(specs["level_transition"].default, "ramp")
        self.assertEqual(specs["ramp_angle_deg"].default, 3.0)
        self.assertEqual(specs["entry_radius_mm"].default, 0.0)
        self.assertEqual([choice.value for choice in specs["entry_method"].choices],
                         ["auto", "arc", "helix", "ramp", "plunge"])
        self.assertEqual([choice.value for choice in specs["level_transition"].choices],
                         ["ramp", "wall_ramp", "direct", "safe"])
        for key in ("entry_method", "retract_method", "level_transition",
                    "ramp_angle_deg", "entry_radius_mm"):
            self.assertEqual(specs[key].group, "进退刀", key)
        self.assertEqual(set(ENTRY_LABELS), {"arc", "helix", "ramp", "plunge"})


if __name__ == "__main__":
    unittest.main()
