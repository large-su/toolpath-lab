"""仿真层：时间参数化（播放）与毛坯切除仿真（材料去除）。"""

from __future__ import annotations

from toolpath_lab.simulation.cut_sim import (
    HeightField,
    SimulationFrame,
    SimulationResult,
    build_height_field,
    cut_move,
    simulate_toolpath,
)
from toolpath_lab.simulation.timeline import Timeline, TimelineState, build_timeline

__all__ = [
    "HeightField",
    "SimulationFrame",
    "SimulationResult",
    "Timeline",
    "TimelineState",
    "build_height_field",
    "build_timeline",
    "cut_move",
    "simulate_toolpath",
]
