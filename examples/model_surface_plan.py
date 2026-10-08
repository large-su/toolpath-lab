"""不用界面的导入模型示例：造一块波形板，在它的表面上排刀路（含分层粗加工）。

在项目根目录运行：

    python examples/model_surface_plan.py

它做的事情和界面里"导入 STL"完全一样，只是绕过了 HTTP 与三维显示：

1. 用三角网格拼一块正弦起伏的板（`Mesh`）；
2. 把一个 STL 二进制塞进模型库（`ModelLibrary.add`）——界面上传时后端做的也是这一步；
3. 区域用模型的投影轮廓，加工面用模型的 Z-map 高度场，毛坯用模型的最小六面体包容体；
4. 按切深分层粗加工 + 沿曲面精加工，打印统计、导出 NC。

想改成读真实文件：把第 2 步换成 `library.add("part.stl", Path("part.stl").read_bytes())`。
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from toolpath_lab.core.mesh import Mesh, ModelLibrary
from toolpath_lab.core.path import MoveKind
from toolpath_lab.core.region import build_region
from toolpath_lab.core.stock import build_stock
from toolpath_lab.core.surface import build_surface
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.planning import run_plan

SIDE_MM = 80.0
AMPLITUDE_MM = 4.0
WAVE_COUNT = 1.5
#: 毛坯顶面相对零件最高点抬高多少（相当于留出加工余量）。
STOCK_MARGIN_TOP_MM = 6.0
#: 每层切多深。
DEPTH_PER_PASS_MM = 2.0


def wavy_plate(side: float = SIDE_MM, steps: int = 40) -> Mesh:
    """一块正弦起伏的板：z = amplitude * sin(2π * x / side * wave_count)。"""

    axis = np.linspace(-side / 2.0, side / 2.0, steps + 1)
    grid_x, grid_y = np.meshgrid(axis, axis)
    grid_z = AMPLITUDE_MM * np.sin(2.0 * np.pi * grid_x / side * WAVE_COUNT)

    def corner(row, column):
        return (float(grid_x[row, column]), float(grid_y[row, column]), float(grid_z[row, column]))

    triangles = []
    for row in range(steps):
        for column in range(steps):
            a = corner(row, column)
            b = corner(row, column + 1)
            c = corner(row + 1, column + 1)
            d = corner(row + 1, column)
            triangles.append([a, b, c])
            triangles.append([a, c, d])
    return Mesh(np.array(triangles, dtype=np.float64))


def as_binary_stl(mesh: Mesh) -> bytes:
    """把网格写成二进制 STL（只是为了走一遍真正的上传格式）。"""

    header = b"toolpath-lab example".ljust(80, b"\0")
    body = bytearray(struct.pack("<I", mesh.triangle_count))
    for triangle in mesh.triangles:
        body += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vertex in triangle:
            body += struct.pack("<3f", *vertex)
        body += struct.pack("<H", 0)
    return header + bytes(body)


def main() -> None:
    library = ModelLibrary()
    model = library.add("wavy_plate.stl", as_binary_stl(wavy_plate()))
    print(f"模型: {model.name} ({model.id}) {model.triangle_count} 个三角形, "
          f"尺寸 {tuple(round(v, 1) for v in model.mesh.size_mm)} mm")

    region = build_region("model", {"outline": "box"}, model=model)
    surface = build_surface(
        "model", {"resolution_mm": 1.0, "pick": "top"}, model=model
    )
    stock = build_stock(
        "model", {"margin_top_mm": STOCK_MARGIN_TOP_MM}, model=model
    )
    print(f"区域: {region.id} -> {len(region.boundary())} 个顶点")
    print(f"加工面: {surface.note()}")
    print(f"毛坯: {stock.note()}")

    outcome = run_plan(
        planner_id="raster",
        tool=Tool(ToolKind.BALL, diameter_mm=8.0, length_mm=40.0),
        region=region,
        surface=surface,
        stock=stock,
        parameters={
            "mode": "zigzag",
            "stepover_mm": 2.0,
            "direction_deg": 0.0,
            "feed_mm_per_min": 1200.0,
            "sample_step_mm": 0.5,
            "depth_per_pass_mm": DEPTH_PER_PASS_MM,
            "finish_pass": True,
        },
    )
    toolpath = outcome.toolpath
    statistics = toolpath.statistics()

    passes = [move for move in toolpath.moves
              if move.kind is MoveKind.CUT and move.pass_index >= 0]
    levels = sorted({round(float(move.points[0][2]), 3) for move in passes})
    heights = np.array(
        [point[2] for move in passes for point in move.points]
    )
    print(f"刀轨 {statistics['pass_count']} 条, 运动段 {statistics['move_count']}, "
          f"刀点 {statistics['point_count']}")
    print(f"粗加工层高 Z: {[level for level in levels if level > 0.0]}")
    print(f"切削长度 {statistics['cut_length_mm']:.1f} mm, "
          f"预计工时 {statistics['estimated_time_s']:.1f} s")
    print(f"刀点高度范围 Z {heights.min():.2f} ~ {heights.max():.2f} mm")
    for warning in outcome.warnings:
        print(f"警告: {warning}")
    for note in toolpath.notes:
        print(f"说明: {note}")

    output = Path(__file__).resolve().parent / "model_surface_demo.nc"
    output.write_text(
        toolpath_to_gcode(toolpath, program_name="TOOLPATH_MODEL_SURFACE"), encoding="utf-8"
    )
    print(f"已写出 {output.name}")


if __name__ == "__main__":
    main()
