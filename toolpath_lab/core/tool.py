"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
曲面精加工的切宽由"残留高度"反算，两者都只取决于刀尖形状。所以三种最常见的
铣刀在这里被归一到同一套描述：

==========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
==========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
==========  ==================  =================  =======================

三种刀型还共用同一条**刀尖回转母线**（tip_profile）：界面把它绕刀具轴旋转成
三维实体，曲面策略用它算残留高度。于是"新增一种刀具形态"就变成了"再加一条母线"，
而不必在每个消费它的地方各加一个分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import cos, isfinite, pi, sin
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

#: 刀尖母线圆弧的离散段数；只影响三维显示与载荷大小。
PROFILE_ARC_SEGMENTS = 24


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS, help="刀尖形状决定足迹半径，也决定切宽能否按残留高度反算"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("corner_radius_mm", "刀尖圆角 Rc", K.FLOAT, 1.0, minimum=0.1,
                 maximum=50.0, step=0.1, unit="mm", group="刀具",
                 visible_if={"kind": ToolKind.BULL.value},
                 help="圆鼻刀的刀尖圆角半径，不得大于刀具半径；球头刀恒等于半径、平底刀恒为 0"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。

    corner_radius_mm 保存的是**生效值**：平底刀恒为 0，球头刀恒等于半径，
    圆鼻刀取参数值。调用方因此不必再按类型分支。
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
            raise ParameterError("刀尖圆角半径必须是非负有限值")
        radius = self.diameter_mm / 2.0
        if self.kind is ToolKind.BALL:
            object.__setattr__(self, "corner_radius_mm", radius)
        elif self.kind is ToolKind.FLAT:
            object.__setattr__(self, "corner_radius_mm", 0.0)
        elif self.corner_radius_mm > radius + 1e-9:
            raise ParameterError(
                f"圆鼻刀的刀尖圆角半径 {self.corner_radius_mm:g} mm "
                f"不能大于刀具半径 {radius:g} mm"
            )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        raw_corner = params.get("corner_radius_mm")
        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            corner_radius_mm=float(raw_corner) if raw_corner is not None else 0.0,
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def bottom_radius_mm(self) -> float:
        """刀尖平底部分的半径：平底刀 R，球头刀 0，圆鼻刀 R − Rc。"""

        return max(0.0, self.radius_mm - self.corner_radius_mm)

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        return self.bottom_radius_mm

    @property
    def flute_length_mm(self) -> float:
        """三维显示里切削部分的长度；至少覆盖刀尖圆弧。"""

        return max(min(self.length_mm * 0.65, self.radius_mm * 6.0),
                   min(self.radius_mm * 1.5, self.length_mm))

    def tip_profile(self, segments: int = PROFILE_ARC_SEGMENTS) -> list[list[float]]:
        """刀尖回转母线，元素为 [r, z]（mm），刀尖在 z = 0。

        界面把它绕刀具轴旋转成实体，所以这条母线就是"刀具形态"的唯一来源。
        """

        radius = self.radius_mm
        flute = max(self.flute_length_mm, self.corner_radius_mm + 1.0)
        points: list[list[float]] = []

        if self.kind is ToolKind.FLAT:
            points.append([0.0, 0.0])
            points.append([radius, 0.0])
        elif self.kind is ToolKind.BALL:
            # 球心在 z = R，母线是半径 R 的四分之一圆弧。
            for step in range(segments + 1):
                angle = (pi / 2.0) * step / segments
                points.append([radius * sin(angle), radius * (1.0 - cos(angle))])
        else:
            # 平底 + 圆角，圆角圆弧的圆心在 (R − Rc, Rc)。
            bottom = self.bottom_radius_mm
            corner = self.corner_radius_mm
            points.append([0.0, 0.0])
            points.append([bottom, 0.0])
            for step in range(segments + 1):
                angle = (pi / 2.0) * step / segments
                points.append([bottom + corner * sin(angle), corner * (1.0 - cos(angle))])

        if points[-1][1] < flute:
            points.append([points[-1][0], flute])
        if points[-1][0] < radius - 1e-9:
            points.append([radius, points[-1][1]])
        return [[round(r, 5), round(z, 5)] for r, z in points]

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "bottom_radius_mm": self.bottom_radius_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
            "flute_length_mm": self.flute_length_mm,
            "profile": self.tip_profile(),
        }
