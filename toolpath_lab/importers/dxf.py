"""Minimal DXF outline import: LWPOLYLINE, POLYLINE and LINE from a 2D drawing.

Only the entities a 2.5D outline needs are read, and everything else is reported instead of guessed:
a drawing that also contains arcs, circles, splines or 3D data imports its polylines and says what it
ignored, so a wrong outline is never quietly machined. Circles and arcs are deliberately *not*
approximated here -- that is a decision for the caller, and silently turning an arc into a chord would
be exactly the kind of guess this project avoids.

Coordinates are taken as millimetres (the $INSUNITS header is not interpreted) and are normalised
later by the region layer (counter-clockwise, no repeated first point). The parser takes text rather
than a path: reading files belongs to the caller (the HTTP layer), which keeps this module pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

#: Endpoints closer than this are treated as the same point (drawings round coordinates).
_TOLERANCE_MM = 1e-6
#: A polyline whose ends are this close counts as closed.
_CLOSE_TOLERANCE_MM = 1e-3


@dataclass(frozen=True, slots=True)
class Outline:
    """One outline from a drawing."""

    points: tuple[tuple[float, float], ...]
    closed: bool
    layer: str = ""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What the drawing contained, plus what the parser had to skip (warnings are user visible)."""

    outlines: tuple[Outline, ...]
    skipped: tuple[str, ...]
    warnings: tuple[str, ...]


def _pairs(text: str) -> list[tuple[int, str]]:
    """DXF tag/value pairs: every odd line is a group code, the next line is its value."""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0].lstrip("\ufeff")
    encoded: list[tuple[int, str]] = []
    for index in range(0, len(lines) - 1, 2):
        try:
            encoded.append((int(lines[index].strip()), lines[index + 1].strip()))
        except ValueError:
            continue  # not a tag: a stray blank line or a comment, ignore the pair
    return encoded


def _entities(pairs: list[tuple[int, str]]) -> list[tuple[str, list[tuple[int, str]]]]:
    """Group the tag stream into (entity type, tags) blocks."""

    blocks: list[tuple[str, list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] | None = None
    name = ""
    for code, value in pairs:
        if code == 0:
            if current is not None:
                blocks.append((name, current))
            name, current = value.upper(), []
        elif current is not None:
            current.append((code, value))
    if current is not None:
        blocks.append((name, current))
    return blocks


def _first(tags: list[tuple[int, str]], code: int) -> str:
    for tag_code, value in tags:
        if tag_code == code:
            return value
    return ""


def _vertices(tags: list[tuple[int, str]]) -> list[tuple[float, float]]:
    """10/20 pairs of one entity, in file order (LWPOLYLINE repeats them, VERTEX holds one each)."""

    points: list[tuple[float, float]] = []
    x: float | None = None
    for code, value in tags:
        try:
            number = float(value)
        except ValueError:
            continue
        if code == 10:
            x = number
        elif code == 20 and x is not None:
            points.append((x, number))
            x = None
    return points


def _chain(segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join LINE segments end to end into runs, so a square drawn as four lines reads as one loop."""

    remaining = list(segments)
    runs: list[list[tuple[float, float]]] = []
    while remaining:
        start, end = remaining.pop(0)
        run = [start, end]
        grew = True
        while grew:
            grew = False
            for index, (head, tail) in enumerate(remaining):
                if hypot(head[0] - run[-1][0], head[1] - run[-1][1]) <= _CLOSE_TOLERANCE_MM:
                    run.append(tail)
                elif hypot(tail[0] - run[-1][0], tail[1] - run[-1][1]) <= _CLOSE_TOLERANCE_MM:
                    run.append(head)
                elif hypot(head[0] - run[0][0], head[1] - run[0][1]) <= _CLOSE_TOLERANCE_MM:
                    run.insert(0, tail)
                elif hypot(tail[0] - run[0][0], tail[1] - run[0][1]) <= _CLOSE_TOLERANCE_MM:
                    run.insert(0, head)
                else:
                    continue
                remaining.pop(index)
                grew = True
                break
        if len(run) >= 3:
            runs.append(run)
    return runs


def parse_dxf(text: str) -> ImportResult:
    """Read the 2D outlines of a DXF drawing, reporting what was skipped."""

    if not text.strip():
        return ImportResult((), (), ("文件是空的，没有读到任何图形",))

    blocks = _entities(_pairs(text))
    outlines: list[Outline] = []
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    skipped: list[str] = []
    open_outlines = 0

    index = 0
    while index < len(blocks):
        name, tags = blocks[index]
        if name == "LWPOLYLINE":
            points = _vertices(tags)
            closed = bool(int(_first(tags, 70) or "0") & 1)
            outlines.extend(_outline(points, closed, _first(tags, 8)))
        elif name == "POLYLINE":
            points: list[tuple[float, float]] = []
            cursor = index + 1
            while cursor < len(blocks) and blocks[cursor][0] == "VERTEX":
                points.extend(_vertices(blocks[cursor][1]))
                cursor += 1
            closed = bool(int(_first(tags, 70) or "0") & 1)
            outlines.extend(_outline(points, closed, _first(tags, 8)))
            index = cursor - 1
        elif name == "LINE":
            start = _vertices(tags)
            end_x, end_y = _first(tags, 11), _first(tags, 21)
            if len(start) == 1 and end_x and end_y:
                segments.append((start[0], (float(end_x), float(end_y))))
        elif name not in ("SEQEND", "ENDSEC", "SECTION", "EOF", "TABLE", "BLOCK", "ENDBLK", "VERTEX"):
            if name and name not in skipped:
                skipped.append(name)
        index += 1

    for run in _chain(segments):
        outlines.extend(_outline(run, _endpoints_match(run), ""))

    for outline in outlines:
        if not outline.closed:
            open_outlines += 1
    warnings: list[str] = []
    if skipped:
        warnings.append(
            "以下图元暂不支持，已忽略：" + "、".join(sorted(skipped)) + "（圆弧/圆请先炸开成折线）"
        )
    if open_outlines:
        warnings.append(f"有 {open_outlines} 条轮廓没有闭合，请检查图纸或在 CAD 里闭合后再导入")
    if not outlines:
        warnings.append("没有读到可用的闭合轮廓（支持 LWPOLYLINE、POLYLINE 与首尾相连的 LINE）")
    return ImportResult(tuple(outlines), tuple(skipped), tuple(warnings))


def _outline(
    points: list[tuple[float, float]], closed: bool, layer: str
) -> list[Outline]:
    """One outline from raw points, dropping a repeated closing point."""

    cleaned = [points[0]]
    for point in points[1:]:
        if hypot(point[0] - cleaned[-1][0], point[1] - cleaned[-1][1]) > _TOLERANCE_MM:
            cleaned.append(point)
    if len(cleaned) >= 3 and hypot(
        cleaned[-1][0] - cleaned[0][0], cleaned[-1][1] - cleaned[0][1]
    ) <= _CLOSE_TOLERANCE_MM:
        cleaned.pop()
        closed = True
    if len(cleaned) < 3:
        return []
    return [Outline(tuple(cleaned), closed, layer)]


def _endpoints_match(points: list[tuple[float, float]]) -> bool:
    return (
        len(points) >= 3
        and hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1])
        <= _CLOSE_TOLERANCE_MM
    )
