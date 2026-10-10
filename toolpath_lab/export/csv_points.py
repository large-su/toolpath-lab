"""CSV 导出：每个刀点一行。

给人看和给后处理用的扁平表：一行一个坐标点，带上它属于哪一段运动、
那一段是什么类型、进给多少、累计走了多远。适合丢进 Excel 画图，
或者接一个只认点表的下位机。

有意**不**写成"一段一行"：一刀可能有上千个点，按段聚合会丢掉逐点的 Z 与进给变化。
点表是无损的——按 `move_index` 分组就能还原成一条完整的刀路。
"""

from __future__ import annotations

from toolpath_lab.core.path import MoveKind, Toolpath

#: 固定表头。用英文列名，方便脚本按名字取列；界面上的中文标签在文档里对照。
COLUMNS = (
    "move_index",
    "move_kind",
    "pass_index",
    "label",
    "point_index",
    "x_mm",
    "y_mm",
    "z_mm",
    "feed_mm_per_min",
    "move_length_mm",
    "segment_length_mm",
    "cumulative_cut_mm",
    "duration_s",
)


def toolpath_to_csv(
    toolpath: Toolpath,
    *,
    decimals: int = 4,
    include_rapid: bool = True,
) -> str:
    """把一条刀路渲染成 CSV 点表。

    `include_rapid=False` 时只输出切削与连接点（不含抬刀/横移/下刀），
    得到的就是纯粹的"被切出来的轨迹"。
    """

    number = f"{{:.{decimals}f}}"
    lines = [",".join(COLUMNS)]
    # 累计切削长度只统计真正在切的点，跳过快移。
    cumulative_cut = 0.0
    for move_index, move in enumerate(toolpath.moves):
        if move.kind is MoveKind.RAPID and not include_rapid:
            continue
        previous: tuple[float, float, float] | None = None
        for point_index, point in enumerate(move.points):
            x, y, z = (float(value) for value in point)
            segment = 0.0
            if previous is not None:
                segment = ((x - previous[0]) ** 2 + (y - previous[1]) ** 2
                           + (z - previous[2]) ** 2) ** 0.5
            if move.kind is not MoveKind.RAPID:
                cumulative_cut += segment
            lines.append(
                ",".join(
                    (
                        str(move_index),
                        move.kind.value,
                        str(move.pass_index),
                        _quote(move.label),
                        str(point_index),
                        number.format(x),
                        number.format(y),
                        number.format(z),
                        number.format(move.feed_mm_per_min),
                        number.format(move.length_mm),
                        number.format(segment),
                        number.format(cumulative_cut),
                        number.format(segment / move.feed_mm_per_min * 60.0),
                    )
                )
            )
            previous = (x, y, z)
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    """标签里可能有逗号或中文标点，必要时加引号。"""

    if any(character in value for character in (",", '"', "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value