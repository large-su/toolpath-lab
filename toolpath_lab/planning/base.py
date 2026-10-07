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
from math import isfinite
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import PlanningError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)
from toolpath_lab.core.path import Move, MoveKind, Toolpath, retract_move
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool
from toolpath_lab.planning.feeds import DEFAULT_CORNER_ANGLE_DEG, DEFAULT_CORNER_FEED_RATIO
from toolpath_lab.planning.geometry2d import ensure_ccw

#: Default distance (mm) the tool lifts above the top face of the workpiece when moving rapidly.
SAFE_HEIGHT_MM = 5.0
#: Default feed rate (mm/min) for rapid moves.
RAPID_FEED_MM_PER_MIN = 5000.0

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

    def approach_move_down(self, point: NDArray[np.float64]) -> Move:
        """Plunge from the safe height down to this point."""

        target = np.asarray(point, dtype=np.float64).reshape(3)
        start = np.array([target[0], target[1], self.safe_height_mm], dtype=np.float64)
        return Move(MoveKind.RAPID, np.vstack([start, target]), self.rapid_feed_mm_per_min,
                    label="下刀")

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
