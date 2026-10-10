"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
将来接入球头/圆鼻刀时，残留高度、刀轴姿态也都从这里出发。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

当前对外只开放平底刀：其余两种在参数目录里标记为"待拓展"（Choice.disabled），
想启用它们只需去掉那个标记，并补上对应的三维显示。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)


class ToolKind(str, Enum):
    """本工程建模的刀具类型。"""

    FLAT = "flat"
    BALL = "ball"
    BULL = "bull"


#: 参数目录里的刀具类型选项。球头刀与圆鼻刀已经可用，它们的足迹半径公式见
#: `Tool.footprint_radius_mm`，三维外形见 web/js/viewport.js 的 toolProfileGeometry。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose"),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose"),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS,
                 help="平底刀刀尖是平面；球头刀刀尖是半球；圆鼻刀刀尖是带圆角的平面"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("corner_radius_mm", "刀尖圆角 Rc", K.FLOAT, 2.0, minimum=0.1, maximum=50.0,
                 step=0.1, unit="mm", group="刀具",
                 help="仅圆鼻刀有效：刀尖平面外缘的圆角；足迹半径 = D/2 − Rc，Rc 越大切得越浅",
                 visible_if={"kind": "bull"}),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。

    字段 `corner_radius` 只对圆鼻刀有意义；平底刀忽略它，球头刀固定等于半径。
    对外的派生量请读属性 `corner_radius_mm` 与 `footprint_radius_mm`。
    """

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    corner_radius: float = 2.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.corner_radius) or self.corner_radius <= 0:
            raise ParameterError("刀尖圆角半径必须是有限正数")
        if self.kind is ToolKind.BULL and self.corner_radius >= self.radius_mm:
            # Rc >= R 时刀尖圆角会吃掉整个端面，那就退化成球头刀了。
            raise ParameterError(
                f"圆鼻刀的刀尖圆角 {self.corner_radius:g} mm 必须小于半径 "
                f"{self.radius_mm:g} mm，否则就变成球头刀了"
            )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            corner_radius=float(params.get("corner_radius_mm", 2.0)),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径，圆鼻刀取参数值）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return self.corner_radius
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。

        - 平底刀：整个端面都在切，足迹就是 R；
        - 球头刀：刀尖是半球，与加工面只切在一个点上，足迹为 0；
        - 圆鼻刀：刀尖平面半径 R − Rc 那一圈在切，足迹比平底刀小。
        """

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_radius)
        return self.radius_mm

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
        }
