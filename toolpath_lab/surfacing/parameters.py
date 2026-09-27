"""曲面加工的声明式参数（与项目其它部分一致：一份声明驱动校验、界面与文档）。

参数按"策略 / 刀具 / 切削 / 安全"分组，和 NX、Mastercam 的参数页习惯一致。
平行行切与等高铣各用到其中一部分，界面上按 ``strategy`` 联动显示。
"""

from __future__ import annotations

from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.operation import SURFACE_STRATEGIES
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)

#: 加工策略。
STRATEGIES: tuple[Choice, ...] = (
    Choice("parallel", "平行行切"),
    Choice("waterline", "等高铣"),
)

#: 走刀方式（平行行切）。
CUT_MODES: tuple[Choice, ...] = (
    Choice("zigzag", "往复"),
    Choice("one_way", "单向"),
)

#: 曲面加工可用的刀具类型。**这里和 2.5 轴的刀具目录不一样**：
#: opencamlib 的落刀本来就实现了圆柱刀、球头刀与圆鼻刀，所以曲面加工三种都能用；
#: 而 ``cam`` 的栅格距离场只按平底刀建模，那边仍然只开放平底刀。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice("flat", "平底刀 Flat"),
    Choice("ball", "球头刀 Ball nose"),
    Choice("bull", "圆鼻刀 Bull nose"),
)

#: 切削层序（等高铣）。
LEVEL_ORDERS: tuple[Choice, ...] = (
    Choice("top_down", "自上而下"),
    Choice("bottom_up", "自下而上"),
)


def surface_parameters() -> ParameterSet:
    """曲面加工参数集合。"""

    return ParameterSet((
        spec("strategy", "加工策略", K.CHOICE, default="parallel", choices=STRATEGIES,
             group="策略", help="平行行切适合平缓曲面；等高铣适合陡壁。"),
        spec("cut_mode", "走刀方式", K.CHOICE, default="zigzag", choices=CUT_MODES,
             group="策略", visible_if={"strategy": "parallel"},
             help="往复不抬刀、效率高；单向表面质量更一致。"),
        spec("direction_deg", "走刀方向", K.FLOAT, default=0.0, minimum=-360.0,
             maximum=360.0, step=15.0, unit="°", group="策略",
             visible_if={"strategy": "parallel"},
             help="切削角，与 +X 轴的夹角。"),
        spec("level_order", "层序", K.CHOICE, default="top_down", choices=LEVEL_ORDERS,
             group="策略", visible_if={"strategy": "waterline"}),

        spec("tool_kind", "刀具类型", K.CHOICE, default="flat", choices=TOOL_KINDS,
             group="刀具", help="球头刀适合曲面精加工；等高铣的偏置法只对平底刀严格正确。"),
        spec("tool_diameter_mm", "刀具直径 D", K.FLOAT, default=6.0, minimum=0.2,
             maximum=100.0, step=0.5, unit="mm", group="刀具"),
        spec("tool_length_mm", "刀具长度 L", K.FLOAT, default=40.0, minimum=1.0,
             maximum=400.0, step=5.0, unit="mm", group="刀具"),

        spec("stepover_mm", "行距", K.FLOAT, default=2.0, minimum=0.05, maximum=100.0,
             step=0.5, unit="mm", group="切削",
             visible_if={"strategy": "parallel"},
             help="相邻两条扫描线的距离。"),
        spec("sampling_mm", "采样间距", K.FLOAT, default=0.5, minimum=0.02,
             maximum=20.0, step=0.1, unit="mm", group="切削",
             visible_if={"strategy": "parallel"},
             help="越小越贴合曲面，也越慢。"),
        spec("step_down_mm", "层高", K.FLOAT, default=2.0, minimum=0.05, maximum=100.0,
             step=0.5, unit="mm", group="切削",
             visible_if={"strategy": "waterline"},
             help="每层下降量。"),
        spec("side_allowance_mm", "侧面余量", K.FLOAT, default=0.0, minimum=0.0,
             maximum=20.0, step=0.25, unit="mm", group="切削",
             visible_if={"strategy": "waterline"},
             help="偏置时额外留出的余量。"),
        spec("stock_allowance_mm", "留量", K.FLOAT, default=0.0, minimum=0.0,
             maximum=20.0, step=0.25, unit="mm", group="切削",
             visible_if={"strategy": "parallel"},
             help="整条刀路整体抬高这么多。"),
        spec("feed_mm_per_min", "进给 F", K.FLOAT, default=800.0, minimum=1.0,
             maximum=20000.0, step=50.0, unit="mm/min", group="切削"),

        spec("safe_height_mm", "安全高度", K.FLOAT, default=10.0, minimum=0.5,
             maximum=500.0, step=1.0, unit="mm", group="安全"),
        spec("rapid_feed_mm_per_min", "快移速度", K.FLOAT, default=5000.0, minimum=100.0,
             maximum=60000.0, step=500.0, unit="mm/min", group="安全"),
    ))


def coerce_surface_parameters(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """校验并补全一份曲面加工参数。"""

    return dict(surface_parameters().coerce(payload or {}))


def surface_parameters_for(kind: str) -> ParameterSet:
    """某一类曲面工序**真正用得上**的参数。

    声明里所有可选项都挂在 ``strategy`` 上（``visible_if``），但 ``strategy`` 本身
    是由加工类型隐含的、界面上并不出现的内部键。所以这里按类型把它过滤掉：
    平行行切的面板上不该有"层高"，等高铣也不该有"行距"。

    留在参数声明里的 ``visible_if`` 仍然有用——前端 `refreshVisibility` 会在
    参数联动时再兜一次底（例如换了刀具类型之后要补出新的相关参数）。
    """

    strategy = SURFACE_STRATEGIES.get(kind)
    if strategy is None:
        raise ParameterError(f"{kind!r} 不是曲面加工类型")
    kept = []
    for item in surface_parameters().specs:
        if item.key == "strategy":
            continue
        rules = dict(item.visible_if)
        if rules and rules.get("strategy", strategy) != strategy:
            continue
        kept.append(item)
    return ParameterSet(tuple(kept))


def surface_defaults_for(kind: str) -> dict[str, Any]:
    """某一类曲面工序的默认参数。"""

    return surface_parameters_for(kind).defaults()


__all__ = ["CUT_MODES", "LEVEL_ORDERS", "STRATEGIES", "TOOL_KINDS",
           "coerce_surface_parameters", "surface_defaults_for", "surface_parameters",
           "surface_parameters_for"]
