"""CSV point table export.

One tool point per row, easy to drop into Excel / pandas for statistics or plots:

    move_index,pass_index,kind,feed_mm_per_min,point_index,x_mm,y_mm,z_mm

Deliberately kept **pure ASCII** (column names and values are English, kind is cut / link / rapid):
UTF-8 without a BOM is more portable than "a BOM so Excel recognises it", and csv.reader, pandas and
scripts need no special encoding handling. pass_index is -1 for non-cutting moves; the first point of
a move may coincide with the last point of the previous one (moves join end to end), which is simply
what the toolpath model looks like, so nothing is deduplicated.

The recipe for a new export format is in docs/extending.md section 4: write a pure function here,
export it in export/__init__.py, and add a branch to _route_api in server/app.py.
"""

from __future__ import annotations

from toolpath_lab.core.path import Toolpath

#: Column names; the order is the output order.
CSV_COLUMNS: tuple[str, ...] = (
    "move_index",
    "pass_index",
    "kind",
    "feed_mm_per_min",
    "point_index",
    "x_mm",
    "y_mm",
    "z_mm",
)


def toolpath_to_csv(toolpath: Toolpath, *, decimals: int = 3) -> str:
    """Render a toolpath as a CSV point table, one tool point per row."""

    number = f"{{:.{decimals}f}}"
    lines = [",".join(CSV_COLUMNS)]
    for move_index, move in enumerate(toolpath.moves):
        feed = number.format(move.feed_mm_per_min)
        for point_index, point in enumerate(move.points):
            x, y, z = (float(value) for value in point)
            lines.append(
                ",".join(
                    (
                        str(move_index),
                        str(move.pass_index),
                        move.kind.value,
                        feed,
                        str(point_index),
                        number.format(x),
                        number.format(y),
                        number.format(z),
                    )
                )
            )
    return "\n".join(lines) + "\n"
