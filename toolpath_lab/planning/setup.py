"""工艺设置：安全高度、快移速度、边界处理。

这三样原本是 `planning/base.py` 里的常量（`SAFE_HEIGHT_MM`、`RAPID_FEED_MM_PER_MIN`）
和一个固定写法（边界一律内缩一个刀具足迹半径）。它们描述的是**机床和工艺**，
而不是某一种刀路策略，所以这里做成一份独立的参数声明，而不是塞进每个策略里——

- 声明一次，`GET /api/catalog` 导出，界面自动生成控件；
- 所有策略共用，不需要每个策略各写一遍；
- 请求里不写就取默认值，因此老脚本不受影响。

边界处理最容易误解，所以这里的"偏置量"统一用**刀心到轮廓的有符号距离**表达：
正值表示往里让（材料不被切出），负值表示往外让（故意留一圈不切的边）。
"""

from __future__ import annotations

from typing import Any, Mapping

from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)

#: 安全高度的默认值（mm）。
DEFAULT_SAFE_HEIGHT_MM = 5.0
#: 快移速度的默认值（mm/min）。
DEFAULT_RAPID_FEED_MM_PER_MIN = 5000.0

#: 边界处理方式。value 对应偏置量的取法。
BOUNDARY_MODES: tuple[Choice, ...] = (
    Choice("tool_radius", "内缩一个刀具半径（默认）"),
    Choice("none", "不偏置：刀心压在轮廓上"),
    Choice("outside", "外扩一个刀具半径：留一圈不切"),
    Choice("custom", "自定义偏置量"),
)


def setup_parameters() -> ParameterSet:
    """工艺设置的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("safe_height_mm", "安全高度", K.FLOAT, DEFAULT_SAFE_HEIGHT_MM,
                 minimum=0.1, maximum=100.0, step=0.5, unit="mm", group="设置",
                 help="快速移动时相对工件上表面抬起的高度"),
            spec("rapid_feed_mm_per_min", "快移速度", K.FLOAT, DEFAULT_RAPID_FEED_MM_PER_MIN,
                 minimum=100.0, maximum=60000.0, step=100.0, unit="mm/min", group="设置",
                 help="抬刀、横移、下刀这些不切削的快速定位"),
            spec("boundary_mode", "边界处理", K.CHOICE, "tool_radius", group="设置",
                 choices=BOUNDARY_MODES,
                 help="刀路相对区域轮廓的偏置方式；默认内缩一个刀具半径，保证不切出材料"),
            spec("boundary_offset_mm", "自定义偏置量", K.FLOAT, 0.0, minimum=-50.0,
                 maximum=50.0, step=0.5, unit="mm", group="设置",
                 help="正值往里让，负值往外留边；仅在边界处理选「自定义」时生效",
                 visible_if={"boundary_mode": "custom"}),
        )
    )


def boundary_offset(
    setup: Mapping[str, Any], footprint_radius_mm: float
) -> float:
    """把"边界处理方式"解析成一个有符号的偏置量（mm）。

    足迹半径取自刀具类型，所以球头刀/圆鼻刀会自然得到不同的偏置量，
    这里不重复推导，只负责选哪一种。
    """

    mode = str(setup.get("boundary_mode", "tool_radius"))
    if mode == "none":
        return 0.0
    if mode == "outside":
        return -footprint_radius_mm
    if mode == "custom":
        return float(setup.get("boundary_offset_mm", 0.0))
    return footprint_radius_mm