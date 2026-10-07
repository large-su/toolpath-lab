"""DXF（CAD 交换格式）刀路导出。

把刀路写成 AutoCAD R12 版 DXF：每个运动段是一条 POLYLINE，并按运动类型分图层，
CUT 切削 / LINK 连接 / RAPID 快移，配不同颜色，在 CAD 里一眼就能区分走刀与抬刀。
AutoCAD、中望 CAD、浩辰 CAD、UG NX 等支持 DXF 的软件都能直接打开查看刀路。
"""

from __future__ import annotations

from toolpath_lab.core.path import MoveKind, Toolpath

#: 运动类型 → (图层名, 颜色号)。DXF 颜色：1=红 2=黄 3=绿 4=青 5=蓝 7=白/黑
_LAYER_BY_KIND: dict[MoveKind, tuple[str, int]] = {
    MoveKind.CUT: ("CUT", 7),
    MoveKind.LINK: ("LINK", 3),
    MoveKind.RAPID: ("RAPID", 1),
}
_LAYER_ORDER = ("CUT", "LINK", "RAPID")


def toolpath_to_dxf(
    toolpath: Toolpath,
    *,
    description: str = "",
    decimals: int = 3,
) -> str:
    """把一条刀路渲染成 R12 DXF 文本。"""

    number = f"{{:.{decimals}f}}"
    groups: list[str] = []

    # -- HEADER ------------------------------------------------------------
    groups += ["0", "SECTION", "2", "HEADER",
               "9", "$ACADVER", "1", "AC1009",
               "9", "$INSUNITS", "70", "4",
               "0", "ENDSEC"]

    # -- 注释（999 组码，CAD 忽略） ----------------------------------------
    comments = ["ToolpathLab DXF export"]
    if description:
        comments.extend(line for line in description.splitlines() if line)
    comments.extend(toolpath.notes)
    groups += ["0", "SECTION", "2", "COMMENTS"]
    for comment in comments:
        groups += ["999", comment[:255]]
    groups += ["0", "ENDSEC"]

    # -- TABLES / 图层表 ----------------------------------------------------
    used = {_LAYER_BY_KIND[move.kind][0] for move in toolpath.moves}
    layers = [name for name in _LAYER_ORDER if name in used]
    groups += ["0", "SECTION", "2", "TABLES",
               "0", "TABLE", "2", "LAYER",
               "70", str(len(layers))]
    for name in layers:
        color = _LAYER_BY_KIND[next(k for k, v in _LAYER_BY_KIND.items() if v[0] == name)][1]
        groups += ["0", "LAYER", "2", name, "70", "0", "62", str(color), "6", "CONTINUOUS"]
    groups += ["0", "ENDTAB", "0", "ENDSEC"]

    # -- ENTITIES -----------------------------------------------------------
    groups += ["0", "SECTION", "2", "ENTITIES"]
    for move in toolpath.moves:
        name = _LAYER_BY_KIND[move.kind][0]
        groups += ["0", "POLYLINE", "8", name, "66", "1", "70", "0"]
        for point in move.points:
            x, y, z = (float(value) for value in point)
            groups += ["0", "VERTEX", "8", name,
                       "10", number.format(x), "20", number.format(y),
                       "30", number.format(z)]
        groups += ["0", "SEQEND"]
    groups += ["0", "ENDSEC"]

    groups += ["0", "EOF"]
    return "\n".join(groups) + "\n"
