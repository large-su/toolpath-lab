"""Assemble the result of one plan.

Everything the UI needs comes together here: the echoed parameters, the tool summary, the region
outline, the toolpath with its statistics, the playback timeline, the coverage analysis (did the tool
pass over the whole region) and the material removal map (how deep did it get).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from toolpath_lab import __version__
from toolpath_lab.core.path import Toolpath
from toolpath_lab.planning import Coverage, coverage_warnings, measure_coverage, run_plan
from toolpath_lab.planning.collision import HolderCheck
from toolpath_lab.planning.removal import Removal, measure_removal, removal_warnings
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.simulation import Timeline, build_timeline

#: Upper bound on playback samples, which sets the size of the response.
DEFAULT_MAX_SAMPLES = 4000
#: Progressive floor snapshots the plan response carries for the playback (see `measure_removal`).
DEFAULT_CHECKPOINTS = 8


@dataclass(frozen=True, slots=True)
class PlanResult:
    """One completed plan."""

    request: PlanRequest
    toolpath: Toolpath
    timeline: Timeline | None
    coverage: Coverage | None
    removal: Removal | None
    holder: HolderCheck | None
    warnings: tuple[str, ...]

    def result_lines(self) -> list[str]:
        """Facts only the service knows, for the header of an exported file.

        Coverage and the removal map are computed for exports too (the timeline is not: it is
        expensive and irrelevant to a file), so a downloaded program can state how much material it
        leaves behind and how much of the floor it actually reached.
        """

        lines: list[str] = []
        if self.coverage is not None:
            lines.append(
                f"coverage {self.coverage.ratio * 100:.2f} % "
                f"(uncut {self.coverage.uncut_area_mm2:.1f} mm2 in {self.coverage.patch_count} patches)"
            )
        if self.removal is not None and self.removal.removed_volume_mm3 > 0.0:
            lines.append(
                f"floor {self.removal.floor_mm:.2f} mm reached on "
                f"{self.removal.floor_ratio * 100:.1f} % of the region; removed "
                f"{self.removal.removed_volume_mm3:.0f} mm3, remaining "
                f"{self.removal.remaining_volume_mm3:.0f} mm3, never touched "
                f"{self.removal.uncut_area_mm2:.0f} mm2"
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
            "removal": None if self.removal is None else self.removal.describe(),
            "holder": None if self.holder is None else self.holder.describe(),
            "warnings": list(self.warnings),
        }


def execute_plan(
    request: PlanRequest,
    *,
    with_timeline: bool = True,
    with_coverage: bool = True,
    with_removal: bool = True,
    with_checkpoints: bool = True,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> PlanResult:
    """Run one plan (the export endpoints turn the timeline off and take the path plus its facts).

    `with_checkpoints` adds the progressive floor snapshots the playback scrubs through. They only make
    sense next to a timeline, so the export endpoints (which carry the path and its facts, not a
    playback) ask for the plain map.
    """

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
    stepover = request.planner_parameters.get("stepover_mm")
    removal = (
        measure_removal(
            outcome.toolpath,
            request.region,
            request.tool,
            stepover_mm=float(stepover) if isinstance(stepover, (int, float)) else None,
            checkpoints=DEFAULT_CHECKPOINTS if with_checkpoints else 0,
        )
        if with_removal
        else None
    )
    warnings = tuple(request.warnings) + tuple(outcome.warnings)
    if coverage is not None:
        warnings += tuple(coverage_warnings(coverage))
    if removal is not None:
        warnings += tuple(removal_warnings(removal))
    return PlanResult(
        request=request,
        toolpath=outcome.toolpath,
        timeline=timeline,
        coverage=coverage,
        removal=removal,
        holder=outcome.holder,
        warnings=warnings,
    )
