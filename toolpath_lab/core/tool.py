"""刀具几何。

刀具不是装饰：刀路相对区域轮廓的偏置量由"刀具在加工面上的足迹半径"决定，
残留高度、刀轴姿态将来也从这里出发。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

三种刀具都已经开放，而三条公式其实是一句话：**足迹半径 = 半径 − 刀尖圆角半径**。
平底刀的圆角是 0，得到 R；球头刀的圆角就是半径，得到 0（只有刀尖接触）；圆鼻刀落在
两者之间——参数 `corner_radius_mm` 取 0 就退化成平底刀，取 R 就退化成球头刀。

足迹半径的物理含义是"刀在加工面上压出的那片区域有多宽"：平底刀与圆鼻刀有有宽度的
切削带，球头刀只有一点，所以球头刀的刀路能一路贴到区域轮廓上。平面上的残留高度要等
三维曲面加工模型才算得清，本基座不计算它。

三维显示：平底刀是一段圆柱；球头刀是"球的下半部分 + 同半径圆柱刃部"；圆鼻刀是
"平底 + 圆环过渡 + 圆柱刃部"（见 web/js/viewport.js 的 setTool）。
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


#: 参数目录里的刀具类型选项；disabled 的项在界面上不可选（当前三种都开放）。
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
                 help="足迹半径 = 半径 − 刀尖圆角半径：平底刀 R、球头刀 0、圆鼻刀 R − Rc"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("corner_radius_mm", "刀尖圆角 Rc", K.FLOAT, 1.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具",
                 help="只有圆鼻刀用得到：0 等于平底刀，等于半径就是球头刀",
                 visible_if={"kind": ToolKind.BULL.value}),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    corner_radius_mm: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.corner_radius_mm) or self.corner_radius_mm < 0:
            raise ParameterError("刀尖圆角半径必须是非负有限数")
        if self.kind is ToolKind.BULL and self.corner_radius_mm > self.radius_mm + 1e-9:
            raise ParameterError(
                f"刀尖圆角半径 {self.corner_radius_mm:g} mm 不能大于刀具半径 "
                f"{self.radius_mm:g} mm"
            )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            corner_radius_mm=float(params.get("corner_radius_mm") or 0.0),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def effective_corner_radius_mm(self) -> float:
        """实际刀尖圆角半径：球头刀就是半径，圆鼻刀取参数（不超过半径），平底刀为 0。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return min(self.corner_radius_mm, self.radius_mm)
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径 = 半径 − 刀尖圆角半径，也就是相对轮廓的偏置量。"""

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
