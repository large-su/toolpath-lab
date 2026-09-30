"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
将来接入碰撞检查时，刀轴姿态也从这里出发。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc（可调）          R - Rc
===========  ==================  =================  =======================

三种刀型都可在界面上选择：

- **平底刀 flat**：底面是半径 R 的平面，足迹半径 = R；
- **球头刀 ball**：只有刀尖一点落在加工面上，足迹半径 = 0（刀体比刀尖宽的那部分在加工面之上）；
- **圆鼻刀 bull**：底面是半径 R − Rc 的平底，外圈用半径 Rc 的圆角过渡到刀体，
  足迹半径 = R − Rc。Rc 是**用户参数**（corner_radius_mm），只在圆鼻刀下生效：
  平底刀恒为 0，球头刀恒为 R。

校验：圆鼻刀的 0 <= Rc <= R 必须成立，否则刀型在几何上不成立（会抛 ParameterError）。
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


#: 参数目录里的刀具类型选项；三种都已实现，没有 disabled 项。
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
                 choices=TOOL_KINDS, help="三种刀型的足迹半径不同，会直接影响刀路相对轮廓的偏置量"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("corner_radius_mm", "刀尖圆角 Rc", K.FLOAT, 0.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具",
                 help="仅圆鼻刀生效（0 等效平底、等于半径等效球头）；平底刀与球头刀会自动忽略它",
                 visible_if={"kind": ToolKind.BULL.value}),
        )
    )



@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    #: 刀尖圆角半径；只在圆鼻刀下由用户指定，另两种刀型在 __post_init__ 里被规范化。
    corner_radius_mm: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.corner_radius_mm):
            raise ParameterError("刀尖圆角半径必须是有限数")

        if self.kind is ToolKind.BALL:
            # 球头刀：整个刀尖都是圆角，Rc 就是刀具半径。
            effective = self.radius_mm
        elif self.kind is ToolKind.BULL:
            if self.corner_radius_mm < 0.0 or self.corner_radius_mm > self.radius_mm + 1e-9:
                raise ParameterError(
                    f"圆鼻刀的刀尖圆角半径 {self.corner_radius_mm:g} mm 必须落在 "
                    f"0 ~ {self.radius_mm:g} mm（刀具半径）之间"
                )
            effective = float(self.corner_radius_mm)
        else:
            # 平底刀：没有圆角，忽略传入的 Rc（界面上那格本来也是隐藏的）。
            effective = 0.0

        if effective != self.corner_radius_mm:
            object.__setattr__(self, "corner_radius_mm", effective)

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
    def flat_radius_mm(self) -> float:
        """底面的平底部分半径 Rf：平底刀为 R，球头刀为 0，圆鼻刀为 R − Rc。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_radius_mm)
        return self.radius_mm

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。

        - 平底刀：整个底面都贴着加工面，足迹是半径 R 的圆 → R；
        - 球头刀：只有刀尖一点落在加工面上 → 0；
        - 圆鼻刀：贴住加工面的是半径 R − Rc 的平底 → R − Rc。
        """

        return self.flat_radius_mm

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "flat_radius_mm": self.flat_radius_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
        }
