"""Time parameterisation of toolpaths (playback and machining time)."""

from toolpath_lab.simulation.timeline import Timeline, TimelineState, build_timeline
from toolpath_lab.simulation.material import HeightField

__all__ = ["HeightField", "Timeline", "TimelineState", "build_timeline"]
