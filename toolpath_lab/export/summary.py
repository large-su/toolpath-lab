"""Self-describing summary lines shared by the export formats.

A file that leaves this program should be able to explain itself: which strategy produced it, how
much it cuts, how long it takes. `toolpath_summary_lines` reads all of that off the toolpath, so
every caller gets it without passing anything; `provenance_lines` normalises the extra facts a caller
knows (coverage, warnings, the echoed request) into the same spirit.

The two export formats print these lines differently on purpose: an NC program's header is comments
anyway, so it always carries the summary; a CSV is a data table first, so it only gets a comment
block when the caller asks for one.
"""

from __future__ import annotations

from collections.abc import Iterable

from toolpath_lab.core.path import Toolpath


def toolpath_summary_lines(toolpath: Toolpath) -> list[str]:
    """Facts read off the toolpath alone: strategy, counts, lengths and estimated time."""

    statistics = toolpath.statistics()
    label = f" ({toolpath.planner_label})" if toolpath.planner_label else ""
    return [
        f"strategy: {toolpath.planner}{label}",
        f"passes {statistics['pass_count']}, moves {statistics['move_count']}, "
        f"points {statistics['point_count']}",
        f"cut {statistics['cut_length_mm']:.2f} mm, rapid {statistics['rapid_length_mm']:.2f} mm",
        f"estimated time {statistics['estimated_time_s']:.1f} s "
        f"(cutting {statistics['cutting_time_s']:.1f} s)",
    ]


def provenance_lines(extra: Iterable[str]) -> list[str]:
    """Normalise caller supplied lines: drop blanks, collapse whitespace.

    A note or warning containing a newline would otherwise break a single line comment, which is why
    everything goes through here before it is written into a file header.
    """

    return [" ".join(str(line).split()) for line in extra if str(line).strip()]
