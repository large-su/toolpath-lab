"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
将来接入圆鼻刀时，刀轴姿态也从这里出发。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

球头刀（ball）的刀尖是一个半球：球心在刀轴上、距刀尖 R，半球与后面的圆柱部分
共用 z = R 处的端面。它在加工平面（z = 0）上只用一个刀尖点接触，所以足迹半径是
0 —— 刀尖走到区域轮廓即可，轮廓附近留下的是半径 R 的圆弧残留，相邻两刀之间还会
留下球面弓高（residual_height_mm()）。

三维显示同样由这里驱动：segments() 给出一组回转轮廓（(半径, 高度) 折线），
前端把它们旋成实体。因此"再加一种刀具"只需要在这里补几何，不用动 JavaScript。

当前对外只开放平底刀与球头刀：圆鼻刀在参数目录里标记为"待拓展"（Choice.disabled）。
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


#: 参数目录里的刀具类型选项；disabled 的项在界面上不可选。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose"),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose（待拓展）", disabled=True),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}

#: 半球轮廓的离散段数，决定三维显示的圆滑程度。
TIP_SEGMENTS = 24


@dataclass(frozen=True, slots=True)
class ToolSegment:
    """刀具的一段回转轮廓，供三维显示直接旋成实体。

    profile 是从刀尖（高度 0）向上的一组 (半径, 高度) 点，单位 mm；
    name 决定配色：cutting 是刀具的切削部分，shank 是刀柄。
    """

    name: str
    profile: tuple[tuple[float, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "profile_mm": [[r, z] for r, z in self.profile]}


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS, help="圆鼻刀留作拓展，启用方式见 docs/extending.md"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
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

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if self.kind is ToolKind.BALL and self.length_mm < self.radius_mm - 1e-9:
            raise ParameterError(
                f"球头刀长度 {self.length_mm:g} mm 小于半球半径 {self.radius_mm:g} mm，"
                "刀尖的半球装不进去"
            )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_radius_mm)
        return self.radius_mm

    @property
    def tip_height_mm(self) -> float:
        """刀尖轮廓的轴向高度：平底刀是 0（平的），球头刀是半径 R（半球）。"""

        return self.corner_radius_mm

    @property
    def flute_length_mm(self) -> float:
        """切削段的轴向长度（三维显示里那段黄色部分）。"""

        return min(self.length_mm * 0.65, self.radius_mm * 6.0)

    @property
    def cutting_length_mm(self) -> float:
        """切削段的总高度：至少要容得下刀尖轮廓，不能短于半球半径。"""

        return max(self.flute_length_mm, self.tip_height_mm)

    def segments(self) -> tuple[ToolSegment, ...]:
        """刀具的回转轮廓（刀尖朝下、高度从 0 起算）。

        平底刀 = 一个带底的圆柱；球头刀 = 半球 + 圆柱，两者共用 z = R 处的端面。
        轮廓是"半剖"的：绕刀轴旋一圈就是完整实体。
        """

        top = self.cutting_length_mm
        shank_radius = self.radius_mm * 1.25
        segments = [ToolSegment("cutting", self._cutting_profile(top))]
        if self.length_mm - top > 1e-9:
            segments.append(
                ToolSegment(
                    "shank",
                    ((0.0, top), (shank_radius, top), (shank_radius, self.length_mm),
                     (0.0, self.length_mm)),
                )
            )
        return tuple(segments)

    def _cutting_profile(self, top: float) -> tuple[tuple[float, float], ...]:
        """切削段的半剖轮廓：从刀尖（高度 0）向上到 top。"""

        radius = self.radius_mm
        points: list[tuple[float, float]] = []
        if self.kind is ToolKind.BALL:
            # 半球：球心在 (0, R)，从刀尖 (0, 0) 扫到赤道 (R, R)。
            for index in range(TIP_SEGMENTS + 1):
                angle = -pi / 2.0 + (pi / 2.0) * index / TIP_SEGMENTS
                points.append((radius * cos(angle), radius + radius * sin(angle)))
        else:
            # 平底：先画刀底平面，再沿半径竖直往上。
            points.append((0.0, 0.0))
            points.append((radius, 0.0))
        if top > points[-1][1] + 1e-9:
            points.append((radius, top))
        return tuple((round(r, 6), round(z, 6)) for r, z in points)

    def residual_height_mm(self, stepover_mm: float) -> float:
        """相邻两条刀线之间留下的理论残留高度（球面在平面上的弓高）。

        平底刀加工平面时不留残留；球头刀在切宽 ae 下留下 R - sqrt(R² - (ae/2)²)，
        所以切宽一大残留就急剧变高——这正是球头刀适合精加工曲面、不适合大切宽开粗的原因。
        """

        if self.kind is not ToolKind.BALL:
            return 0.0
        half = min(abs(stepover_mm) / 2.0, self.radius_mm)
        return self.radius_mm - sqrt(max(self.radius_mm**2 - half**2, 0.0))

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "tip_height_mm": self.tip_height_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
            "segments": [segment.to_dict() for segment in self.segments()],
        }
