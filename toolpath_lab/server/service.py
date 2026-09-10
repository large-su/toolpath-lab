"""组装一次规划的结果。

界面需要的全部内容都在这里汇合：参数回显、刀具摘要、区域轮廓、刀路与统计、播放时间轴。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from toolpath_lab import __version__
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning import run_plan
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.simulation import Timeline, build_timeline

#: 播放采样的上限，决定响应的体积。
DEFAULT_MAX_SAMPLES = 4000


@dataclass(frozen=True, slots=True)
class PlanResult:
    """一次完成的规划。"""

    request: PlanRequest
    toolpath: Toolpath
    timeline: Timeline | None
    warnings: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        request = self.request
        boundary = request.region.boundary()
        return {
            "ok": True,
            "version": __version__,
            "request": request.to_payload(),
            "tool": request.tool.describe(),
            "region": {
                **request.region.describe(),
                "boundary": [
                    [round(float(point[0]), 4), round(float(point[1]), 4), 0.0]
                    for point in boundary
                ],
            },
            "toolpath": self.toolpath.to_payload(),
            "timeline": None if self.timeline is None else self.timeline.to_payload(),
            "warnings": list(self.warnings),
        }


def execute_plan(
    request: PlanRequest,
    *,
    with_timeline: bool = True,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> PlanResult:
    """执行一次规划。"""

    outcome = run_plan(
        planner_id=request.planner_id,
        tool=request.tool,
        region=request.region,
        parameters=request.planner_parameters,
    )
    timeline = (
        build_timeline(outcome.toolpath, max_samples=max_samples) if with_timeline else None
    )
    return PlanResult(
        request=request,
        toolpath=outcome.toolpath,
        timeline=timeline,
        warnings=tuple(request.warnings) + tuple(outcome.warnings),
    )
