"""JSON 导出：刀路的无损快照。

G-code 是给控制器看的，CSV 是给人看表用的；这个格式给**脚本**看：
把刀具、区域、参数、运动段、统计和时间轴原样写成一个 JSON 文档，
让外部程序（比如批量对比不同参数的结果、接自己的后处理）可以直接消费。

与 `POST /api/plan` 的响应相比，这里多了导出时刻的上下文（版本、时间戳），
并且把点坐标保留为完整精度而不是接口里那样截断到 4 位小数。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from toolpath_lab import __version__
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool


def toolpath_to_json(
    toolpath: Toolpath,
    *,
    tool: Tool | None = None,
    region: RegionShape | None = None,
    request: dict[str, Any] | None = None,
    decimals: int | None = None,
) -> str:
    """把一条刀路渲染成 JSON 文本。

    `decimals` 给定时对坐标做四舍五入（体积更小）；默认 `None` 表示保留全精度。
    """

    def number(value: float) -> float:
        return float(value) if decimals is None else round(float(value), decimals)

    payload: dict[str, Any] = {
        "format": "toolpath-lab/toolpath",
        "format_version": 1,
        "generator": {"name": "ToolpathLab", "version": __version__},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "planner": {
            "id": toolpath.planner,
            "label": toolpath.planner_label,
            "notes": list(toolpath.notes),
        },
    }
    if request is not None:
        payload["request"] = request
    if tool is not None:
        payload["tool"] = tool.describe()
    if region is not None:
        described = region.describe()
        described["boundary"] = [
            [number(point[0]), number(point[1])] for point in region.boundary()
        ]
        payload["region"] = described
    payload["statistics"] = toolpath.statistics()
    payload["moves"] = [
        {
            "index": index,
            "kind": move.kind.value,
            "pass_index": move.pass_index,
            "label": move.label,
            "feed_mm_per_min": number(move.feed_mm_per_min),
            "length_mm": number(move.length_mm),
            "duration_s": round(move.duration_s, 6),
            "points": [[number(value) for value in row] for row in move.points],
        }
        for index, move in enumerate(toolpath.moves)
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"