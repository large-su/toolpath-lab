"""Tool geometry.

The tool is not decoration: the strategies' boundary offset comes from how far the cutter reaches
sideways over the height it cuts, and the coverage analysis sweeps the flat part of its bottom.

===========  ==================  =================  =======================
kind         bottom radius Rf    corner radius Rc   footprint radius
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

Three quantities are worth keeping apart:

- **footprint radius** (`R - Rc`): the flat contact on the machining plane, which is what the offset
  at the tip is and what the coverage analysis sweeps as a disc. A ball nose tool touches the floor in
  a single point, so its planar coverage is a point too -- the real surface is the envelope of the
  ball, its scallop height depends on the stepover, and this model does not simulate that.
- **corner radius** (`Rc`): 0 for a flat mill, the tool radius for a ball nose, and a parameter for a
  bull nose tool (`0 <= Rc <= R`; 0 makes it a flat mill again, R makes it a ball nose).
- **wall clearance** (`wall_clearance_mm`): the widest horizontal reach over a cut of a given depth.
  A flat mill reaches R at any depth, while a ball or bull nose tool is narrower at the tip and only
  reaches its full R once the cut is at least Rc deep. With step-down the offset has to use the
  clearance at the *deepest* layer, otherwise the tool would gouge the wall down there; the planning
  context therefore exposes `cutting_radius_mm` rather than the raw footprint.

All three kinds are selectable now. The parameter layer validates the corner radius and the domain
layer keeps its invariants, both raising `ParameterError` for anything impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite, sqrt
from typing import Any, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)


class ToolKind(str, Enum):
    """Tool kinds modelled by this project."""

    FLAT = "flat"
    BALL = "ball"
    BULL = "bull"


#: Tool kind choices in the parameter catalogue; every kind is selectable.
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose"),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose"),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}


def tool_parameters() -> ParameterSet:
    """Parameter declarations for the tool group (drives both the UI and request validation)."""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS,
                 help="平底刀贴壁间隙就是半径；球头与圆鼻刀按整段切深的外伸半径算"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("corner_radius_mm", "圆鼻刀角半径 Rc", K.FLOAT, 0.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具",
                 help="只对圆鼻刀有效：0 = 平底，等于刀具半径 = 球头；超过刀具半径会被拒绝"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """A validated tool."""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    bull_corner_radius_mm: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.bull_corner_radius_mm) or self.bull_corner_radius_mm < 0:
            raise ParameterError("圆鼻刀的角半径必须是非负有限数")
        if self.bull_corner_radius_mm > self.radius_mm + 1e-9:
            raise ParameterError(
                f"圆鼻刀的角半径 {self.bull_corner_radius_mm:g} mm "
                f"不能超过刀具半径 {self.radius_mm:g} mm"
            )

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """Build a tool from the parameter dictionary of the UI/API."""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            bull_corner_radius_mm=float(params.get("corner_radius_mm", 0.0)),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """Effective corner radius: 0 for a flat mill, the radius for a ball nose, Rc for a bull."""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return min(self.bull_corner_radius_mm, self.radius_mm)
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """Flat contact radius on the machining plane: the flat nose of the cutter."""

        return max(0.0, self.radius_mm - self.corner_radius_mm)

    def wall_clearance_mm(self, depth_mm: float) -> float:
        """Horizontal reach of the cutter over a cut `depth_mm` tall, measured from the tool axis.

        The cutter is a flat bottom of radius `R - Rc` plus a corner of radius `Rc`, so its reach grows
        from the flat radius to the full radius R over the first `Rc` of depth and stays there.
        """

        corner = self.corner_radius_mm
        if depth_mm <= 0.0 or corner <= 0.0:
            return self.footprint_radius_mm
        rise = min(depth_mm, corner)
        return self.footprint_radius_mm + sqrt(corner * corner - (corner - rise) ** 2)

    def describe(self) -> dict[str, Any]:
        """Summary used by the UI and the API."""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
        }
