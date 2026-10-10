"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
材料切除仿真里的切除宽度则由"刀尖形状 + 轴向切深"共同决定。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

本版本已启用全部三种刀具（ball / bull 不再是"待拓展"），并新增两件事：

1. **圆鼻刀的圆角半径是参数**：`corner_radius_mm` 会按刀具类型归一化——
   平底刀固定 0、球头刀固定等于半径、圆鼻刀取用户输入（必须小于半径）；
2. **轴向切深下的切除足迹**：`cutting_footprint_radius_mm(ap)` 给出刀具
   下沉 ap 后在加工面上真正切到的半径，材料切除仿真用它决定切除范围：
   平底刀恒为 R；球头刀为 sqrt(2·R·ap - ap²)；圆鼻刀在 ap < Rc 时还要加上底面半径。

刀尖轮廓 `profile_mm()` 以 (径向, 轴向) 折线给出，供三维显示与文档绘图使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import cos, isfinite, pi, sin, sqrt
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


#: 参数目录里的刀具类型选项；三种类型都已实现，不再有 disabled 项。
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
                 help="三种刀具都已实现：圆鼻刀的圆角半径见下一项"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("corner_radius_mm", "刀尖圆角 Rc", K.FLOAT, 1.0, minimum=0.0,
                 maximum=49.0, step=0.5, unit="mm", group="刀具",
                 visible_if={"kind": ToolKind.BULL.value},
                 help="仅圆鼻刀使用，必须小于刀具半径；平底刀与球头刀自动忽略该值"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。

    `corner_radius_mm` 是**归一化后**的刀尖圆角半径：构造时按刀具类型改写，
    因此调用方可以直接读它，不必再判断类型。
    """

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

        radius = self.diameter_mm / 2.0
        if self.kind is ToolKind.BALL:
            object.__setattr__(self, "corner_radius_mm", radius)
        elif self.kind is ToolKind.FLAT:
            object.__setattr__(self, "corner_radius_mm", 0.0)
        elif self.corner_radius_mm >= radius:
            raise ParameterError(
                f"圆鼻刀的圆角半径 {self.corner_radius_mm:g} mm 必须小于刀具半径 {radius:g} mm"
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

    # -- 基本几何 ----------------------------------------------------------
    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_radius_mm)
        return self.radius_mm

    def cutting_footprint_radius_mm(self, axial_depth_mm: float) -> float:
        """刀具下沉 `axial_depth_mm` 后在加工面上真正切到的半径。

        - 平底刀：整个底面都吃刀，恒为 R；
        - 球头刀：只有球头低于已加工面的部分吃刀，为 sqrt(2·R·ap - ap²)；
        - 圆鼻刀：ap ≤ Rc 时底面不接触，等于底面半径 + 圆角部分的弦长；ap > Rc 后为 R。

        轴向切深非正（没有吃刀）时返回 0。
        """

        if not isfinite(axial_depth_mm) or axial_depth_mm <= 0.0:
            return 0.0
        radius = self.radius_mm
        if self.kind is ToolKind.FLAT:
            return radius
        if self.kind is ToolKind.BALL:
            capped = min(axial_depth_mm, radius)
            return sqrt(max(0.0, 2.0 * radius * capped - capped * capped))
        # BULL
        corner = self.corner_radius_mm
        if axial_depth_mm >= corner:
            return radius
        chord = sqrt(max(0.0, 2.0 * corner * axial_depth_mm - axial_depth_mm * axial_depth_mm))
        return min(radius, self.footprint_radius_mm + chord)

    def profile_mm(self, *, samples: int = 12) -> list[list[float]]:
        """刀尖轮廓折线，点为 (径向 r, 轴向 z)，z 从刀尖向上。"""

        radius = self.radius_mm
        points: list[list[float]] = []
        if self.kind is ToolKind.FLAT:
            points = [[0.0, 0.0], [radius, 0.0], [radius, self.length_mm]]
        elif self.kind is ToolKind.BALL:
            for index in range(samples + 1):
                angle = (pi / 2.0) * index / samples
                points.append([radius * sin(angle), radius * (1.0 - cos(angle))])
            points.append([radius, self.length_mm])
        else:  # BULL
            corner = self.corner_radius_mm
            flat = self.footprint_radius_mm
            points.append([0.0, 0.0])
            points.append([flat, 0.0])
            for index in range(samples + 1):
                angle = (pi / 2.0) * index / samples
                points.append([flat + corner * sin(angle), corner * (1.0 - cos(angle))])
            points.append([radius, self.length_mm])
        return [[round(float(a), 4), round(float(b), 4)] for a, b in points]

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        payload = {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
        }
        if self.kind is not ToolKind.FLAT:
            payload["cutting_footprint_at_1mm_mm"] = round(
                self.cutting_footprint_radius_mm(1.0), 4
            )
        return payload
