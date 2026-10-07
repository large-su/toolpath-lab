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

三种刀具类型（平底刀 / 球头刀 / 圆鼻刀）均已开放：平底刀与球头刀为常见基准形态，
圆鼻刀可通过"刀尖圆角半径 Rc"参数自由调节刀尖圆角，用于不同的加工残留高度控制。
"""

from __future__ import annotations

import math
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


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS, help="平底刀 / 球头刀 / 圆鼻刀三种刀具类型"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("corner_mm", "刀尖圆角半径 Rc", K.FLOAT, 0.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具", help="仅圆鼻刀生效；球头刀的圆角恒等于刀具半径"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("stepover_mm", "行距（步距）", K.FLOAT, 1.0, minimum=0.1, maximum=50.0,
                 step=0.1, unit="mm", group="刀具",
                 help="相邻刀轨的间距；配合刀尖圆角几何估算加工表面残留高度"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    corner_mm: float = 0.0
    stepover_mm: float = 1.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.corner_mm) or self.corner_mm < 0:
            raise ParameterError("刀具圆角半径必须是有限非负数")
        if self.corner_mm >= self.radius_mm:
            raise ParameterError("刀具圆角半径必须小于刀具半径")
        if not isfinite(self.stepover_mm) or self.stepover_mm <= 0:
            raise ParameterError("行距必须是有限正数")

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            corner_mm=float(params.get("corner_mm", 0.0)),
            stepover_mm=float(params.get("stepover_mm", 1.0)),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径，圆鼻刀取名义圆角 Rc）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return self.corner_mm
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_mm)
        return self.radius_mm

    def residual_height_mm(self, stepover: float | None = None) -> float:
        """估算相邻刀轨之间的残留高度（加工表面质量的核心指标）。

        平底刀在平面加工中刀轨完全覆盖，残留为零；
        球头刀按圆弧截面估算：h = R - sqrt(R^2 - (s/2)^2)；
        圆鼻刀底部有半径 R-Rc 的平底：行距不超过 2(R-Rc) 时无残留，
        超过后由刀尖圆角圆弧决定残留。
        """

        s = max(0.0, self.stepover_mm if stepover is None else stepover)
        if self.kind is ToolKind.FLAT:
            return 0.0
        radius = self.radius_mm
        if self.kind is ToolKind.BALL:
            if s >= 2.0 * radius:
                return radius
            return radius - math.sqrt(radius * radius - (s / 2.0) ** 2)
        # 圆鼻刀：平底半径 base = R - Rc，圆角半径 Rc
        corner = self.corner_radius_mm
        base = max(0.0, radius - corner)
        if s <= 2.0 * base:
            return 0.0
        if s >= 2.0 * radius:
            return corner
        half = (s - 2.0 * base) / 2.0
        if half >= corner:
            return corner
        return corner - math.sqrt(corner * corner - half * half)

    def recommended_stepover_mm(self, target_height: float = 0.02) -> float:
        """由允许残留高度反推最大行距（残留高度公式的反函数）。

        target_height 默认取 0.02 mm，是精加工常见的表面残留要求。
        """

        h = max(0.0, target_height)
        if self.kind is ToolKind.FLAT:
            return self.diameter_mm
        radius = self.radius_mm
        if self.kind is ToolKind.BALL:
            if h >= radius:
                return 2.0 * radius
            return 2.0 * math.sqrt(radius * radius - (radius - h) ** 2)
        corner = self.corner_radius_mm
        base = max(0.0, radius - corner)
        if h <= 0.0:
            return 2.0 * base
        if h >= corner:
            return 2.0 * radius
        return 2.0 * base + 2.0 * math.sqrt(corner * corner - (corner - h) ** 2)

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "length_mm": self.length_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
            "stepover_mm": self.stepover_mm,
            "residual_height_mm": self.residual_height_mm(),
            "recommended_stepover_mm": self.recommended_stepover_mm(),
        }
