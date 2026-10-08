"""TXT 导出：把刀路以纯文本方式输出，便于查看和保存。"""

from __future__ import annotations

from typing import Any

from toolpath_lab.core.path import Toolpath


def toolpath_to_txt(
    toolpath: Toolpath,
    *,
    program_name: str = "TOOLPATH_LAB",
    description: str = "",
    decimals: int = 3,
    extra_header: dict[str, Any] | None = None,
) -> str:
    """把一条刀路渲染成纯文本格式，适合下载查看。"""

    lines: list[str] = [f"Toolpath: {program_name}"]
    if description:
        for chunk in description.splitlines():
            lines.append(f"Description: {chunk}")
    if extra_header:
        for key, value in extra_header.items():
            lines.append(f"{key}: {value}")
    for note in toolpath.notes:
        lines.append(f"Note: {note}")

    lines.append(f"Move count: {len(toolpath.moves)}")
    lines.append(f"Total length mm: {toolpath.total_length_mm:.{decimals}f}")
    lines.append(f"Estimated time s: {toolpath.estimated_time_s:.{decimals}f}")
    lines.append(f"Cut length mm: {toolpath.cut_length_mm:.{decimals}f}")
    lines.append(f"Rapid length mm: {toolpath.rapid_length_mm:.{decimals}f}")
    lines.append("")

    for index, move in enumerate(toolpath.moves, start=1):
        lines.append(
            f"Move {index}: kind={move.kind.value}, "
            f"feed_mm_per_min={move.feed_mm_per_min:.{decimals}f}, "
            f"length_mm={move.length_mm:.{decimals}f}"
        )
        for point_index, point in enumerate(move.points, start=1):
            x, y, z = (float(value) for value in point)
            lines.append(
                f"  point {point_index}: x={x:.{decimals}f}, y={y:.{decimals}f}, z={z:.{decimals}f}"
            )

    return "\n".join(lines) + "\n"