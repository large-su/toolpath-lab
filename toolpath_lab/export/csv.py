"""CSV 点表导出。

一行一个刀点，方便丢进 Excel / pandas 做统计或画图：

    move_index,pass_index,kind,feed_mm_per_min,point_index,x_mm,y_mm,z_mm

刻意保持**纯 ASCII**（列名与取值都是英文，kind 用 cut / link / rapid）：不带 BOM 的 UTF-8
比"带 BOM 才能被 Excel 认出来"更通用，csv.reader、pandas 与各种脚本都不需要额外处理编码。
非切削段的 pass_index 是 -1；每段运动的第一个点与上一段的最后一个点可能重合（运动段首尾相接），
这是刀路模型本身的样子，不做去重。

新增导出格式的套路见 docs/extending.md §4：这里写一个纯函数，在 export/__init__.py 导出，
再在 server/app.py 的 _route_api 里加一个分支。
"""

from __future__ import annotations

from toolpath_lab.core.path import Toolpath

#: 列名，顺序即输出顺序。
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
    """把一条刀路渲染成"一个刀点一行"的 CSV 点表。"""

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
