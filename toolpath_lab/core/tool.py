"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
材料切除仿真则直接按下面的圆角半径推出刀底形状（残留高度也从这里出发）。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

平底刀、球头刀与圆鼻刀都已对外开放，三者在 simulation/material.py 里共用同一套
刀底公式（靠圆角半径 Rc 区分形态）。
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


#: 参数目录里的刀具类型选项。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose"),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose"),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}

#: 圆鼻刀圆角半径的允许范围：留出 0.5 mm 的依据是"圆角不能吃掉整个刀底"，
#: 否则底面半径退化为 0，圆鼻刀就变成球头刀了。
MIN_CORNER_MM = 0.5
CORNER_MARGIN_MM = 0.5


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS,
                 help="平底刀切出平底；球头刀留扇贝形残留；圆鼻刀介于两者之间"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("corner_radius_mm", "刀尖圆角 Rc", K.FLOAT, 1.0,
                 minimum=MIN_CORNER_MM, maximum=49.0, step=0.5, unit="mm", group="刀具",
                 help="仅圆鼻刀使用；必须小于刀具半径，越大刀底越接近球面"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    #: 仅圆鼻刀使用；平底刀忽略，球头刀恒等于半径。
    corner_radius_mm: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if self.kind is ToolKind.BULL:
            if not isfinite(self.corner_radius_mm) or self.corner_radius_mm <= 0:
                raise ParameterError("圆鼻刀的刀尖圆角必须是有限正数")
            if self.corner_radius_mm > self.radius_mm - CORNER_MARGIN_MM + 1e-9:
                raise ParameterError(
                    "圆鼻刀的刀尖圆角必须小于刀具半径（至少要小 %.1f mm）" % CORNER_MARGIN_MM
                )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        kind = ToolKind(str(params["kind"]))
        diameter = float(params["diameter_mm"])
        # 圆角只对圆鼻刀有意义：其余两种形态由几何唯一确定，忽略传入值。
        if kind is ToolKind.BULL:
            corner = float(params.get("corner_radius_mm", 1.0))
        else:
            corner = 0.0
        return cls(
            kind=kind,
            diameter_mm=diameter,
            length_mm=float(params["length_mm"]),
            corner_radius_mm=corner,
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def effective_corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径，圆鼻刀取用户值）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return min(self.corner_radius_mm, self.radius_mm)
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        return max(0.0, self.radius_mm - self.effective_corner_radius_mm)

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.effective_corner_radius_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
        }
