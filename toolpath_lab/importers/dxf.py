"""Minimal DXF outline import: LWPOLYLINE, POLYLINE, LINE, and curves when asked to approximate them.

Only the entities a 2.5D outline needs are read, and everything else is reported instead of guessed: a
drawing that also contains splines or 3D data imports its polylines and says what it ignored, so a wrong
outline is never quietly machined.

Curves (`ARC`, `CIRCLE`, `ELLIPSE`) sit exactly on that line. **By default they are still reported as
skipped**, because turning an arc into chords is a decision with a tolerance attached and the parser
refuses to make it behind the caller's back. Give `arc_tolerance_mm` (a chord/sagitta tolerance, the
option the panel offers and `/api/catalog` publishes) and the same entities are tessellated instead:
every step's bulge is at most that tolerance, the number of segments follows from the arc's own radius
(`segments = sweep / (2 * acos(1 - tolerance / radius))`, capped), and the answer says how many points
that produced. `SPLINE` stays skipped either way: evaluating NURBS weights and knots is a different order
of complexity, and the module's rule is to report rather than guess.

Coordinates are taken as millimetres (the $INSUNITS header is not interpreted) and are normalised
later by the region layer (counter-clockwise, no repeated first point). The parser takes text rather
than a path: reading files belongs to the caller (the HTTP layer), which keeps this module pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, ceil, cos, hypot, pi, radians, sin

from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    spec,
)

#: Endpoints closer than this are treated as the same point (drawings round coordinates).
_TOLERANCE_MM = 1e-6
#: A polyline whose ends are this close counts as closed.
_CLOSE_TOLERANCE_MM = 1e-3

#: Upper bound on the segments one curve is turned into: a 0.001 mm tolerance on a 500 mm radius would
#: otherwise ask for tens of thousands of points for a single arc.
MAX_CURVE_SEGMENTS = 2048

#: Import options, declared like every other user facing switch (CONTRIBUTING): the same declaration
#: validates the request, generates the control in the panel and is published by `/api/catalog`.
IMPORT_PARAMETERS: ParameterSet = ParameterSet(
    (
        spec("arc_tolerance_mm", "圆弧弦高容差", K.FLOAT, 0.0, minimum=0.0, maximum=5.0,
             step=0.01, unit="mm", group="导入",
             help="0 = 不近似：圆弧与圆列进「跳过项」，宁可少读也不猜。大于 0 时按这个弦高容差把 "
                  "ARC / CIRCLE / ELLIPSE 折线化（每段弓高不超过它），越小越贴近原弧、点越多，"
                  f"单条曲线最多 {MAX_CURVE_SEGMENTS} 段"),
    )
)


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
    #: Entity kinds tessellated because a chord tolerance was given; empty by default.
    approximated: tuple[str, ...] = ()


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


def parse_dxf(text: str, *, arc_tolerance_mm: float = 0.0) -> ImportResult:
    """Read the 2D outlines of a DXF drawing, reporting what was skipped.

    `arc_tolerance_mm` is the opt-in chord tolerance for curves: 0 (the default) keeps the parser's rule
    of reporting arcs and circles instead of guessing, anything larger tessellates them (see the module
    docstring). It is validated by `IMPORT_PARAMETERS`, which the HTTP layer applies before calling here.
    """

    if not text.strip():
        return ImportResult((), (), ("文件是空的，没有读到任何图形",))

    blocks = _entities(_pairs(text))
    outlines: list[Outline] = []
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    skipped: list[str] = []
    approximated: list[str] = []
    approximated_points = 0
    capped = False
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
        elif name in ("ARC", "CIRCLE", "ELLIPSE"):
            if arc_tolerance_mm <= 0.0:
                # The default rule: report the curve instead of guessing how finely to cut it up.
                if name not in skipped:
                    skipped.append(name)
            else:
                curves = _curve_outlines(name, tags, arc_tolerance_mm)
                if not curves:
                    if name not in skipped:
                        skipped.append(name)
                else:
                    if name not in approximated:
                        approximated.append(name)
                    for curve in curves:
                        approximated_points += len(curve.points)
                        capped = capped or len(curve.points) >= MAX_CURVE_SEGMENTS
                    outlines.extend(curves)
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
    if approximated:
        warnings.append(
            f"已按 {arc_tolerance_mm:g} mm 弦高容差把 "
            + "、".join(approximated)
            + f" 折线化成 {approximated_points} 个点（每段弓高不超过容差；给 0 就会像以前一样只报告不近似）"
        )
    if capped:
        warnings.append(
            f"有曲线的段数已封顶在 {MAX_CURVE_SEGMENTS} 段，容差再小也不会更细"
        )
    if skipped:
        warnings.append(
            "以下图元暂不支持，已忽略：" + "、".join(sorted(skipped))
            + "（圆弧/圆可以给「圆弧弦高容差」让它折线化，样条请先转成折线）"
        )
    if open_outlines:
        warnings.append(f"有 {open_outlines} 条轮廓没有闭合，请检查图纸或在 CAD 里闭合后再导入")
    if not outlines:
        warnings.append("没有读到可用的闭合轮廓（支持 LWPOLYLINE、POLYLINE 与首尾相连的 LINE）")
    return ImportResult(tuple(outlines), tuple(skipped), tuple(warnings), tuple(approximated))


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


# ----------------------------------------------------------------- curve tessellation
def _number(tags: list[tuple[int, str]], code: int) -> float | None:
    value = _first(tags, code)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _pair(tags: list[tuple[int, str]], first_code: int, second_code: int) -> tuple[float, float] | None:
    x, y = _number(tags, first_code), _number(tags, second_code)
    return None if x is None or y is None else (x, y)


def _segments_for(radius_mm: float, sweep_rad: float, tolerance_mm: float) -> int:
    """Segments a curve of this radius needs for a chord tolerance, capped.

    A chord spanning the angle `step` bulges by `r * (1 - cos(step / 2))` at its middle, so the step is
    `2 * acos(1 - tolerance / r)` and the segment count follows from the sweep. A tolerance larger than
    the radius means a single chord is already within tolerance.
    """

    if radius_mm <= _TOLERANCE_MM or sweep_rad <= _TOLERANCE_MM:
        return 1
    if tolerance_mm >= radius_mm:
        step = pi
    else:
        step = 2.0 * acos(max(-1.0, min(1.0, 1.0 - tolerance_mm / radius_mm)))
    return max(1, min(MAX_CURVE_SEGMENTS, int(ceil(sweep_rad / step))))


def _curve_outlines(name: str, tags: list[tuple[int, str]], tolerance_mm: float) -> list[Outline]:
    """One `ARC`, `CIRCLE` or `ELLIPSE` as a polyline whose every step is within the tolerance."""

    layer = _first(tags, 8)
    center = _pair(tags, 10, 20)
    if center is None:
        return []
    if name == "CIRCLE":
        radius = _number(tags, 40)
        if radius is None or radius <= _TOLERANCE_MM:
            return []
        count = _segments_for(radius, 2.0 * pi, tolerance_mm)
        points = [
            (center[0] + radius * cos(2.0 * pi * step / count),
             center[1] + radius * sin(2.0 * pi * step / count))
            for step in range(count)
        ]
        return [Outline(tuple(points), True, layer)]
    if name == "ARC":
        radius = _number(tags, 40)
        if radius is None or radius <= _TOLERANCE_MM:
            return []
        start = _number(tags, 50) or 0.0
        end = _number(tags, 51) or 0.0
        sweep = (end - start) % 360.0
        if sweep <= _TOLERANCE_MM:
            sweep = 360.0  # a zero sweep means a full turn in DXF practice
        count = _segments_for(radius, radians(sweep), tolerance_mm)
        begin = radians(start)
        points = [
            (center[0] + radius * cos(begin + radians(sweep) * step / count),
             center[1] + radius * sin(begin + radians(sweep) * step / count))
            for step in range(count + 1)
        ]
        # An arc on its own is not a closed outline; the caller is told so in the warnings.
        return [Outline(tuple(points), False, layer)]

    major = _pair(tags, 11, 21)
    ratio = _number(tags, 40)
    if name != "ELLIPSE" or major is None or ratio is None or ratio <= _TOLERANCE_MM:
        return []
    semi_major = hypot(major[0], major[1])
    if semi_major <= _TOLERANCE_MM:
        return []
    axis_x = (major[0] / semi_major, major[1] / semi_major)
    axis_y = (-axis_x[1], axis_x[0])
    semi_minor = semi_major * ratio
    start = _number(tags, 41)
    end = _number(tags, 42)
    begin = 0.0 if start is None else start
    finish = 2.0 * pi if end is None else end
    sweep = finish - begin
    while sweep <= _TOLERANCE_MM:
        sweep += 2.0 * pi
    closed = abs(sweep - 2.0 * pi) <= 1e-6
    # The major axis is the ellipse's largest radius, so sizing the steps on it is conservative.
    count = _segments_for(semi_major, sweep, tolerance_mm)
    steps = range(count) if closed else range(count + 1)
    points = [
        (center[0] + semi_major * cos(begin + sweep * step / count) * axis_x[0]
         + semi_minor * sin(begin + sweep * step / count) * axis_y[0],
         center[1] + semi_major * cos(begin + sweep * step / count) * axis_x[1]
         + semi_minor * sin(begin + sweep * step / count) * axis_y[1])
        for step in steps
    ]
    return [Outline(tuple(points), closed, layer)]
