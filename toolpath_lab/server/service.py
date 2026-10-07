"""Assemble the result of one plan.

Everything the UI needs comes together here: the echoed parameters, the tool summary, the region
outline, the toolpath with its statistics, the playback timeline, and the coverage analysis (whether
this toolpath actually machines the region out).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from toolpath_lab import __version__
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning import Coverage, coverage_warnings, measure_coverage, run_plan
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.simulation import Timeline, build_timeline

#: Upper bound on playback samples, which sets the size of the response.
DEFAULT_MAX_SAMPLES = 4000


@dataclass(frozen=True, slots=True)
class PlanResult:
    """One completed plan."""

    request: PlanRequest
    toolpath: Toolpath
    timeline: Timeline | None
    coverage: Coverage | None
    warnings: tuple[str, ...]

    def result_lines(self) -> list[str]:
        """Facts only the service knows, for the header of an exported file.

        Coverage is computed for exports too (the timeline is not: it is expensive and irrelevant to
        a file), so a downloaded program can state how much material it leaves behind.
        """

        lines: list[str] = []
        if self.coverage is not None:
            lines.append(
                f"coverage {self.coverage.ratio * 100:.2f} % "
                f"(uncut {self.coverage.uncut_area_mm2:.1f} mm2 in {self.coverage.patch_count} patches)"
            )
        lines.extend(self.warnings)
        return lines

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
            "coverage": None if self.coverage is None else self.coverage.describe(),
            "warnings": list(self.warnings),
        }


def execute_plan(
    request: PlanRequest,
    *,
    with_timeline: bool = True,
    with_coverage: bool = True,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> PlanResult:
    """Run one plan (the export endpoints turn the timeline and coverage off and take the path only)."""

    outcome = run_plan(
        planner_id=request.planner_id,
        tool=request.tool,
        region=request.region,
        parameters=request.planner_parameters,
    )
    timeline = (
        build_timeline(outcome.toolpath, max_samples=max_samples) if with_timeline else None
    )
    coverage = (
        measure_coverage(outcome.toolpath, request.region, request.tool)
        if with_coverage
        else None
    )
    warnings = tuple(request.warnings) + tuple(outcome.warnings)
    if coverage is not None:
        warnings += tuple(coverage_warnings(coverage))
    return PlanResult(
        request=request,
        toolpath=outcome.toolpath,
        timeline=timeline,
        coverage=coverage,
        warnings=warnings,
    )
