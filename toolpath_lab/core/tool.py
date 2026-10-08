"""Tool geometry.

The tool is not decoration: the strategies' boundary offset comes from how far the cutter reaches
sideways over the height it cuts, the coverage analysis sweeps the flat part of its bottom, and the
holder collision check asks whether the part *above* the flutes fits into the pocket it machines.

===========  ==================  =================  =======================
kind         bottom radius Rf    corner radius Rc   footprint radius
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

Four quantities are worth keeping apart:

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
- **flute length and shank radius** (`flute_mm`, `shank_radius_mm`): the cutting head is the cylinder
  of radius R up to the flutes; above them the tool continues as the shank, which is what can hit the
  wall once a cut is deeper than the flutes (`planning/collision.py`). Both default to the rule the 3D
  view has always drawn -- `min(0.65 * length, 6 * radius)` and `1.25 * radius` -- and both are
  parameters, because whether a real tool collides depends on the real tool.
- **taper** (`taper_angle_deg`): a tapered tool widens above its bottom corner, by `tan(angle)` of
  radius per millimetre of height, so its reach at a depth of `d` is `R + (d - Rc) * tan(angle)`. The
  diameter parameter is then the diameter **at the tip**. This is why a tapered tool in a deep pocket
  is not a contradiction: `wall_clearance_mm` grows with the depth, so the strategy pushes the whole
  path further from the wall instead of gouging it (`cutting_radius_mm` uses the deepest layer). 0
  degrees (the default) is the straight tool every earlier number in this project was computed with.

All three kinds are selectable now. The parameter layer validates the corner radius and the domain
layer keeps its invariants, both raising `ParameterError` for anything impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite, radians, sqrt, tan
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
                 step=1.0, unit="mm", group="刀具",
                 help="刀刃与刀柄的总长（夹头以下的部分）；也是碰撞检查的输入：切深超过它，夹头就伸进工件了"),
            spec("corner_radius_mm", "圆鼻刀角半径 Rc", K.FLOAT, 0.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具",
                 help="只对圆鼻刀有效：0 = 平底，等于刀具半径 = 球头；超过刀具半径会被拒绝"),
            spec("taper_angle_deg", "锥度半角", K.FLOAT, 0.0, minimum=0.0, maximum=45.0,
                 step=0.5, unit="°", group="刀具",
                 help="刀刃侧面每侧的张开角度：从底面刀刃往上，每毫米长 tan(锥度) 的半径；"
                      "0 = 直壁（默认）。锥度刀的直径指刀尖处，深腔里靠贴壁间隙把锥面让出来"),
            spec("flute_length_mm", "刀刃长度 Lf", K.FLOAT, 0.0, minimum=0.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具",
                 help="刀刃部分的高度，刀柄从它顶端开始；0 = 自动取 0.65 × 刀具长度与 "
                      "6 × 刀具直径的较小值。切深超过刀刃后刀柄会进入型腔，可能碰到壁"),
            spec("shank_diameter_mm", "刀柄直径 Ds", K.FLOAT, 0.0, minimum=0.0, maximum=200.0,
                 step=0.5, unit="mm", group="刀具",
                 help="刀刃以上的刀柄直径（夹头可当成更大的值）；0 = 自动取 1.25 × 刀具直径。"
                      "比刀刃粗的刀柄需要型腔留出相应间隙"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """A validated tool."""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    bull_corner_radius_mm: float = 0.0
    #: Taper half-angle of the flanks in degrees; 0 (the default) is a straight-walled tool.
    taper_angle_deg: float = 0.0
    #: Requested flute length; 0 (the default) means "use the model's own rule" -- see flute_mm.
    flute_length_mm: float = 0.0
    #: Requested shank diameter; 0 (the default) means "use the model's own rule" -- see shank_radius_mm.
    shank_diameter_mm: float = 0.0

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
        if not isfinite(self.taper_angle_deg) or self.taper_angle_deg < 0.0:
            raise ParameterError("锥度半角必须是非负有限数")
        if self.taper_angle_deg >= 90.0:
            raise ParameterError(f"锥度半角 {self.taper_angle_deg:g}° 必须小于 90°")
        if not isfinite(self.flute_length_mm) or self.flute_length_mm < 0:
            raise ParameterError("刀刃长度必须是非负有限数")
        if self.flute_length_mm > self.length_mm + 1e-9:
            raise ParameterError(
                f"刀刃长度 {self.flute_length_mm:g} mm 不能超过刀具长度 {self.length_mm:g} mm"
            )
        if not isfinite(self.shank_diameter_mm) or self.shank_diameter_mm < 0:
            raise ParameterError("刀柄直径必须是非负有限数")

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """Build a tool from the parameter dictionary of the UI/API."""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            bull_corner_radius_mm=float(params.get("corner_radius_mm", 0.0)),
            taper_angle_deg=float(params.get("taper_angle_deg", 0.0)),
            flute_length_mm=float(params.get("flute_length_mm", 0.0)),
            shank_diameter_mm=float(params.get("shank_diameter_mm", 0.0)),
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
        from the flat radius to the full radius R over the first `Rc` of depth; above that corner a
        tapered tool keeps widening, by `tan(taper)` of radius per millimetre of height. A straight
        tool (taper 0, the default) therefore stays at R, which is what every earlier number in the
        project was computed with.
        """

        corner = self.corner_radius_mm
        slope = self.taper_slope
        if depth_mm <= 0.0:
            return self.footprint_radius_mm
        if corner <= 0.0:
            # A flat mill: the flank starts right at the bottom edge of the flat.
            return self.radius_mm + depth_mm * slope
        rise = min(depth_mm, corner)
        reach = self.footprint_radius_mm + sqrt(corner * corner - (corner - rise) ** 2)
        if depth_mm > corner:
            reach += (depth_mm - corner) * slope
        return reach

    @property
    def taper_slope(self) -> float:
        """Radius gained per millimetre of height on the flanks (`tan` of the taper half-angle)."""

        return tan(radians(self.taper_angle_deg))

    @property
    def flank_radius_mm(self) -> float:
        """Radius at the top of the flutes: the widest part of the cutting portion of the tool."""

        return self.wall_clearance_mm(self.flute_mm)

    @property
    def flute_mm(self) -> float:
        """Height of the cutting head: the flutes end here and the shank starts.

        A requested `flute_length_mm` wins; 0 (the default) means the tool model's own rule,
        `min(0.65 * length, 6 * radius)`, which is the head the 3D view has always drawn. Everything
        the collision check needs to know about the tool above the tip starts from this number.
        """

        if self.flute_length_mm > 0.0:
            return min(self.flute_length_mm, self.length_mm)
        return min(0.65 * self.length_mm, 6.0 * self.radius_mm)

    @property
    def shank_radius_mm(self) -> float:
        """Radius of the tool above the flutes.

        A requested `shank_diameter_mm` wins; 0 (the default) means `1.25 * radius`, again the stand-in
        the view draws. A value *smaller* than the tool radius is allowed: that is a necked tool, and
        it can only make the collision check more forgiving.
        """

        if self.shank_diameter_mm > 0.0:
            return self.shank_diameter_mm / 2.0
        return 1.25 * self.radius_mm

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
            # Resolved tool geometry: the view draws exactly this, and the collision check uses it.
            "taper_angle_deg": self.taper_angle_deg,
            "flute_mm": self.flute_mm,
            "flank_radius_mm": self.flank_radius_mm,
            "shank_radius_mm": self.shank_radius_mm,
        }
