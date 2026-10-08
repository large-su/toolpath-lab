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

import numpy as np

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

#: Slack on the profile's reach test, so a tool exactly of radius R still cuts at its own edge.
_REACH_EPS_MM = 1e-9

#: Parameter keys of the tool group, in declaration order (the panel fills exactly these from a preset).
TOOL_PARAMETER_KEYS: tuple[str, ...] = (
    "kind",
    "diameter_mm",
    "length_mm",
    "corner_radius_mm",
    "taper_angle_deg",
    "flute_length_mm",
    "shank_diameter_mm",
)


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

    @property
    def reach_radius_mm(self) -> float:
        """How far from its axis the *cutting* surface reaches: the window a removal sweep needs.

        Planar coverage sweeps the flat contact (`footprint_radius_mm`), but material removal has to
        account for the curved nose: a cut `d` deep takes material out to `sqrt(2*R*d - d^2)` from the
        axis with a ball nose, and out along the corner torus of a bull nose, so both reach the full
        radius R. A tapered tool's flanks rise *above* its flat bottom, so they add nothing to the
        footprint of one layer.
        """

        if self.kind in (ToolKind.BALL, ToolKind.BULL):
            return self.radius_mm
        return self.footprint_radius_mm

    def profile_height_mm(self, distance_mm: Any) -> Any:
        """Height of the cutting surface **above the tip** at `distance_mm` from the tool axis.

        This is the tool's own cross section, and it is what makes material removal a surface sweep
        instead of a flat-bottomed disc: a ball nose sits `R - sqrt(R^2 - d^2)` higher at offset `d`, a
        bull nose is flat out to `R - Rc` and then follows its corner torus, and flat or tapered tools
        keep the flat bottom (a tapered flank is modelled for wall clearance, not for the floor it
        leaves). `inf` means the tool does not reach that far, so `min` over a sweep reads naturally.

        Accepts a scalar or an array (the sweep works block by block) and returns the same shape, as a
        plain `float` for scalars so it can go straight into a payload.
        """

        distance = np.asarray(distance_mm, dtype=np.float64)
        if self.kind is ToolKind.BALL:
            radius = self.radius_mm
            offset = radius - np.sqrt(np.maximum(radius * radius - distance * distance, 0.0))
        elif self.kind is ToolKind.BULL and self.corner_radius_mm > 0.0:
            corner = self.corner_radius_mm
            beyond = np.maximum(distance - self.footprint_radius_mm, 0.0)
            offset = corner - np.sqrt(np.maximum(corner * corner - beyond * beyond, 0.0))
        else:
            offset = np.zeros_like(distance)
        offset = np.where(distance <= self.reach_radius_mm + _REACH_EPS_MM, offset, np.inf)
        return float(offset) if np.ndim(offset) == 0 else offset

    def cusp_height_mm(self, stepover_mm: float) -> float:
        """Residual ridge left between two parallel passes a `stepover_mm` apart.

        The middle between two passes is `stepover / 2` from both axes, so the ridge the later pass
        leaves standing is exactly the profile height there: the classic scallop formula
        `R - sqrt(R^2 - (s/2)^2)` for a ball nose, 0 while a flat or bull nose spans the stepover, and
        `inf` when the passes do not overlap at all -- then the ridge is full height, not a scallop.
        """

        return float(self.profile_height_mm(max(stepover_mm, 0.0) / 2.0))

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


@dataclass(frozen=True, slots=True)
class ToolPreset:
    """A named tool the panel offers as a starting point.

    A preset is a **shortcut, not a second configuration system** (CONTRIBUTING: user facing switches
    live in a ParameterSet): picking one copies its values into the very fields the user edits by hand,
    so the request that reaches the API is the same in both cases and nothing has to be read back out
    of the preset later. That is also why the values carry every tool parameter key -- the panel fills
    the whole group from them.
    """

    id: str
    label: str
    description: str
    values: Mapping[str, Any]

    def describe(self) -> dict[str, Any]:
        """JSON form published in the catalogue."""

        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "values": dict(self.values),
        }


#: The tool library. Every entry has to be a *valid* tool that plans (tests build each one and run a
#: plan with it), because a preset that the API rejects would be a broken button in the panel.
TOOL_LIBRARY: tuple[ToolPreset, ...] = (
    ToolPreset(
        id="flat_d6",
        label="平底 D6（通用）",
        description="默认的通用平底刀：面铣与型腔都用它，刀刃与刀柄取自动值",
        values={
            "kind": "flat", "diameter_mm": 6.0, "length_mm": 30.0, "corner_radius_mm": 0.0,
            "taper_angle_deg": 0.0, "flute_length_mm": 0.0, "shank_diameter_mm": 0.0,
        },
    ),
    ToolPreset(
        id="flat_d10",
        label="平底 D10（面铣）",
        description="大刀面铣：切宽可以开大，同样的区域刀轨数明显更少",
        values={
            "kind": "flat", "diameter_mm": 10.0, "length_mm": 40.0, "corner_radius_mm": 0.0,
            "taper_angle_deg": 0.0, "flute_length_mm": 0.0, "shank_diameter_mm": 0.0,
        },
    ),
    ToolPreset(
        id="ball_d6",
        label="球头 D6（精加工）",
        description="球头刀：平面足迹是一个点，覆盖率天然很低，用来对比刀路与残留",
        values={
            "kind": "ball", "diameter_mm": 6.0, "length_mm": 30.0, "corner_radius_mm": 0.0,
            "taper_angle_deg": 0.0, "flute_length_mm": 12.0, "shank_diameter_mm": 0.0,
        },
    ),
    ToolPreset(
        id="bull_d10",
        label="圆鼻 D10 Rc2",
        description="圆鼻刀：底平带 R2 圆角，粗精之间，贴壁间隙按整段切深的外伸半径算",
        values={
            "kind": "bull", "diameter_mm": 10.0, "length_mm": 40.0, "corner_radius_mm": 2.0,
            "taper_angle_deg": 0.0, "flute_length_mm": 0.0, "shank_diameter_mm": 0.0,
        },
    ),
    ToolPreset(
        id="taper_d6_15",
        label="锥度 15°（D6）",
        description="锥度刀：刀刃 8 mm、刀柄比锥面顶端细（缩颈），深腔里靠贴壁间隙让出锥面",
        values={
            "kind": "flat", "diameter_mm": 6.0, "length_mm": 30.0, "corner_radius_mm": 0.0,
            "taper_angle_deg": 15.0, "flute_length_mm": 8.0, "shank_diameter_mm": 8.0,
        },
    ),
    ToolPreset(
        id="micro_d3",
        label="细小 D3（清角）",
        description="小刀清角：刀柄比刀刃粗，切深超过 6 mm 刀刃后刀柄会顶到壁（碰撞检查会报）",
        values={
            "kind": "flat", "diameter_mm": 3.0, "length_mm": 25.0, "corner_radius_mm": 0.0,
            "taper_angle_deg": 0.0, "flute_length_mm": 6.0, "shank_diameter_mm": 4.0,
        },
    ),
)


def tool_library() -> list[dict[str, Any]]:
    """The tool library in the same JSON shape the rest of the catalogue uses."""

    return [preset.describe() for preset in TOOL_LIBRARY]
