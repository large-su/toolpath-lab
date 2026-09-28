"""Time parameterisation of toolpaths (playback and machining time)."""

from toolpath_lab.simulation.timeline import Timeline, TimelineState, build_timeline
from toolpath_lab.simulation.stock import StockSpec, StockState, stock_spec_for

__all__ = [
    "Timeline", "TimelineState", "build_timeline",
    "StockSpec", "StockState", "stock_spec_for",
]
