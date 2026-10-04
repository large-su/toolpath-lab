"""刀路的时间参数化（播放）与材料切除仿真（毛坯成形）。"""

from toolpath_lab.simulation.material import (
    CELLS_PER_PASS,
    FRAME_CELL_BUDGET,
    MAX_GRID_CELLS,
    MIN_FRAME_BUDGET,
    HeightField,
    MaterialRemoval,
    StockSettings,
    build_height_field,
    carve,
    resample_points,
    simulate_material_removal,
    stock_parameters,
)
from toolpath_lab.simulation.timeline import Timeline, TimelineState, build_timeline

__all__ = [
    "CELLS_PER_PASS",
    "FRAME_CELL_BUDGET",
    "MAX_GRID_CELLS",
    "MIN_FRAME_BUDGET",
    "HeightField",
    "MaterialRemoval",
    "StockSettings",
    "Timeline",
    "TimelineState",
    "build_height_field",
    "build_timeline",
    "carve",
    "resample_points",
    "simulate_material_removal",
    "stock_parameters",
]
