"""型腔铣（cavity / pocket milling）。

型腔铣的目标是把选中的腔体（含岛屿）按层切除到底面。走刀方式两种：

``contour``（默认，环切 contour-parallel）
    按步距做一层层等距环，从腔壁向中心收敛——刀具始终贴着已加工面走，负荷均匀，
    是 NX "Cavity Mill / Follow Periphery" 的做法。
``zigzag`` / ``one_way``
    每层做平行扫描线，行程短、空刀少，适合开阔的型腔。

区域运算（外轮廓向内偏置、岛屿向外偏置并自动合并）由 :mod:`toolpath_lab.cam.boundary`
的距离场统一处理，因此"腔里有几个岛、形状多复杂"都不需要在刀路代码里特判。

**底面可以是水平面、斜面或曲面。** 水平底面就是"这一层切到 Z"；底面一旦倾斜或弯曲，
同一层刀路在不同位置的高度不同，刀轴必须逐点按底面高度 + 防过切抬升来算
（:mod:`toolpath_lab.cam.tool_engagement`）：

* 每一层的每个刀位点取 ``Z = max(层高, 该点底面高度 + 抬升)``；
* 于是最底下几层自然就"贴"到底面上，不需要为斜面/曲面单独写一条刀路；
* 抬升只与底面坡度和刀具形状有关：平底刀 ≈ ``r·tanθ``、球头刀 ≈ 一半，
  水平面时为 0（与引入高度场之前完全一致）。

已知的几何限制：平底刀在**斜面与竖直侧壁的交角**处会留下一小块三角残料。
这不是算法问题，而是平底刀本身够不到那个角（球头/圆鼻刀才能贴进去），
刀路备注里会明确写出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.cam.boundary import MachiningRegion, offset_outline_polygons
from toolpath_lab.cam.common import (LevelCoverage, MillingContext, MoveBuilder,
                                     depth_levels, in_level_transfer, level_key,
                                     stepped_levels)
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.path import Toolpath

#: 环切时两层之间的抬刀方式：True 时抬到安全面再定位（更安全，空刀多）。
SAFE_LINK_BETWEEN_RINGS = False


@dataclass(slots=True)
class _PocketJob:
    """一个加工面（区域）的型腔铣规划现场。

    除层列表外的全部准备工作都在这里：走刀方式、偏置、步距、加工底与斜/曲面修正。
    单面与多区域调度共用同一份现场，保证"区域的一层"在两条路径上语义一致。
    """

    context: MillingContext
    region: MachiningRegion
    prefix: str
    mode: str
    base_offset: float
    stepover: float
    flat_floor: bool
    floor_target: float
    step: float
    levels: list[float] = field(default_factory=list)
    ring_count: int = 0
    skipped_layers: int = 0


def _prepare_pocket(context: MillingContext, notes_prefix: str) -> _PocketJob:
    """层循环之外的全部准备（含斜/曲面底面的加工底修正）。"""

    region = context.region
    if region is None:
        raise PlanningError(f"{notes_prefix}型腔铣缺少加工区域")

    mode = str(context.parameters.get("cut_mode", "contour"))
    if mode not in {"contour", "zigzag", "one_way"}:
        mode = "contour"
    base_offset = context.tool_radius + context.stock_allowance
    # 步距夹到刀具直径内，避免两条刀轨之间残留毛坯（详见 effective_stepover）
    stepover = context.effective_stepover
    flat_floor = region.is_flat_floor
    # 刀心可行区域（开粗第一环所在的那圈等距区）。斜/曲面底面的"最低可切高度"必须在
    # 它上面取：刀心走不到的位置，分多少层都没用。
    feasible = region.offset_mask(base_offset)

    # -- 分层范围 ----------------------------------------------------------
    # 水平底面：切到 floor_z 就是老行为，一字不改。
    # 斜/曲面底面：刀尖要贴住底面还得再抬``required_lift``，真正的加工底是
    # ``区域内 min(底面高度 + 抬升)``。按 floor_z 分层，最底下几层永远"区域为空"
    # 被整层跳过 —— 底面留下整整一层（甚至几毫米）残料，实测就是这样。
    floor_target = float(region.floor_z) + context.finish_allowance
    step = float(context.cut_depth)
    if not flat_floor:
        deepest = region.deepest_axis_z(context.tool, feasible)
        if np.isfinite(deepest):
            floor_target = float(deepest) + context.finish_allowance
        # 第一层也不能落到区域内最高的底面之下，否则那一带材料只能留给"底面跟随"刀路，
        # 一刀切太深。留出余量后仍按不超过每层切深来分。
        highest = region.highest_axis_z(context.tool, feasible)
        headroom = float(region.top_z) - float(highest)
        step = float(min(step, max(headroom, 0.2 * context.cut_depth)))
    return _PocketJob(context=context, region=region, prefix=notes_prefix, mode=mode,
                      base_offset=base_offset, stepover=stepover, flat_floor=flat_floor,
                      floor_target=floor_target, step=step)


def _cut_pocket_level(builder: MoveBuilder, job: _PocketJob, level_index: int,
                      target_z: float, previous_z: float, first_cut: bool, *,
                      exclude_mask: NDArray[np.bool_] | None = None) -> bool:
    """切这个区域的某一层（环切 / 平行扫描 + 每层壁精修）。

    单面与多区域调度共用这一份实现；返回值是新的 ``first_cut``
    （第一个切削段落地后变 False，后续段才能按距离判断抬刀还是直连）。
    ``exclude_mask`` 是同层调度里**已被先加工区域切过**的格子（见
    :class:`~toolpath_lab.cam.common.LevelCoverage`）：从本层可切掩码里去掉，
    整层被覆盖就整层静默跳过——同一层只切一遍。
    """

    region = job.region
    context = job.context
    depth = previous_z - target_z
    if depth <= 1e-9:
        return first_cut
    # 这一层"还能切"的区域：斜面/曲面时层高落到某处底面之下，那一块必须裁掉，
    # 否则刀会平着切过去把高处的底面切掉。水平底面时它与 inside 等价。
    level_region = region.level_mask(target_z, context.tool)
    if exclude_mask is not None:
        level_region = level_region & ~exclude_mask
        if not level_region.any():
            # 整层已被同层先加工的区域覆盖：去重跳过，属预期行为，不报警。
            return first_cut
    if job.mode == "contour":
        rings, last_offset = _contour_rings(region, job.base_offset, job.stepover,
                                            mask=level_region)
        if not rings:
            # 斜/曲面底面：靠近底部时"层高仍高于底面 + 抬升"的区域会自然收缩到没有
            # 环，这是预期的（那部分由最后一道"底面跟随"刀路切出），不再逐层报警。
            if job.flat_floor:
                context.warn(
                    f"第 {level_index + 1} 层：区域在偏置 {job.base_offset:g} mm 后为空，已跳过"
                )
            else:
                job.skipped_layers += 1
            return first_cut
        for ring_index, polygon in enumerate(rings):
            job.ring_count += 1
            points = _ring_points(region, context, polygon, target_z, close=True)
            _emit_ring(builder, context, points, first_cut, level_index, ring_index,
                       region=region, level_mask=level_region,
                       label_prefix=job.prefix)
            first_cut = False
        # BUG-025 修：最后一环到区域最深点的距离若超过刀半径，中心会剩一块
        # 谁都切不到的"内岛"（中心处的小等距轮廓已被 min_area 过滤）。
        # 在最深点补一个小清理环，把内岛吃掉。
        max_distance = float(region.distance.max()) if region.distance.size else 0.0
        if max_distance - last_offset > context.tool_radius + 1e-6:
            cleanup = _cleanup_loop(region, target_z, context, mask=level_region)
            if cleanup is not None:
                job.ring_count += 1
                _emit_ring(builder, context, cleanup, first_cut,
                           level_index, len(rings), region=region,
                           level_mask=level_region, label_suffix="（中心清理）",
                           label_prefix=job.prefix)
                first_cut = False
    else:
        mask = region.offset_mask(job.base_offset)
        if level_region is not None:
            mask = mask & level_region
        if not mask.any():
            if job.flat_floor:
                context.warn(f"第 {level_index + 1} 层：偏置后区域为空，已跳过")
            else:
                job.skipped_layers += 1
            return first_cut
        angle = float(context.parameters.get("direction_deg", 0.0))
        # 粗加工层的扫描线**不加密**：层高裁剪保证每条扫描线都落在"刀轴仍高于该处
        # 底面"的格子上，取端点即等于取层高；贴住底面的那一道由 _emit_floor_follow
        # 单独加密（否则每层几万个点，刀路文件白白膨胀）。
        passes = _zigzag_passes(region, mask, angle, job.stepover)
        # BUG-009 修：先前 one_way / zigzag 两个分支效果相同（都翻折），
        # 单向走刀失效。单向走刀的语义是"每刀抬刀 → 同向落刀"，不翻折；
        # 往复（zigzag）才是"奇数刀翻折 + 邻刀相连"。
        force_safe_link = (job.mode == "one_way")
        for pass_index, world_pass in enumerate(passes):
            if job.mode == "zigzag" and pass_index % 2 == 1:
                world_pass = world_pass[::-1]
            points = _ring_points(region, context, world_pass, target_z)
            _emit_ring(builder, context, points, first_cut, level_index, pass_index,
                       region=region, level_mask=level_region,
                       force_safe_link=force_safe_link, label_prefix=job.prefix)
            first_cut = False

    # BUG-005 修：先前精修用 base_offset (= R + stock_allowance) 偏置——
    # 与开粗第一环原样重走，既没切掉余量又多一圈空程。改成"按刀半径 R 偏置"
    # 开粗按 R+allowance、精修按 R，侧面余量恰好被精修这一刀吃掉。
    # 不传层高掩码：斜/曲面时它的 Z=max(层高, 刀轴) 自己会防过切，
    # 裁掩码只会把贴壁底面从最终深度里裁掉（壁边留料，详见 _emit_wall_finish）。
    # 但**几何障碍**要裁：贴壁环不许贴着凸台/悬臂走（与层高无关，只看谁挡着刀）。
    if bool(context.parameters.get("finish_pass", True)) \
            and region.offset_area_mm2(context.tool_radius) > 0.0:
        _emit_wall_finish(builder, context, region, context.tool_radius, target_z,
                          mask=region.obstacle_mask(target_z, context.tool),
                          level_mask=level_region, label_prefix=job.prefix)
    return first_cut


def _follow_floor(builder: MoveBuilder, job: _PocketJob, first_cut: bool) -> bool:
    """斜/曲面底面的"底面跟随"收尾（水平底面是空操作），返回新的 ``first_cut``。"""

    if job.flat_floor:
        return first_cut
    count = _emit_floor_follow(builder, job.context, job.region, job.base_offset,
                               job.stepover, job.floor_target, first_cut,
                               label_prefix=job.prefix)
    job.ring_count += count
    return first_cut and count == 0


def plan_pocket_mill(context: MillingContext, *, notes_prefix: str = "") -> Toolpath:
    """生成型腔铣刀路（水平底面、斜面底面、曲面底面共用这一条）。"""

    job = _prepare_pocket(context, notes_prefix)
    region = job.region
    job.levels = depth_levels(region.top_z, job.floor_target, max(job.step, 1e-6))
    if not job.levels:
        raise PlanningError(
            f"{notes_prefix}型腔铣没有可切除的深度：区域顶面 {region.top_z:g} mm，"
            f"加工底面 {job.floor_target:g} mm。请检查毛坯 Z 向余量、底面余量，"
            "或换一个更低的加工面"
        )
    builder = MoveBuilder(context)
    first_cut = True

    for level_index, target_z in enumerate(job.levels):
        previous_z = float(region.top_z) if level_index == 0 else float(job.levels[level_index - 1])
        first_cut = _cut_pocket_level(builder, job, level_index, target_z, previous_z, first_cut)

    # -- 底面跟随 ----------------------------------------------------------
    # 斜/曲面底面：Z 层粗加工只在"层高仍高于底面 + 抬升"的地方下刀，层与层之间必然留下
    # 台阶，最深处也够不到（那里刀轴必须抬高才能不扎进上坡一侧）。这一道沿整片可切区域
    # 走一圈圈等距环、Z **逐点**取刀轴不过切高度，把台阶一次切净、底面切到实处。
    _follow_floor(builder, job, first_cut)

    notes = _notes(context, region, job.mode, job.stepover, job.ring_count, len(job.levels),
                   notes_prefix, job.floor_target, job.skipped_layers)
    return builder.finish(planner="pocket_mill", label="型腔铣", notes=notes)


def plan_pocket_mill_multi(items: Sequence[tuple[MillingContext, str]], *,
                           order: str = "level_first") -> Toolpath:
    """多加工面（多区域）的统一层调度（UG/NX 的层优先 / 深度优先）。

    所有区域共用**同一套层高网格**（:func:`~toolpath_lab.cam.common.stepped_levels`），
    同一高度的层在区域间天然对齐：

    * ``level_first``（层优先）——按层下降，每一层把各区域在该高度的加工依次做完，
      即"同一高度能合并加工就合并"；
    * ``depth_first``（深度优先）——一个区域从上到下切完，再换下一个区域。

    两种顺序只是遍历顺序不同：层高与每层刀路完全一致（区域顺序仍按选面顺序）。
    斜/曲面底面的"底面跟随"跟随区域：深度优先时该区域切完立即收尾，层优先时
    等全部层下完再逐区域收尾。

    同一高度上区域互相重叠时（顶面内环并入可切区域后，顶面与它下方的型腔底
    在共享层就是两块重叠区域），**谁先调度谁先覆盖**，后切的区域让出重叠部分，
    同一层只切一遍（见 :class:`~toolpath_lab.cam.common.LevelCoverage`）。
    """

    jobs = [_prepare_pocket(context, prefix) for context, prefix in items]
    global_top = max(float(job.region.top_z) for job in jobs)
    # 斜/曲面底面时各区域的 step 可能被 headroom 压小，取最小值保守共用。
    step = min(job.step for job in jobs)
    layers = stepped_levels(global_top, [job.floor_target for job in jobs], step)
    for job in jobs:
        top = float(job.region.top_z)
        # 下界用归一后的键比较：floor_target 带 ±1e-7 的网格面拟合噪声，而 layers
        # 已按 1 nm 归一（40.00000015 → 40.0），裸比较会把末层滤掉导致漏切。
        floor_key = level_key(job.floor_target)
        job.levels = [z for z in layers if floor_key - 1e-9 <= z <= top - 1e-9]
        if not job.levels:
            raise PlanningError(
                f"{job.prefix}型腔铣没有可切除的深度：区域顶面 {top:g} mm，"
                f"加工底面 {job.floor_target:g} mm。请检查毛坯 Z 向余量、底面余量，"
                "或换一个更低的加工面"
            )

    # 共享一个 MoveBuilder：跨面转移由 rapid_to_safe / plunge / link 自动接住。
    # 调度现场取全局最高顶面，安全高度统一抬到所有区域之上。
    lead = jobs[0].context
    dispatch = MillingContext(
        tool=lead.tool,
        top_z=global_top,
        floor_z=min(float(job.region.floor_z) for job in jobs),
        parameters=lead.parameters,
        region=None,
    )
    builder = MoveBuilder(dispatch)
    first_cut = True

    def previous_z_of(job: _PocketJob, level_index: int) -> float:
        return float(job.region.top_z) if level_index == 0 \
            else float(job.levels[level_index - 1])

    # 同层去重：先切的区域登记"这层切过哪里"，后切的区域把重叠部分让出去
    # （顶面内环并入可切区域后，顶面与它下方的型腔底在共享层会切同一块 XY）。
    coverage = LevelCoverage()
    dedup_count = 0

    def cut_level(job: _PocketJob, level_index: int, target_z: float) -> None:
        nonlocal first_cut, dedup_count
        exclude = coverage.exclude(job.region, target_z)
        natural = LevelCoverage.cut_mask(job.region, target_z, job.context.tool,
                                         job.base_offset)
        if exclude is not None and natural is not None \
                and bool((natural & exclude).any()):
            dedup_count += 1
        first_cut = _cut_pocket_level(builder, job, level_index, target_z,
                                      previous_z_of(job, level_index), first_cut,
                                      exclude_mask=exclude)
        coverage.record(job.region, target_z, natural)

    if str(order) == "depth_first":
        for job in jobs:
            for level_index, target_z in enumerate(job.levels):
                cut_level(job, level_index, target_z)
            first_cut = _follow_floor(builder, job, first_cut)
    else:
        # 未知取值一律按层优先（参数校验在上游，这里是防御性回退，与 cut_mode 同风格）。
        index_of = [{z: index for index, z in enumerate(job.levels)} for job in jobs]
        all_levels = sorted({z for job in jobs for z in job.levels}, reverse=True)
        for target_z in all_levels:
            for job, mapping in zip(jobs, index_of):
                level_index = mapping.get(target_z)
                if level_index is None:
                    continue
                cut_level(job, level_index, target_z)
        for job in jobs:
            first_cut = _follow_floor(builder, job, first_cut)

    notes: list[str] = []
    for job in jobs:
        notes.extend(_notes(job.context, job.region, job.mode, job.stepover, job.ring_count,
                            len(job.levels), job.prefix, job.floor_target, job.skipped_layers))
    if dedup_count:
        notes.append(
            f"同层去重：{dedup_count} 个（加工面×层）与同层先加工的区域重叠，"
            "重叠部分已切过，不再重复切削"
        )
    if str(order) == "depth_first":
        notes.append(
            f"切削顺序：深度优先——单个区域从上到下切完再换下一个（共 {len(jobs)} 个加工面）"
        )
    else:
        notes.append(
            f"切削顺序：层优先——各区域在同一高度合并、逐层下切（共 {len(jobs)} 个加工面）"
        )
    return builder.finish(planner="pocket_mill", label="型腔铣", notes=notes)


def _notes(context: MillingContext, region: MachiningRegion, mode: str, stepover: float,
           ring_count: int, level_count: int, notes_prefix: str, floor_target: float,
           skipped_layers: int = 0) -> list[str]:
    mode_label = "环切" if mode == "contour" else "平行扫描"
    notes = [
        f"{notes_prefix}型腔铣（{mode_label}）：{level_count} 层，"
        f"每层切深 ≤ {context.cut_depth:g} mm，步距 {stepover:g} mm，共 {ring_count} 条刀轨",
        f"刀具 D{context.tool.diameter_mm:g} mm；径向余量 {context.stock_allowance:g} mm，"
        f"底面余量 {context.finish_allowance:g} mm；岛屿自动避让",
    ]
    if not region.is_flat_floor:
        notes.append(
            f"底面为斜面/曲面（最大坡度 {region.floor_slope_deg:.1f}°，"
            f"底面最低 {region.floor_z:g} mm、最高 {region.floor.z_max:g} mm）："
            f"刀轴 Z 逐点按底面高度 + 防过切抬升计算，加工底（刀轴最低）{floor_target:g} mm"
        )
        notes.append(
            "斜面/曲面底面在分层粗加工之后另走一道「底面跟随」刀路：Z 逐点贴住底面，"
            "把层间台阶切净"
        )
        if skipped_layers:
            notes.append(
                f"有 {skipped_layers} 个粗加工层在层高裁剪后为空（底面较陡处），"
                "这些位置由底面跟随刀路切出"
            )
        notes.extend(region.floor.notes or [])
    return notes


# ------------------------------------------------------------------ 内部
def _ring_points(region: MachiningRegion, context: MillingContext,
                 planar: NDArray[np.float64], level_z: float, *,
                 close: bool = False) -> NDArray[np.float64]:
    """把 XY 刀轨抬到"这一层该在的高度"：``Z = max(层高, 不过切的刀轴高度)``。

    水平底面时刀轴高度就是底面常数，于是取到的一直是层高（与引入高度场之前一字不差）；
    斜面/曲面时刀轴高度取自 :meth:`MachiningRegion.axis_z_at`（底面高度 + 防过切抬升，
    栅格上取周边四格最大值），因此最底下几层会自然贴住底面、而且**不会扎进底面**。

    ``close=True`` 用于环切：``offset_outline_polygons`` 返回的是**开放**多边形
    （首尾不重复），刀轨必须自己闭合 —— 少了这一段，每一圈的最后一条边都切不到，
    而且"上一圈的终点"与"下一圈的起点"会相距一整条边长，连接方式被误判成抬刀转移。
    """

    points = np.asarray(planar, dtype=np.float64).reshape(-1, 2)
    if points.shape[0] == 0:
        return np.zeros((0, 3), dtype=np.float64)
    if close and points.shape[0] >= 3:
        points = np.vstack([points, points[:1]])
    axis = np.asarray(region.axis_z_at(points[:, 0], points[:, 1], context.tool),
                      dtype=np.float64).reshape(-1)
    z = np.maximum(np.full(points.shape[0], float(level_z)), axis)
    return np.column_stack((points, z))


def _densify_planar(points: NDArray[np.float64], step: float, *, close: bool = False
                    ) -> NDArray[np.float64]:
    """在折线/多边形的每条边内按 ``step`` 补插中间点。

    为什么必须补：``offset_outline_polygons`` 会把共线点简化掉（矩形型腔的等距环
    最后只剩 4 个角点），而 Z 是**逐点**算的——不补点，整条边就悬在两个角点之间
    "骑弦"。底面谷形时弦高于底面，边上留下最高几毫米的残料；底面凸起时弦穿到底面
    之下，直接过切。:func:`_subdivide` 在平行扫描分支解决的正是同一个问题，
    环切与腔壁精修分支一直漏了（实测曲底环切中段残料 0.9~4 mm，而往复干净）。
    """

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if pts.shape[0] < 2 or not np.isfinite(step) or step <= 1e-9:
        return pts
    out: list[NDArray[np.float64]] = []
    edge_count = pts.shape[0] if close else pts.shape[0] - 1
    for index in range(edge_count):
        a = pts[index]
        b = pts[(index + 1) % pts.shape[0]]
        out.append(a)
        length = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        pieces = int(np.ceil(length / step)) if step > 1e-9 else 1
        if pieces > 1:
            ratio = np.arange(1, pieces, dtype=np.float64) / pieces
            out.append(a + ratio[:, None] * (b - a))
    if not close:
        out.append(pts[-1])
    return np.vstack(out)


def _subdivide(pass_xy: NDArray[np.float64], step: float) -> NDArray[np.float64]:
    """把一条扫描线按 ``step`` 加密采样点。

    斜/曲面底面上一条扫描线两端高、中间低（或反过来），只取两个端点定 Z 会在中间段
    过切。加密到栅格尺度后，逐点取不过切高度即可贴住底面。
    """

    points = np.asarray(pass_xy, dtype=np.float64).reshape(-1, 2)
    if points.shape[0] < 2 or step <= 1e-9:
        return points
    length = float(np.linalg.norm(points[-1] - points[0]))
    count = int(np.ceil(length / step)) + 1
    if count <= 2:
        return points
    ratio = np.linspace(0.0, 1.0, count)[:, None]
    return points[0] + ratio * (points[-1] - points[0])


def _contour_rings(region: MachiningRegion, base_offset: float, stepover: float,
                   mask: NDArray[np.bool_] | None = None
                   ) -> tuple[list[NDArray[np.float64]], float]:
    """由外向内逐圈取等距轮廓，直到区域中心。

    返回 ``(rings, last_offset)``：``last_offset`` 是最后一圈所在的等距层级，
    调用方用它判断"最深点是否已被最后一圈 + 刀半径覆盖"（BUG-025：环切在
    型腔中心留残料——最后一环到最深点的距离可以超过刀半径，而中心处的小
    等距轮廓又被 ``min_area_mm2`` 过滤，谁都不补这一刀）。

    ``mask`` 是这一层"还能切"的区域（斜面/曲面时层高以上的部分被裁掉）。
    """

    rings: list[NDArray[np.float64]] = []
    offset = base_offset
    active = region.inside if mask is None else mask
    if not active.any():
        return rings, -1.0
    max_distance = float(region.distance[active].max()) if region.distance.size else 0.0
    last_offset = -1.0
    guard = 0
    while offset <= max_distance + 1e-9 and guard < 4096:
        guard += 1
        polygons = offset_outline_polygons(region, offset, min_area_mm2=1.0, mask=active)
        if not polygons:
            break
        rings.extend(polygons)
        last_offset = offset
        # 间距下限取半格（等距轮廓的分辨极限），不再取整格：
        # 整格下限会在"粗栅格 + 小刀具"时间距超过刀直径，两环之间留下残料。
        offset += max(stepover, 0.5 * region.cell_mm)
    return rings, last_offset


def _zigzag_passes(region: MachiningRegion, mask: NDArray[np.bool_], angle: float,
                   stepover: float, *, subdivide: bool = False
                   ) -> list[NDArray[np.float64]]:
    """平行扫描线，返回每条刀线的世界坐标点列（端点或加密后的折线）。"""

    angle_rad = np.radians(angle)
    u_axis = np.array([np.cos(angle_rad), np.sin(angle_rad)], dtype=np.float64)
    v_axis = np.array([-np.sin(angle_rad), np.cos(angle_rad)], dtype=np.float64)
    levels = region.scanline_levels(angle, stepover, mask)
    passes: list[NDArray[np.float64]] = []
    for level in levels:
        for interval in region.scanline_intervals(float(level), angle, mask):
            start = interval[0] * u_axis + interval[2] * v_axis
            end = interval[1] * u_axis + interval[2] * v_axis
            line = np.array([[start[0], start[1]], [end[0], end[1]]], dtype=np.float64)
            passes.append(_subdivide(line, region.cell_mm) if subdivide else line)
    return passes


def _emit_ring(builder: MoveBuilder, context: MillingContext, points: NDArray[np.float64],
               first_cut: bool, level_index: int, ring_index: int, *,
               region: MachiningRegion | None = None,
               level_mask: NDArray[np.bool_] | None = None,
               force_safe_link: bool = False, label_suffix: str = "",
               label_prefix: str = "") -> None:
    """走一条闭合环（或一段扫描线）。

    环间转移**优先在层内平移**：一次下刀后把整层连贯切完（z 不同时补一段
    斜降连接），只有出界（穿岛、跨两个型腔之间的实体、贴凹角）或 ``one_way``
    强制抬刀时才回退到"抬到安全面 + 下刀"。
    """

    label = f"{label_prefix}第 {level_index + 1} 层 第 {ring_index + 1} 条刀轨{label_suffix}"
    previous = builder.last_point
    if previous is None or first_cut:
        builder.rapid_to_safe(points[0], label="定位到下刀点")
        builder.plunge(points[0], label=f"下刀 Z{points[0][2]:.3f}")
        builder.cut(points, label=label)
        return
    gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
    z_gap = abs(float(previous[2]) - float(points[0][2]))
    transfer_ok = (not force_safe_link and not SAFE_LINK_BETWEEN_RINGS
                   and region is not None
                   and in_level_transfer(region, context.tool_radius,
                                         previous[:2], points[0][:2],
                                         level_mask=level_mask))
    if not transfer_ok:
        builder.rapid_to_safe(points[0], label="环间转移")
        builder.plunge(points[0])
    elif gap > 1e-9 or z_gap > 1e-9:
        builder.link(np.vstack([previous, points[0]]), label="层内转移")
    builder.cut(points, label=label)


def _emit_floor_follow(builder: MoveBuilder, context: MillingContext,
                       region: MachiningRegion, base_offset: float, stepover: float,
                       floor_target: float, first_cut: bool, *,
                       label_prefix: str = "") -> int:
    """斜/曲面底面的"底面跟随"刀路：整片可切区域按底面实际高度走一遍。

    分层粗加工只能切到"层高仍高于底面 + 抬升"的地方，层间台阶与最深的一带都留给这一道：
    环切时沿等距环走、平行扫描时沿扫描线走，Z **逐点**取 ``max(加工底, 刀轴不过切高度)``。
    最深点再补一个小环（否则平底刀在最深处那一小块永远够不到）。
    """

    count = 0
    mode = str(context.parameters.get("cut_mode", "contour"))
    # 底面跟随按 floor_target 这一刀的障碍裁剪：区域可能已并入内环，
    # 不裁就会贴着凸台顶把底面高度走过去（直接过切）。与层高掩码无关——
    # 这里刻意不裁"刀轴高于本层"的贴壁带，只裁谁挡着刀。
    allowed = region.obstacle_mask(floor_target, context.tool)
    shapes: list[tuple[NDArray[np.float64], bool]] = []
    if mode == "contour":
        rings, _ = _contour_rings(region, base_offset, stepover, mask=allowed)
        # 环上的点要加密到栅格尺度再算 Z（详见 _densify_planar）：
        # 矩形腔的环被 _simplify 成 4 个角点，只在角点取 Z 会整条边骑弦，
        # 谷底残料、凸底过切都是这一下漏出来的。
        dense = float(region.cell_mm)
        shapes = [(_densify_planar(ring, dense, close=True), True) for ring in rings]
    else:
        angle = float(context.parameters.get("direction_deg", 0.0))
        scan_mask = region.offset_mask(base_offset)
        if allowed is not None:
            scan_mask = scan_mask & allowed
        passes = _zigzag_passes(region, scan_mask, angle, stepover,
                                subdivide=True)
        shapes = [(line, False) for line in passes]
    cleanup = _cleanup_loop(region, floor_target, context, mask=allowed)
    if cleanup is not None:
        shapes.append((cleanup[:, :2], True))
    for index, (shape, close) in enumerate(shapes):
        points = _ring_points(region, context, shape, floor_target, close=close)
        if points.shape[0] < 2:
            continue
        _emit_ring(builder, context, points, first_cut, 0, index,
                   region=region, level_mask=allowed,
                   label_prefix=f"{label_prefix}底面跟随 ",
                   label_suffix="（斜面/曲面底面）")
        first_cut = False
        count += 1
    return count


def _cleanup_loop(region: MachiningRegion, target_z: float, context: MillingContext,
                  mask: NDArray[np.bool_] | None = None) -> NDArray[np.float64] | None:
    """在区域最深点构造一个小清理环（BUG-025：环切中心残料）。

    内岛一定在最后一环 + 刀半径之外、且距离最深点不超过一个步距，
    因此以最深点为圆心走一个半格小圆即可全部吃掉。斜/曲面底面上这一环的 Z 会取到
    该处"不过切的刀轴高度"，于是最深处也真的被切到。
    """

    if not region.distance.size:
        return None
    active = region.inside if mask is None else mask
    if not active.any():
        return None
    field = np.where(active, region.distance, -1.0)
    di, dj = np.unravel_index(int(np.argmax(field)), field.shape)
    if field[di, dj] < 0.0:
        return None
    center_x = region.bounds[0] + (di + 0.5) * region.cell_mm
    center_y = region.bounds[1] + (dj + 0.5) * region.cell_mm
    radius = max(0.5 * region.cell_mm, 0.2)
    angles = np.linspace(0.0, 2.0 * np.pi, 24, endpoint=True)
    loop = np.column_stack((center_x + radius * np.cos(angles),
                            center_y + radius * np.sin(angles)))
    return _ring_points(region, context, loop, target_z)


def _emit_wall_finish(builder: MoveBuilder, context: MillingContext,
                      region: MachiningRegion, offset: float, target_z: float, *,
                      mask: NDArray[np.bool_] | None = None,
                      level_mask: NDArray[np.bool_] | None = None,
                      label_prefix: str = "") -> None:
    """沿腔壁补一刀精修（把侧面余量留到这一刀）。

    斜/曲面底面上这一刀同时也是"贴壁的底面跟随"：Z 取 ``max(层高, 刀轴高度)``，
    逐点贴住底面，所以环上的点必须先加密（见 :func:`_densify_planar`）。
    调用方**不要**再用层高掩码裁这一圈——裁掉的正是"刀轴高于本层"的贴壁带，
    裁完最深层就再也够不到壁边底面，实测曲底壁边残留整整一层切深的料。

    本层环切已经把层面切平，去壁起点的平移必然在层内完成（不再抬到安全面
    插下来）；只有直线出界（穿岛/凹角）时才回退到抬刀定位。
    """

    polygons = offset_outline_polygons(region, offset, mask=mask)
    previous = builder.last_point
    for polygon in polygons:
        if polygon.shape[0] < 3:
            continue
        loop = np.vstack([polygon, polygon[:1]])
        if not region.is_flat_floor:
            loop = _densify_planar(loop, region.cell_mm)
        points = _ring_points(region, context, loop, target_z)
        if previous is not None and in_level_transfer(
                region, context.tool_radius, previous[:2], points[0][:2],
                level_mask=level_mask):
            gap = float(np.linalg.norm(previous[:2] - points[0][:2]))
            z_gap = abs(float(previous[2]) - float(points[0][2]))
            if gap > 1e-9 or z_gap > 1e-9:
                builder.link(np.vstack([previous, points[0]]),
                             label=f"{label_prefix}层内转移")
        else:
            builder.rapid_to_safe(points[0], label=f"{label_prefix}定位到腔壁起点")
            builder.plunge(points[0], label=f"{label_prefix}下刀（壁精修）")
        builder.cut(points, label=f"{label_prefix}精修腔壁")
        previous = builder.last_point


__all__ = ["plan_pocket_mill"]
