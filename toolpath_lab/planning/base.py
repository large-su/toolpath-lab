"""Shared contract for toolpath strategies.

A strategy receives a PlanningContext (tool + region + its own parameters) and returns a Toolpath.
It knows nothing about HTTP, JSON or the UI, so it can be tested and called without the service.

"Retract height" and "rapid feed" are motion parameters every strategy needs, but their values are
the caller's choice, so they are declared once in MOTION_PARAMETERS and merged into each strategy's
own ParameterSet; when a strategy does not declare them, PlanningContext falls back to the defaults
below (third party plugins are therefore not tripped up by this convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, cos, isfinite, pi, radians, sin, tan
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.entry import ENTRY_LABEL_SUFFIX, ENTRY_MODE_LABELS
from toolpath_lab.planning.feeds import DEFAULT_CORNER_ANGLE_DEG, DEFAULT_CORNER_FEED_RATIO
from toolpath_lab.planning.geometry2d import ensure_ccw
from toolpath_lab.planning.stepdown import DEFAULT_DEPTH_MM, DEFAULT_STEPDOWN_MM

#: Default distance (mm) the tool lifts above the top face of the workpiece when moving rapidly.
SAFE_HEIGHT_MM = 5.0
#: Default feed rate (mm/min) for rapid moves.
RAPID_FEED_MM_PER_MIN = 5000.0

#: Entry modes: how the tool gets down to the cutting plane of a layer.
ENTRY_MODES: tuple[Choice, ...] = (
    Choice("plunge", "直插（默认）"),
    Choice("ramp", "斜坡"),
    Choice("helix", "螺旋"),
)
DEFAULT_ENTRY_MODE = "plunge"
DEFAULT_RAMP_ANGLE_DEG = 10.0
DEFAULT_HELIX_RADIUS_MM = 1.5

#: Motion parameters shared by every strategy: merge them into your ParameterSet to expose them.
#: Corner feed reduction is in here too, so even a third party strategy gets it for free (see
#: planning/feeds.py); leaving `corner_angle_deg` at 0 keeps the plain geometric toolpath.
MOTION_PARAMETERS: ParameterSet = ParameterSet(
    (
        spec("safe_height_mm", "安全高度", K.FLOAT, SAFE_HEIGHT_MM, minimum=0.0,
             maximum=200.0, step=0.5, unit="mm", group="刀路",
             help="快移时抬到工件上表面（Z = 0）之上的高度；0 表示不抬刀"),
        spec("rapid_feed_mm_per_min", "快移速度", K.FLOAT, RAPID_FEED_MM_PER_MIN,
             minimum=100.0, maximum=50000.0, step=100.0, unit="mm/min", group="刀路",
             help="抬刀 / 横移 / 下刀的进给速度，会计入预计工时"),
        spec("corner_angle_deg", "拐角减速起始角", K.FLOAT, DEFAULT_CORNER_ANGLE_DEG,
             minimum=0.0, maximum=180.0, step=5.0, unit="°", group="刀路",
             help="切削段转角超过它就开始降速；0 表示关闭（圆滑曲线因此不受影响）"),
        spec("corner_feed_ratio", "拐角最低进给", K.FLOAT, DEFAULT_CORNER_FEED_RATIO,
             minimum=0.05, maximum=1.0, step=0.05, unit="×", group="刀路",
             help="180° 折返处降到编程进给的这个比例；起始角为 0 时不生效"),
        spec("depth_mm", "总深度", K.FLOAT, DEFAULT_DEPTH_MM, minimum=0.0, maximum=200.0,
             step=0.5, unit="mm", group="刀路",
             help="工件要切到的深度；0 表示只在加工面走一层（默认）"),
        spec("stepdown_mm", "吃刀深度", K.FLOAT, DEFAULT_STEPDOWN_MM, minimum=0.1,
             maximum=50.0, step=0.5, unit="mm", group="刀路",
             help="每层下刀多少；总深度不是它的整数倍时，最后一层取剩余量"),
        spec("entry_mode", "进刀方式", K.CHOICE, DEFAULT_ENTRY_MODE, group="刀路",
             choices=ENTRY_MODES,
             help="直插最快；斜坡与螺旋是切入材料更常见的做法，两者用切削进给"),
        spec("ramp_angle_deg", "斜坡角度", K.FLOAT, DEFAULT_RAMP_ANGLE_DEG, minimum=1.0,
             maximum=45.0, step=1.0, unit="°", group="刀路",
             help="斜坡与螺旋的下切角度；角度越小，每层需要的引入距离越长"),
        spec("helix_radius_mm", "螺旋半径", K.FLOAT, DEFAULT_HELIX_RADIUS_MM, minimum=0.2,
             maximum=20.0, step=0.1, unit="mm", group="刀路",
             help="螺旋进刀的圆半径；超过刀具贴壁间隙时会被压到该间隙以内"),
    )
)


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """Everything one plan needs, without depending on any transport layer."""

    tool: Tool
    region: RegionShape
    parameters: Mapping[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # -- parameters --------------------------------------------------------
    @property
    def feed_mm_per_min(self) -> float:
        return float(self.parameters["feed_mm_per_min"])

    @property
    def safe_height_mm(self) -> float:
        """Retract height; uses the safe_height_mm parameter when the strategy declares it."""

        return float(self.parameters.get("safe_height_mm", SAFE_HEIGHT_MM))

    @property
    def rapid_feed_mm_per_min(self) -> float:
        """Rapid feed; uses the rapid_feed_mm_per_min parameter when the strategy declares it."""

        return float(self.parameters.get("rapid_feed_mm_per_min", RAPID_FEED_MM_PER_MIN))

    @property
    def corner_angle_deg(self) -> float:
        """Turn angle above which cutting feeds ramp down; 0 means corner slowdown is off."""

        return float(self.parameters.get("corner_angle_deg", DEFAULT_CORNER_ANGLE_DEG))

    @property
    def corner_feed_ratio(self) -> float:
        """Feed factor at a full reversal; uses the default when the strategy declares no such key."""

        return float(self.parameters.get("corner_feed_ratio", DEFAULT_CORNER_FEED_RATIO))

    @property
    def depth_mm(self) -> float:
        """Total depth to machine; 0 means a single layer on the machining plane."""

        return float(self.parameters.get("depth_mm", DEFAULT_DEPTH_MM))

    @property
    def stepdown_mm(self) -> float:
        """Depth of cut per layer; uses the default when the strategy declares no such key."""

        return float(self.parameters.get("stepdown_mm", DEFAULT_STEPDOWN_MM))

    @property
    def cutting_radius_mm(self) -> float:
        """Radius the path must keep from the outline: the cutter's reach over the whole cut.

        For a flat mill that is just its radius. A ball or bull nose tool is narrower at the tip, so
        the offset has to cover everything it sweeps from the machining plane down to the deepest
        layer -- otherwise it would gouge the wall down there.
        """

        return self.tool.wall_clearance_mm(self.depth_mm)

    @property
    def entry_mode(self) -> str:
        """How the tool descends to a layer: "plunge", "ramp" or "helix"."""

        return str(self.parameters.get("entry_mode", DEFAULT_ENTRY_MODE))

    @property
    def ramp_angle_deg(self) -> float:
        """Descent angle of a ramp or helix entry."""

        return float(self.parameters.get("ramp_angle_deg", DEFAULT_RAMP_ANGLE_DEG))

    @property
    def helix_radius_mm(self) -> float:
        """Helix radius, never wider than the wall clearance (a wider helix would cut the wall)."""

        wanted = float(self.parameters.get("helix_radius_mm", DEFAULT_HELIX_RADIUS_MM))
        limit = max(self.cutting_radius_mm * 0.9, 0.05)
        return min(wanted, limit)

    @property
    def entry_depth_mm(self) -> float:
        """How deep one entry descends: the depth of cut of a single layer."""

        if self.depth_mm <= 0.0:
            return max(self.stepdown_mm, 0.0)
        return max(min(self.stepdown_mm, self.depth_mm), 0.0)

    # -- geometry ----------------------------------------------------------
    @property
    def boundary(self) -> NDArray[np.float64]:
        """Counter-clockwise region outline, shape (N, 2)."""

        return ensure_ccw(self.region.boundary())

    def to_positions(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """Lift planar points (N, 2) to workpiece coordinates (N, 3) on the machining plane Z = 0."""

        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.column_stack((planar, np.zeros(planar.shape[0], dtype=np.float64)))

    def warn(self, message: str) -> None:
        """Record a non-fatal warning, returned with the response and shown in the UI."""

        if message not in self.warnings:
            self.warnings.append(message)

    # -- move construction -------------------------------------------------
    def cut_move(self, points_xy: NDArray[np.float64], *, pass_index: int, label: str) -> Move:
        return Move(
            MoveKind.CUT,
            self.to_positions(points_xy),
            self.feed_mm_per_min,
            pass_index=pass_index,
            label=label,
        )

    def link_move(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> Move:
        return Move(
            MoveKind.LINK,
            np.vstack([start, end]),
            self.feed_mm_per_min,
            label="刀间连接",
        )

    def rapid_between(self, start: NDArray[np.float64], end: NDArray[np.float64]) -> Move:
        return retract_move(start, end, self.safe_height_mm, self.rapid_feed_mm_per_min)

    def entry_moves(
        self,
        point: NDArray[np.float64],
        direction: NDArray[np.float64] | None = None,
    ) -> tuple[Move, ...]:
        """Moves that bring the tool down to this point: a plunge, a ramp or a helix.

        The entry is built relative to the machining plane of its own layer, so with step-down a ramp
        or helix spans exactly the depth of cut of that layer once the layer shift is applied: the
        tool comes down from the previous floor (and, on the top layer, through open air) to the floor
        it is about to cut.

        The ramp walks backwards along the cutting direction and the helix turns around a centre
        offset the same way, so both end on the cut start and merge into it. Both use the cutting feed
        because both remove material, and neither is clipped against the region outline: a shallow
        angle over a deep cut asks for a long entry, which the plan's notes report so the length is
        never a surprise.
        """

        target = np.asarray(point, dtype=np.float64).reshape(3)
        plane = np.array([target[0], target[1], self.safe_height_mm], dtype=np.float64)
        depth = self.entry_depth_mm
        if self.entry_mode not in ("ramp", "helix") or depth <= 1e-9:
            return (
                Move(MoveKind.RAPID, np.vstack([plane, target]), self.rapid_feed_mm_per_min,
                     label="下刀"),
            )

        step = self._planar_direction(direction)
        if self.entry_mode == "helix":
            points = self._helix_points(target, step, depth)
        else:
            travel = depth / max(tan(radians(self.ramp_angle_deg)), 1e-6)
            points = np.array(
                [
                    [target[0] - step[0] * travel, target[1] - step[1] * travel, depth],
                    target,
                ],
                dtype=np.float64,
            )
        # The label is the shared vocabulary: planning/entry.py reads it back to name the mode in the
        # plan's notes, so both sides spell it the same way.
        label = f"{ENTRY_MODE_LABELS[self.entry_mode]}{ENTRY_LABEL_SUFFIX}"
        approach = Move(
            MoveKind.RAPID,
            np.vstack([plane, np.array([points[0][0], points[0][1], depth], dtype=np.float64)]),
            self.rapid_feed_mm_per_min,
            label="下刀",
        )
        return (
            approach,
            Move(MoveKind.CUT, points, self.feed_mm_per_min, pass_index=-1, label=label),
        )

    def _planar_direction(self, direction: NDArray[np.float64] | None) -> NDArray[np.float64]:
        """Unit direction of the first cutting segment; +X when the caller has none."""

        fallback = np.array([1.0, 0.0], dtype=np.float64)
        if direction is None:
            return fallback
        flat = np.asarray(direction, dtype=np.float64).reshape(-1)[:2]
        length = float(np.linalg.norm(flat))
        return fallback if length <= 1e-9 else flat / length

    def _helix_points(
        self, target: NDArray[np.float64], step: NDArray[np.float64], depth: float
    ) -> NDArray[np.float64]:
        """A helix of whole turns ending on the target, descending `depth` on the way.

        Whole turns matter: they bring the tool back to the same XY it started above, so the entry
        ends exactly where the cut begins. The pitch follows the ramp angle, so both entry modes
        descend at the same angle.
        """

        radius = self.helix_radius_mm
        pitch = 2.0 * pi * radius * max(tan(radians(self.ramp_angle_deg)), 1e-6)
        turns = max(1.0, ceil(depth / max(pitch, 1e-6)))
        centre = np.array([target[0] - step[0] * radius, target[1] - step[1] * radius])
        samples = max(16, int(48 * turns))
        angles = np.linspace(-2.0 * pi * turns, 0.0, samples + 1)
        xy = centre + radius * np.column_stack((np.cos(angles), np.sin(angles)))
        z = depth * (1.0 - np.linspace(0.0, 1.0, samples + 1))
        points = np.column_stack((xy, z))
        points[-1] = target
        return points

    def retract_move_up(self, point: NDArray[np.float64]) -> Move:
        """Retract from this point up to the safe height."""

        start = np.asarray(point, dtype=np.float64).reshape(3)
        end = np.array([start[0], start[1], self.safe_height_mm], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, end]), self.rapid_feed_mm_per_min,
                    label="抬刀")


class Planner:
    """Base class of every toolpath strategy.

    A subclass declares id (the identifier in the API), label (the name in the UI), description and a
    ParameterSet, then implements plan().
    """

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()

    def plan(self, context: PlanningContext) -> Toolpath:
        raise NotImplementedError

    @staticmethod
    def require_positive(value: float, name: str) -> float:
        if not isfinite(value) or value <= 0.0:
            raise PlanningError(f"{name} 必须是有限正数（收到 {value!r}）")
        return value
