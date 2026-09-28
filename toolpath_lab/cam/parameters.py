"""CAM 加工参数：一份声明驱动界面控件、接口校验与 NC 头部注释。

参数按"机床/刀具/切削"三组排列，与 NX、Mastercam 的参数页习惯一致：

============  ==========================================================
组            参数
============  ==========================================================
刀具          tool_id（刀具库选中的刀具）、tool_kind、tool_diameter_mm、
              tool_length_mm、tool_flute_mm
切削          spindle_rpm、feed_mm_per_min、plunge_feed_mm_per_min、
              stepover_ratio、cut_depth_mm、stock_allowance_mm、
              finish_allowance_mm、stepdown_mm（仅型腔铣）
安全          safe_height_mm、clearance_mm、rapid_feed_mm_per_min、
              spindle_direction、coolant
============  ==========================================================

**刀具的来源只有一处**：刀具库（:mod:`toolpath_lab.storage.tool_library`）。
选了库里的刀，它的几何就写进 ``tool_kind`` / ``tool_diameter_mm`` /
``tool_length_mm`` / ``tool_flute_mm`` 这几个键（见
:func:`toolpath_lab.core.tool.tool_geometry_values`），于是刀路、仿真与 NC
三条路读到的都是同一把刀，不需要各自再去查库。没有选刀（``tool_id`` 为空）
时这几个键就是手填的数值，行为与引入刀具库之前完全一致。

"固定设置"（``CAM_FIXED``）是界面只展示不编辑的量，与原有基座的做法一致。
"""

from __future__ import annotations

from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.tool import Tool, ToolKind

#: NC 程序里写死的量（界面只展示）。
CAM_FIXED: dict[str, Any] = {
    "units": "mm",
    "plane": "G17",
    "absolute": "G90",
    "program_end": "M30",
    "arc_tolerance_mm": 0.01,
    "max_stepdown_ratio": 0.9,
}

#: 主轴转向；与常见机床的 M 代码一致。
SPINDLE_DIRECTIONS: tuple[Choice, ...] = (
    Choice("cw", "正转 M03"),
    Choice("ccw", "反转 M04"),
)

#: 冷却方式。
COOLANTS: tuple[Choice, ...] = (
    Choice("flood", "乳化液 M08"),
    Choice("mist", "气雾 M07"),
    Choice("off", "关闭 M09"),
)


def tool_parameters() -> ParameterSet:
    """刀具相关参数（与原有基座保持同样的键名习惯）。"""

    return ParameterSet(
        (
            spec("tool_id", "刀具库刀具", K.STRING, "", group="刀具",
                 help="刀具库里选中的刀具 id；为空表示不引用刀具库，按下面的数值手工设定"),
            spec("tool_diameter_mm", "刀具直径 D", K.FLOAT, 10.0, minimum=0.5,
                 maximum=200.0, step=0.5, unit="mm", group="刀具"),
            spec("tool_length_mm", "刀具长度 L", K.FLOAT, 40.0, minimum=2.0,
                 maximum=400.0, step=1.0, unit="mm", group="刀具"),
            spec("tool_flute_mm", "刃长", K.FLOAT, 25.0, minimum=1.0, maximum=200.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示与碰撞参考"),
        )
    )


def cutting_parameters() -> ParameterSet:
    """切削参数。"""

    return ParameterSet(
        (
            spec("spindle_rpm", "主轴转速 S", K.FLOAT, 3000.0, minimum=100.0,
                 maximum=30000.0, step=100.0, unit="r/min", group="切削"),
            spec("feed_mm_per_min", "进给速度 F", K.FLOAT, 900.0, minimum=10.0,
                 maximum=20000.0, step=50.0, unit="mm/min", group="切削",
                 help="切削进给；下刀与快移另有各自的速度"),
            spec("plunge_feed_mm_per_min", "下刀进给", K.FLOAT, 300.0, minimum=5.0,
                 maximum=10000.0, step=25.0, unit="mm/min", group="切削"),
            spec("stepover_ratio", "步距 ae", K.FLOAT, 0.5, minimum=0.05,
                 maximum=0.95, step=0.05, unit="D %", group="切削",
                 help="相邻两条刀轨的间距占刀具直径的比例（UG NX 风格）；"
                 "0.5 = 直径的 50%，换刀具时自动跟随"),
            spec("cut_depth_mm", "每层切深 ap", K.FLOAT, 2.0, minimum=0.05,
                 maximum=200.0, step=0.5, unit="mm", group="切削",
                 help="单层切除的深度；总深度由所选面的高度差决定"),
            spec("stock_allowance_mm", "侧面余量", K.FLOAT, 0.3, minimum=0.0,
                 maximum=20.0, step=0.1, unit="mm", group="切削",
                 help="留给后续精加工的径向余量"),
            spec("finish_allowance_mm", "底面余量", K.FLOAT, 0.0, minimum=0.0,
                 maximum=20.0, step=0.1, unit="mm", group="切削"),
            spec("cut_mode", "走刀方式", K.CHOICE, "zigzag", group="切削", choices=(
                Choice("zigzag", "往复 Zigzag"),
                Choice("one_way", "单向 One-way"),
                Choice("contour", "环切 Contour"),
            ), help="平面铣支持往复/单向；型腔铣以环切为主"),
            spec("direction_deg", "走刀方向", K.FLOAT, 0.0, minimum=0.0, maximum=180.0,
                 step=15.0, unit="°", group="切削"),
            spec("finish_pass", "精修轮廓", K.BOOL, True, group="切削",
                 help="在每层最后沿轮廓补一刀，侧壁更光洁"),
        )
    )


def safety_parameters() -> ParameterSet:
    """安全与辅助参数。"""

    return ParameterSet(
        (
            spec("safe_height_mm", "安全高度", K.FLOAT, 10.0, minimum=0.5,
                 maximum=500.0, step=1.0, unit="mm", group="安全",
                 help="相对毛坯顶面的抬刀高度"),
            spec("clearance_mm", "进刀间隙", K.FLOAT, 1.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="安全",
                 help="切入前在毛坯外侧的定位距离"),
            spec("rapid_feed_mm_per_min", "快移速度", K.FLOAT, 5000.0, minimum=100.0,
                 maximum=60000.0, step=500.0, unit="mm/min", group="安全"),
            spec("spindle_direction", "主轴转向", K.CHOICE, "cw", group="安全",
                 choices=SPINDLE_DIRECTIONS),
            spec("coolant", "冷却", K.CHOICE, "flood", group="安全", choices=COOLANTS),
        )
    )


def cam_parameters() -> ParameterSet:
    """全部 CAM 参数（合计顺序就是界面上的分组顺序）。"""

    return tool_parameters() + cutting_parameters() + safety_parameters()


def controller_parameters() -> ParameterSet:
    """后处理相关参数（程序号、工件坐标系、公差）。"""

    return ParameterSet(
        (
            spec("program_number", "程序号", K.INT, 1000, minimum=1, maximum=99999,
                 step=1, group="后处理", help="写成 O 号"),
            spec("work_offset", "工件坐标系", K.CHOICE, "g54", group="后处理", choices=(
                Choice("g54", "G54"), Choice("g55", "G55"), Choice("g56", "G56"),
                Choice("g57", "G57"), Choice("g58", "G58"), Choice("g59", "G59"),
            )),
            spec("use_cycles", "使用固定循环", K.BOOL, False, group="后处理",
                 help="开启后每层切深用 G83/G1 表达式注释标出，便于人工核对"),
            spec("output_comments", "输出注释", K.BOOL, True, group="后处理",
                 help="在 NC 程序里写入工序与参数说明"),
        )
    )


def tool_from_cam_parameters(values: Mapping[str, Any]) -> Tool:
    """由 CAM 参数构造一把刀。

    刀具类型取 ``tool_kind``（刀具库选刀时由刀具类型映射而来），缺省是平底刀——
    2.5 轴的栅格距离场就是按平底刀建模的，这里保持原有行为不变。
    """

    diameter = float(values.get("tool_diameter_mm", 10.0))
    length = float(values.get("tool_length_mm", 40.0))
    if diameter <= 0 or length <= 0:
        raise ParameterError("刀具直径与长度必须为正")
    try:
        kind = ToolKind(str(values.get("tool_kind") or ToolKind.FLAT.value))
    except ValueError:
        kind = ToolKind.FLAT
    return Tool(
        kind=kind,
        diameter_mm=diameter,
        length_mm=length,
        flute_length_mm=_optional_positive(values.get("tool_flute_mm")),
    )


def tool_geometry_parameters(tool: Tool) -> dict[str, Any]:
    """刀具几何 → 工序参数里的刀具键（与 :func:`tool_geometry_values` 对称）。

    每次生成工序前用它把刀具的实际几何**覆盖**进参数字典：刀具库里改了直径，
    刀路读到的就是新直径，不会留下"参数里写着 10、实际按 6 算"的错位。
    """

    values: dict[str, Any] = {
        "tool_kind": tool.kind.value,
        "tool_diameter_mm": tool.diameter_mm,
        "tool_length_mm": tool.length_mm,
    }
    flute = tool.flute_length_mm
    if flute is None or flute <= 0:
        flute = min(tool.length_mm, max(1.0, tool.length_mm * 0.6))
    values["tool_flute_mm"] = float(flute)
    return values


def _optional_positive(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


__all__ = [
    "CAM_FIXED",
    "COOLANTS",
    "SPINDLE_DIRECTIONS",
    "cam_parameters",
    "controller_parameters",
    "cutting_parameters",
    "safety_parameters",
    "tool_from_cam_parameters",
    "tool_geometry_parameters",
    "tool_parameters",
]
