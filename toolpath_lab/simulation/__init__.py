"""Time parameterisation of toolpaths (playback and machining time),
plus the material removal (Z-map) simulation added on the development branch."""

from toolpath_lab.simulation.removal import (
    CLEAN_TOLERANCE_MM,
    RemovalReport,
    simulate_removal,
)
from toolpath_lab.simulation.timeline import Timeline, TimelineState, build_timeline

__all__ = [
    "CLEAN_TOLERANCE_MM",
    "RemovalReport",
    "Timeline",
    "TimelineState",
    "build_timeline",
    "simulate_removal",
]
