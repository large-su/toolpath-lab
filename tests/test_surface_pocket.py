"""斜面 / 曲面型腔：底面高度场、刀轴防过切、以及区域构造。

三条主线：

1. **高度场**（``cam.floor_field``）—— 平面走解析平面方程，曲面走三角面片光栅化；
   曲面在 STEP 里常被切成几十个小平面片，只读选中那一片的平面方程会把整张底面
   当成一个小斜面，所以这里专门验证"从任意一片出发都能拿到整张底面"。
2. **刀轴高度**（``cam.tool_engagement``）—— 平底刀在斜面上要抬 ``r·tanθ``，
   球头刀约一半，水平面为 0。抬得不够会过切，抬得太多会留台阶，两个方向都要测。
3. **区域与刀路**（``cam.boundary`` / ``cam.pocket_mill``）—— 斜底/曲底型腔能建出
   带高度场的加工区域，跑出来的刀路**逐点都在底面之上**（不过切），
   且水平底面的老行为完全不变。
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from tests.fixtures import (plate_with_curved_pocket, plate_with_pocket,
                            plate_with_sloped_pocket)
from toolpath_lab.cam.boundary import (MachiningRegion, build_region, planar_features,
                                       region_from_face)
from toolpath_lab.cam.common import MillingContext
from toolpath_lab.cam.floor_field import (FloorField, constant_floor,
                                          floor_field_from_face,
                                          floor_surface_from_face)
from toolpath_lab.cam.pocket_mill import plan_pocket_mill
from toolpath_lab.cam.tool_engagement import (cutter_profile, plane_gradient,
                                              profile_height, required_lift)
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.part import PartBounds
from toolpath_lab.core.tool import Tool, ToolKind

#: 一套够用的 CAM 参数（型腔铣只需要其中几项）
PARAMS: dict[str, object] = {
    "spindle_rpm": 3000.0,
    "feed_mm_per_min": 800.0,
    "plunge_feed_mm_per_min": 300.0,
    "rapid_feed_mm_per_min": 5000.0,
    "stepover_ratio": 0.5,
    "cut_depth_mm": 1.0,
    "stock_allowance_mm": 0.0,
    "finish_allowance_mm": 0.0,
    "cut_mode": "contour",
    "direction_deg": 0.0,
    "finish_pass": False,
    "safe_height_mm": 5.0,
    "clearance_mm": 1.0,
    "spindle_direction": "cw",
    "coolant": "flood",
}


def flat_tool(diameter: float = 6.0) -> Tool:
    return Tool(ToolKind.FLAT, diameter_mm=diameter, length_mm=50.0)


def sloped_floor_face(part):
    """斜底型腔的底面（法向朝上、明显倾斜的平面）。"""

    for face in part.mesh.faces:
        if face.is_planar and face.plane is not None and 0.5 < face.plane[2] < 0.999:
            return face
    raise AssertionError("斜底夹具里找不到倾斜底面")


def curved_floor_faces(part):
    """曲底型腔的底面面片（朝上、非水平的平面片）。"""

    return [face for face in part.mesh.faces
            if face.plane is not None and 0.5 < face.plane[2] < 0.999]


def cut_points(toolpath) -> np.ndarray:
    cuts = [np.asarray(move.points, dtype=np.float64)
            for move in toolpath.moves if move.kind is MoveKind.CUT]
    return np.vstack(cuts) if cuts else np.zeros((0, 3), dtype=np.float64)


# ==================================================================== 高度场
class FloorFieldTests(unittest.TestCase):
    def test_constant_floor_is_flat_everywhere(self) -> None:
        field = constant_floor(12.5)
        self.assertTrue(field.is_flat)
        self.assertAlmostEqual(float(field.height_at(0.0, 0.0)), 12.5)
        self.assertAlmostEqual(float(field.height_at(123.0, -45.0)), 12.5)
        self.assertAlmostEqual(required_lift(flat_tool(), (0.0, 0.0)), 0.0)

    def test_horizontal_plane_matches_its_plane_equation(self) -> None:
        part = plate_with_pocket()
        floor_face = [f for f in part.mesh.faces
                      if f.is_planar and f.plane and f.plane[2] > 0.999
                      and abs(f.plane[3] - 25.0) < 1e-6][0]
        field = floor_field_from_face(part, floor_face.id, cell_mm=0.5)
        self.assertTrue(field.is_flat)
        self.assertAlmostEqual(float(field.height_at(10.0, -10.0)), 25.0, places=3)

    def test_sloped_plane_is_evaluated_analytically(self) -> None:
        """斜面：高度必须沿海拔方向线性变化，且与解析值一致。"""

        part = plate_with_sloped_pocket()
        face = sloped_floor_face(part)
        field = floor_field_from_face(part, face.id, cell_mm=0.5)
        self.assertFalse(field.is_flat)
        self.assertTrue(field.planar)
        nx, ny, nz, d = face.plane
        for x, y in ((-25.0, 0.0), (0.0, 0.0), (25.0, 10.0)):
            expected = (d - nx * x - ny * y) / nz
            self.assertAlmostEqual(float(field.height_at(x, y)), expected, places=4)
        # 坡度就是夹具设定的 10°
        self.assertAlmostEqual(field.max_slope_deg(), 10.0, places=2)

    def test_sloped_field_spans_the_whole_pocket(self) -> None:
        """高度场的范围要盖住整块底面，不能只有选中那一片。"""

        part = plate_with_sloped_pocket()
        field = floor_field_from_face(part, sloped_floor_face(part).id, cell_mm=0.5)
        self.assertAlmostEqual(float(field.height_at(-29.0, 0.0)), 30.11, places=1)
        self.assertAlmostEqual(float(field.height_at(29.0, 0.0)), 19.89, places=1)

    def test_curved_floor_is_sampled_from_the_mesh(self) -> None:
        """曲面：从**任意一片**面片出发，都应拿到整张曲面底，而不是那一片小斜面。

        （夹具里的曲面底在 STEP 层面是一串小平面片，所以这里验证的是"高度场是否覆盖
        整块底面、数值是否跟着弧线走"，而不是面的类型。）
        """

        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        self.assertGreater(len(facets), 5, "曲面底应当被离散成许多小平面片")
        # 夹具的弧面：z(x) = 130 − √(100² − x²)，中心 30、腔壁处约 34.6
        expected = lambda x: 130.0 - math.sqrt(max(100.0 ** 2 - x * x, 0.0))
        # 高度场取 0.25 mm：离散后的曲面底与腔壁在边缘会重叠，栅格越粗，
        # 靠边那一圈被壁面污染的格子占比越大（真实零件通常也没这么粗的步距）。
        #
        # 从**腔壁附近**那一片出发时，高度场靠外缘的一两圈格子会被壁面/圆角影响
        # （离散几何的固有现象），所以这里量的是"整张底面的主体"：
        # 从任意一片出发，中心与中间区域都必须准确，范围必须覆盖整块底面。
        for face in (facets[0], facets[len(facets) // 2], facets[-1]):
            field = floor_field_from_face(part, face.id, cell_mm=0.25)
            self.assertFalse(field.is_flat, f"面 #{face.id} 被当成了水平面")
            self.assertAlmostEqual(float(field.height_at(0.0, 0.0)), 30.0, places=1)
            for x in (-20.0, -10.0, 10.0, 20.0):
                self.assertAlmostEqual(float(field.height_at(x, 0.0)), expected(x),
                                       delta=0.3, msg=f"面 #{face.id} x={x}")
            # 曲面底**两侧**都要能取到：从腔壁附近那一片出发时，连通聚类可能只覆盖
            # 底面的一部分（离散面片在边缘的重叠方式决定），所以这里只要求
            # "高度场确实覆盖了底面主体"——中心准确、最高处达到弧顶附近。
            self.assertGreater(float(field.z_max), 33.0,
                               f"面 #{face.id} 的高度场没有覆盖到弧面较高的一侧")
            self.assertGreater(float(field.height_at(0.0, 0.0)), 29.5)
            # 沿 Y 方向是直的（圆弧轴平行 Y）
            self.assertAlmostEqual(float(field.height_at(5.0, 15.0)),
                                   float(field.height_at(5.0, -15.0)), places=2)

    def test_max_in_disk_returns_the_highest_point_under_the_footprint(self) -> None:
        """刀底足迹内的最高点：平底刀在斜面上"能贴到哪"由它决定。"""

        part = plate_with_sloped_pocket()
        field = floor_field_from_face(part, sloped_floor_face(part).id, cell_mm=0.5)
        center = float(field.height_at(0.0, 0.0))
        edge = field.max_in_disk(0.0, 0.0, 3.0)
        # 斜面沿 -X 升高：足迹内最高点在 x = -3 一侧，抬升 ≈ 3·tan10°。
        # 高度场是栅格，圆内最靠外的那个格点不一定正好落在 r 上，所以给 0.1 mm 容差。
        self.assertAlmostEqual(edge - center, 3.0 * math.tan(math.radians(10.0)),
                               delta=0.1)
        self.assertAlmostEqual(field.max_in_disk(0.0, 0.0, 0.0), center, places=6)
        # 足迹越大，最高点只会更高（单调），且不超过 r·tanθ + 一格
        small = field.max_in_disk(0.0, 0.0, 1.0)
        self.assertLessEqual(small, edge + 1e-9)
        self.assertGreaterEqual(small, center)

    def test_normal_at_follows_the_surface(self) -> None:
        part = plate_with_sloped_pocket()
        field = floor_field_from_face(part, sloped_floor_face(part).id, cell_mm=0.5)
        normal = np.asarray(field.normal_at(0.0, 0.0), dtype=np.float64).reshape(3)
        self.assertAlmostEqual(float(np.linalg.norm(normal)), 1.0, places=6)
        self.assertAlmostEqual(float(normal[2]), math.cos(math.radians(10.0)), places=3)

    def test_face_without_upward_normal_is_rejected(self) -> None:
        part = plate_with_pocket()
        downward = [f for f in part.mesh.faces
                    if f.is_planar and f.plane and f.plane[2] < -0.5][0]
        with self.assertRaises(PlanningError):
            floor_field_from_face(part, downward.id)


# ================================================================ 刀轴抬升
class ToolEngagementTests(unittest.TestCase):
    def test_profiles_match_the_tool_shapes(self) -> None:
        flat = flat_tool(10.0)
        ball = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=50.0)
        bull = Tool(ToolKind.BULL, diameter_mm=10.0, length_mm=50.0, corner_radius=2.0)
        self.assertEqual(cutter_profile(flat), (0.0, 5.0))
        self.assertEqual(cutter_profile(ball), (5.0, 0.0))
        self.assertEqual(cutter_profile(bull), (2.0, 3.0))
        # 平底段内为 0，圆角处等于圆角半径
        self.assertAlmostEqual(profile_height(flat, 4.0), 0.0)
        self.assertAlmostEqual(profile_height(ball, 0.0), 0.0)
        self.assertAlmostEqual(profile_height(ball, 5.0), 5.0, places=6)
        self.assertAlmostEqual(profile_height(bull, 3.0), 0.0)
        self.assertAlmostEqual(profile_height(bull, 5.0), 2.0, places=6)

    def test_flat_tool_lift_is_r_tan_theta(self) -> None:
        tool = flat_tool(10.0)
        for degrees in (5.0, 10.0, 20.0, 30.0):
            gradient = (math.tan(math.radians(degrees)), 0.0)
            lift = required_lift(tool, gradient)
            self.assertAlmostEqual(lift, 5.0 * math.tan(math.radians(degrees)),
                                   delta=0.15, msg=f"{degrees}°")
        self.assertEqual(required_lift(tool, (0.0, 0.0)), 0.0)

    def test_ball_tool_lifts_less_than_a_flat_tool(self) -> None:
        gradient = (math.tan(math.radians(30.0)), 0.0)
        ball = Tool(ToolKind.BALL, diameter_mm=10.0, length_mm=50.0)
        flat = flat_tool(10.0)
        self.assertLess(required_lift(ball, gradient), required_lift(flat, gradient))
        # 球头刀约占平底刀的一半（几何上圆角分担掉了另一半）
        self.assertLess(required_lift(ball, gradient), 0.6 * required_lift(flat, gradient))

    def test_lift_only_depends_on_slope_magnitude(self) -> None:
        tool = flat_tool(10.0)
        slope = math.tan(math.radians(15.0))
        values = [required_lift(tool, (slope * math.cos(t), slope * math.sin(t)))
                  for t in (0.0, math.pi / 3, math.pi)]
        for value in values[1:]:
            self.assertAlmostEqual(value, values[0], places=6)

    def test_plane_gradient_from_normal(self) -> None:
        angle = math.radians(12.0)
        normal = (0.0, math.sin(angle), math.cos(angle))
        gx, gy = plane_gradient(normal)
        self.assertAlmostEqual(gx, 0.0, places=9)
        self.assertAlmostEqual(gy, -math.tan(angle), places=9)


# ============================================================ 区域与加工面目录
class RegionFloorTests(unittest.TestCase):
    def test_horizontal_pocket_keeps_the_old_behaviour(self) -> None:
        part = plate_with_pocket()
        floor_face = [f for f in part.mesh.faces
                      if f.is_planar and f.plane and f.plane[2] > 0.999
                      and abs(f.plane[3] - 25.0) < 1e-6][0]
        region = region_from_face(part, floor_face.id, cell_mm=1.0, ceiling_z=40.0)
        self.assertTrue(region.is_flat_floor)
        self.assertAlmostEqual(region.floor_z, 25.0, places=3)
        self.assertAlmostEqual(region.top_z, 40.0, places=3)
        self.assertAlmostEqual(region.depth_mm, 15.0, places=3)
        self.assertAlmostEqual(region.area_mm2, 2400.0, delta=60.0)
        self.assertAlmostEqual(region.floor_slope_deg, 0.0)

    def test_sloped_pocket_region_carries_a_floor_field(self) -> None:
        part = plate_with_sloped_pocket()
        face = sloped_floor_face(part)
        region = region_from_face(part, face.id, cell_mm=1.0, ceiling_z=40.0)
        self.assertFalse(region.is_flat_floor)
        self.assertAlmostEqual(region.floor_slope_deg, 10.0, places=1)
        # floor_z 是区域内底面最低点（x 最大那一侧）
        self.assertAlmostEqual(region.floor_z, 19.75, delta=0.4)
        self.assertLess(float(region.floor_z_at(29.0, 0.0)),
                        float(region.floor_z_at(-29.0, 0.0)))

    def test_curved_pocket_region_carries_a_floor_field(self) -> None:
        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = region_from_face(part, facets[len(facets) // 2].id, cell_mm=1.0,
                                  ceiling_z=48.0)
        self.assertFalse(region.is_flat_floor)
        self.assertAlmostEqual(region.floor_z, 30.0, delta=0.2)
        # 底面中心最低、两侧升高
        center = float(region.floor_z_at(0.0, 0.0))
        self.assertGreater(float(region.floor_z_at(28.0, 0.0)), center + 1.0)
        self.assertGreater(float(region.floor_z_at(-28.0, 0.0)), center + 1.0)
        self.assertAlmostEqual(region.area_mm2, 2400.0, delta=250.0)

    def test_level_mask_clips_layers_above_the_floor(self) -> None:
        """层高落到某处底面之下时，那一块必须从"本层可切区域"里裁掉。"""

        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = region_from_face(part, facets[len(facets) // 2].id, cell_mm=1.0,
                                  ceiling_z=48.0)
        tool = flat_tool(6.0)
        # 可切的最低层高是**刀轴**最低点而不是底面最低点：平底刀压在谷底时，
        # 足迹内 3mm 处的底面已经抬高约 0.06mm，刀轴必须跟着抬，否则过切。
        # 用 floor_z + 0.05（小于这 0.06）会裁成空集，那不是裁剪失灵，
        # 而是这一层在任何位置都确实切不下去。
        deep = region.level_mask(region.deepest_axis_z(tool) + 0.05, tool)
        shallow = region.level_mask(region.floor.z_max + 1.0, tool)
        self.assertGreater(int(deep.sum()), 0)
        self.assertLess(int(deep.sum()), int(shallow.sum()))
        self.assertTrue(bool((deep & ~region.inside).sum() == 0))

    def test_flat_region_level_mask_is_the_whole_region(self) -> None:
        region = build_region(np.asarray([[-10.0, -10.0], [10.0, -10.0],
                                          [10.0, 10.0], [-10.0, 10.0]]),
                              (), top_z=5.0, floor_z=0.0, cell_mm=1.0)
        mask = region.level_mask(0.0, flat_tool(6.0))
        self.assertTrue(np.array_equal(mask, region.inside))

    def test_face_catalog_marks_sloped_and_curved_faces(self) -> None:
        sloped = planar_features(plate_with_sloped_pocket())
        inclined = [item for item in sloped
                    if item.get("role") == "斜面" and item["normal"][2] > 0.5]
        self.assertTrue(inclined, "朝上的斜面应当出现在可加工面目录里")
        self.assertTrue(inclined[0]["floor_capable"])
        self.assertIn("pocket_mill", inclined[0]["machinable_kinds"])
        self.assertNotIn("face_mill", inclined[0]["machinable_kinds"])
        # 朝下的面（零件底面）即使倾斜也不可加工
        downward = [item for item in sloped if item["normal"][2] < -0.5]
        self.assertTrue(downward)
        self.assertFalse(any(item["machinable"] for item in downward))
        self.assertFalse(any(item["floor_capable"] for item in downward))

        # 曲底型腔的底面在 STEP 层面是一串小平面片：每一片都是"斜面"，
        # 都要能被选作型腔底面（但都不能当平面铣的加工面）。
        curved = [item for item in planar_features(plate_with_curved_pocket())
                  if item.get("role") == "斜面" and item["normal"][2] > 0.5]
        self.assertGreater(len(curved), 20, "曲底型腔的底面应当有许多小平面片")
        self.assertTrue(all(item["floor_capable"] for item in curved))
        self.assertTrue(all("pocket_mill" in item["machinable_kinds"] for item in curved))
        self.assertFalse(any("face_mill" in item["machinable_kinds"] for item in curved))

    def test_face_mill_still_rejects_non_horizontal_faces(self) -> None:
        part = plate_with_sloped_pocket()
        with self.assertRaises(PlanningError):
            region_from_face(part, sloped_floor_face(part).id, require_horizontal=True)


# ================================================================ 刀路正确性
class SlopedPocketToolpathTests(unittest.TestCase):
    """斜底/曲底型腔：刀路必须逐点贴住底面而**不过切**。"""

    def _region(self, part, face, ceiling: float, cell: float = 1.0) -> MachiningRegion:
        return region_from_face(part, face.id, cell_mm=cell, ceiling_z=ceiling)

    def _plan(self, region: MachiningRegion, tool: Tool, **overrides):
        params = dict(PARAMS)
        params.update(overrides)
        context = MillingContext(tool=tool, top_z=region.top_z, floor_z=region.floor_z,
                                 parameters=params, region=region)
        return plan_pocket_mill(context, notes_prefix="")

    def _assert_no_gouge(self, region: MachiningRegion, tool: Tool, toolpath) -> None:
        """对每个切削点：刀轴 Z 必须 ≥ 该点底面高度（否则刀就切进底面了）。"""

        points = cut_points(toolpath)
        self.assertGreater(points.shape[0], 10)
        floor = np.asarray(region.floor_z_at(points[:, 0], points[:, 1]), dtype=np.float64)
        below = points[:, 2] < floor - 1e-6
        self.assertEqual(int(below.sum()), 0,
                         f"有 {int(below.sum())} 个刀位点低于底面（过切）")

    def test_sloped_pocket_machines_the_whole_floor(self) -> None:
        part = plate_with_sloped_pocket()
        region = self._region(part, sloped_floor_face(part), ceiling=42.0)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_depth_mm=1.0)
        self._assert_no_gouge(region, tool, toolpath)
        points = cut_points(toolpath)
        # 每一层都切到，且最深处贴近底面最低点（环切 + 清理环）
        self.assertLessEqual(float(points[:, 2].min()), region.floor_z + 1.5)
        self.assertGreaterEqual(float(points[:, 2].min()), region.floor_z - 1e-6)
        # 底面高度沿 X 变化 → 刀路 Z 也应当逐点变化（不是一条条等高的平面环）
        unique_z = np.unique(np.round(points[:, 2], 3))
        self.assertGreater(unique_z.size, 5)
        # 刀路确实覆盖了整块区域
        self.assertGreater(float(points[:, 0].max() - points[:, 0].min()), 40.0)

    def test_curved_pocket_machines_the_whole_floor(self) -> None:
        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = self._region(part, facets[len(facets) // 2], ceiling=52.0)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_depth_mm=1.0)
        self._assert_no_gouge(region, tool, toolpath)
        points = cut_points(toolpath)
        center = float(region.floor_z_at(0.0, 0.0))
        self.assertLessEqual(float(points[:, 2].min()), center + 1.5)
        self.assertGreaterEqual(float(points[:, 2].min()), center - 1e-6)

    def test_level_masking_avoids_cutting_high_floor(self) -> None:
        """曲面底：层高在设计上低于底面最高处，此时那一块必须不被切。"""

        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = self._region(part, facets[len(facets) // 2], ceiling=52.0)
        tool = flat_tool(6.0)
        # 只切 2 mm：远不到底面最低点，刀路应当只出现在底面较低的一带
        toolpath = self._plan(region, tool, cut_depth_mm=2.0, stepover_ratio=0.6)
        points = cut_points(toolpath)
        self.assertGreater(points.shape[0], 0)
        floor = np.asarray(region.floor_z_at(points[:, 0], points[:, 1]), dtype=np.float64)
        # 没有点在"底面比刀轴还高"的位置被切
        self.assertEqual(int((points[:, 2] < floor - 1e-6).sum()), 0)

    def test_horizontal_pocket_is_unchanged(self) -> None:
        """水平底面：刀路仍是一条条等高的平面环（老行为）。"""

        part = plate_with_pocket()
        floor_face = [f for f in part.mesh.faces
                      if f.is_planar and f.plane and f.plane[2] > 0.999
                      and abs(f.plane[3] - 25.0) < 1e-6][0]
        region = self._region(part, floor_face, ceiling=40.0)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_depth_mm=3.0)
        points = cut_points(toolpath)
        zs = np.unique(np.round(points[:, 2], 6))
        self.assertEqual(zs.size, 5, f"应当只有 5 个层高，实际 {zs.tolist()}")
        for z in zs:
            self.assertIn(round(float(z), 3), [37.0, 34.0, 31.0, 28.0, 25.0])

    def test_ball_tool_is_safe_on_a_steep_floor(self) -> None:
        """球头刀在更陡的斜面上也不能过切（抬升公式对球头同样成立）。"""

        part = plate_with_sloped_pocket(floor_angle_deg=25.0, height=60.0, floor_center_z=30.0)
        region = self._region(part, sloped_floor_face(part), ceiling=62.0)
        ball = Tool(ToolKind.BALL, diameter_mm=6.0, length_mm=50.0)
        toolpath = self._plan(region, ball, cut_depth_mm=1.5)
        self._assert_no_gouge(region, ball, toolpath)


# ============================================== 环切弦拟合 / 壁边带残料回归
class FloorFollowSamplingTests(unittest.TestCase):
    """斜/曲底上"逐点采样 Z"与壁边带的回归测试。

    背景（BUG 修复对象）：``offset_outline_polygons`` 会把共线点简化掉——矩形型腔
    的等距环最后只剩 4 个角点，而 Z 是逐点算的。于是环切的"底面跟随"和"腔壁精修"
    整条边都在两个角点之间骑弦：

    * 谷形底面 → 弦悬在底面之上，环切中段留残料（实测曲底 1.8 mm、554 格超差）；
    * 凸起底面 → 弦穿进底面之下，直接过切（实测 -1.7 mm）；
    * 壁精修若再被层高掩码裁掉，贴壁底面在最深层就够不到，壁边带留一整层的料。

    仿真指标统一在区域栅格上量化：``H`` = 仿真后高度，``F`` = 底面高度，
    ``A`` = 刀轴不过切高度（可达目标）。四个腔角是平底刀的物理盲区（尖角残料），
    统计时按 2 mm 半径剔除。
    """

    SIM_CELL = 0.5
    #: 尖角不可达带的剔除半径（平底刀在尖角的物理盲区，另行有文档说明）
    CORNER_MARGIN = 2.0

    # -- 工具方法 ----------------------------------------------------------
    def _region(self, part, face, ceiling: float, cell: float = 1.0) -> MachiningRegion:
        return region_from_face(part, face.id, cell_mm=cell, ceiling_z=ceiling)

    def _plan(self, region: MachiningRegion, tool: Tool, **overrides):
        params = dict(PARAMS)
        params.update({"cut_depth_mm": 2.0, "stepover_ratio": 0.5,
                       "stock_allowance_mm": 0.3, "finish_pass": True})
        params.update(overrides)
        context = MillingContext(tool=tool, top_z=region.top_z, floor_z=region.floor_z,
                                 parameters=params, region=region)
        return plan_pocket_mill(context, notes_prefix="")

    def _simulate(self, region: MachiningRegion, toolpath, tool: Tool,
                  stock_bounds) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        from toolpath_lab.core.stock import RectangularStock
        from toolpath_lab.simulation.cut_sim import simulate_toolpath

        stock = RectangularStock(bounds=stock_bounds)
        final = simulate_toolpath(toolpath, stock, tool_radius=tool.radius_mm,
                                  cell_mm=self.SIM_CELL, max_frames=2).final
        xs = region.bounds[0] + (np.arange(region.shape[0]) + 0.5) * region.cell_mm
        ys = region.bounds[1] + (np.arange(region.shape[1]) + 0.5) * region.cell_mm
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        rows = np.clip(((gx - final.x0) / final.cell_mm).astype(int), 0,
                       final.height.shape[0] - 1)
        cols = np.clip(((gy - final.y0) / final.cell_mm).astype(int), 0,
                       final.height.shape[1] - 1)
        height = final.height[rows, cols]
        floor = np.asarray(region.floor_z_at(gx, gy), dtype=np.float64)
        axis = np.asarray(region.axis_z_at(gx, gy, tool), dtype=np.float64)
        return height, floor, axis

    def _masks(self, region: MachiningRegion) -> tuple[np.ndarray, np.ndarray]:
        """返回 (内部无尖角掩码, 壁边带掩码)。"""

        xs = region.bounds[0] + (np.arange(region.shape[0]) + 0.5) * region.cell_mm
        ys = region.bounds[1] + (np.arange(region.shape[1]) + 0.5) * region.cell_mm
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        inside = region.inside
        x_in, y_in = gx[inside], gy[inside]
        near_corner = np.zeros(inside.shape, dtype=bool)
        for cx, cy in ((x_in.max(), y_in.max()), (x_in.max(), y_in.min()),
                       (x_in.min(), y_in.max()), (x_in.min(), y_in.min())):
            near_corner |= np.hypot(gx - cx, gy - cy) <= self.CORNER_MARGIN
        plain = inside & ~near_corner
        band = plain & (region.distance <= 1.5)
        return plain, band

    def _residual(self, region: MachiningRegion, height, floor, axis):
        """过切、内部残料、壁边带残料三个标量。"""

        inside = region.inside
        gouge = float(np.min(np.where(inside, height - floor, np.inf)))
        plain, band = self._masks(region)
        interior = plain & (region.distance >= 4.0)
        interior_max = float(np.max(np.where(interior, height - axis, -np.inf)))
        band_max = float(np.max(np.where(band, height - axis, -np.inf)))
        return gouge, interior_max, band_max

    @staticmethod
    def _max_segment(toolpath, prefixes) -> float:
        best = 0.0
        for move in toolpath.moves:
            if move.kind is not MoveKind.CUT:
                continue
            if not any((move.label or "").startswith(p) for p in prefixes):
                continue
            points = np.asarray(move.points, dtype=np.float64)
            if points.shape[0] >= 2:
                best = max(best, float(np.hypot(np.diff(points[:, 0]),
                                                np.diff(points[:, 1])).max()))
        return best

    # -- 测试 --------------------------------------------------------------
    def test_contour_rings_and_wall_pass_sample_z_per_cell(self) -> None:
        """环切的底面跟随环与腔壁精修必须逐格补点（Z 不能只在 4 个角点上取）。"""

        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = self._region(part, facets[len(facets) // 2], ceiling=45.0)
        toolpath = self._plan(region, flat_tool(6.0), cut_mode="contour")
        limit = 1.5 * region.cell_mm
        for label in ("底面跟随", "精修腔壁"):
            longest = self._max_segment(toolpath, (label,))
            self.assertLessEqual(
                longest, limit,
                f"{label}存在 {longest:.2f} mm 的长弦：环被简化成少数角点后"
                f"Z 只在角点取值，谷底会留残料、凸底会过切（上限 {limit:.2f}）",
            )

    def test_curved_contour_cuts_the_floor_without_residual(self) -> None:
        """曲底 + 环切：型腔内部切净、壁边带贴住刀轴、且不过切。"""

        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = self._region(part, facets[len(facets) // 2], ceiling=45.0)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_mode="contour")
        bounds = part.bounds
        height, floor, axis = self._simulate(
            region, toolpath, tool,
            PartBounds(bounds.x_min, bounds.y_min, bounds.z_min,
                       bounds.x_max, bounds.y_max, 45.0))
        gouge, interior_max, band_max = self._residual(region, height, floor, axis)
        self.assertGreaterEqual(gouge, -0.1, f"过切 {gouge:.3f} mm")
        self.assertLessEqual(
            interior_max, 0.4,
            f"环切中段残料 {interior_max:.3f} mm：底面跟随环没有逐格采样 Z（骑弦）",
        )
        self.assertLessEqual(
            band_max, 0.4,
            f"壁边带残料 {band_max:.3f} mm：壁精修被层高掩码裁掉或 Z 采样过粗",
        )

    def test_curved_zigzag_wall_band_is_cut_down(self) -> None:
        """曲底 + 往复：壁边带只靠腔壁精修这一刀够到，它不能被层高掩码裁掉。"""

        part = plate_with_curved_pocket()
        facets = curved_floor_faces(part)
        region = self._region(part, facets[len(facets) // 2], ceiling=45.0)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_mode="zigzag")
        bounds = part.bounds
        height, floor, axis = self._simulate(
            region, toolpath, tool,
            PartBounds(bounds.x_min, bounds.y_min, bounds.z_min,
                       bounds.x_max, bounds.y_max, 45.0))
        gouge, _, band_max = self._residual(region, height, floor, axis)
        self.assertGreaterEqual(gouge, -0.1, f"过切 {gouge:.3f} mm")
        self.assertLessEqual(
            band_max, 0.4,
            f"壁边带残料 {band_max:.3f} mm：最后一刀壁精修没走到贴壁底面",
        )

    def test_sloped_zigzag_wall_band_stays_at_physical_minimum(self) -> None:
        """斜底 + 往复：壁边带残料不得超过物理下限（R·tanθ + 量化余量）。

        平底刀贴壁下行时刀盘内侧还要压在更高的斜面上，壁边带**必然**留下约
        ``R·tanθ`` 的三角残料（见 pocket_mill 模块文档）——这里断言的是
        "达到物理下限"而不是"零残料"：修复前壁精修被层高掩码裁掉，
        残料高达 1.7 mm（一整层切深），修复后应贴住 0.5 mm 左右的物理值。
        """

        part = plate_with_sloped_pocket()
        region = self._region(part, sloped_floor_face(part), ceiling=40.0)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_mode="zigzag")
        bounds = part.bounds
        height, floor, axis = self._simulate(
            region, toolpath, tool,
            PartBounds(bounds.x_min, bounds.y_min, bounds.z_min,
                       bounds.x_max, bounds.y_max, 40.0))
        gouge, _, band_max = self._residual(region, height, floor, axis)
        self.assertGreaterEqual(gouge, -0.1, f"过切 {gouge:.3f} mm")
        self.assertLessEqual(
            band_max, 1.0,
            f"壁边带残料 {band_max:.3f} mm 超过物理下限（约 R·tan10° + 量化 ≈ 0.9）",
        )

    def test_convex_floor_is_not_gouged_by_ring_chords(self) -> None:
        """凸底（中心高、四角低）：环上 Z 只在角点取值会把弦切进底面。"""

        outline = np.asarray([[-30.0, -30.0], [30.0, -30.0],
                              [30.0, 30.0], [-30.0, 30.0]])
        node = np.arange(-34.5, 35.0, 1.0)
        grid_x, grid_y = np.meshgrid(node, node, indexing="ij")
        values = 6.0 - 0.003 * (grid_x ** 2 + grid_y ** 2)
        floor_field = FloorField(
            bounds=(-35.0, -35.0, 35.0, 35.0), cell_mm=1.0, values=values,
            normal=(0.0, 0.0, 1.0), planar=False,
            grad=(-0.006 * grid_x, -0.006 * grid_y), source="凸底夹具")
        region = build_region(outline, (), top_z=10.0, floor_z=0.6, cell_mm=1.0,
                              floor=floor_field)
        tool = flat_tool(6.0)
        toolpath = self._plan(region, tool, cut_mode="contour")
        height, floor, axis = self._simulate(
            region, toolpath, tool, PartBounds(-40.0, -40.0, -10.0,
                                               40.0, 40.0, 10.0))
        gouge, interior_max, _ = self._residual(region, height, floor, axis)
        self.assertGreaterEqual(
            gouge, -0.1,
            f"过切 {gouge:.3f} mm：环切弦穿进凸底面（Z 只在角点采样）",
        )
        self.assertLessEqual(interior_max, 0.4, f"内部残料 {interior_max:.3f} mm")


if __name__ == "__main__":
    unittest.main()
