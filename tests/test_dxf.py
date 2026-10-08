"""DXF outline import: what is read, what is skipped, and what is reported."""

from __future__ import annotations

import unittest

from toolpath_lab.importers import parse_dxf


def _dxf(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _lwpolyline(points: list[tuple[float, float]], closed: bool = True) -> str:
    body = ["0", "SECTION", "2", "ENTITIES", "0", "LWPOLYLINE", "8", "outline",
            "90", str(len(points)), "70", "1" if closed else "0"]
    for x, y in points:
        body += ["10", str(x), "20", str(y)]
    return _dxf(*body, "0", "ENDSEC", "0", "EOF")


SQUARE = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]


class LwPolylineTests(unittest.TestCase):
    def test_a_closed_square_is_read(self) -> None:
        result = parse_dxf(_lwpolyline(SQUARE))
        self.assertEqual(len(result.outlines), 1)
        outline = result.outlines[0]
        self.assertEqual(len(outline.points), 4)
        self.assertTrue(outline.closed)
        self.assertEqual(outline.layer, "outline")
        self.assertEqual(result.warnings, ())

    def test_a_repeated_closing_point_is_dropped(self) -> None:
        result = parse_dxf(_lwpolyline([*SQUARE, SQUARE[0]]))
        self.assertEqual(len(result.outlines[0].points), 4)
        self.assertTrue(result.outlines[0].closed)

    def test_an_open_polyline_is_reported_as_open(self) -> None:
        result = parse_dxf(_lwpolyline(SQUARE[:3], closed=False))
        self.assertEqual(len(result.outlines), 1)
        self.assertFalse(result.outlines[0].closed)
        self.assertTrue(any("没有闭合" in warning for warning in result.warnings))


class PolylineTests(unittest.TestCase):
    def test_an_old_style_polyline_with_vertices_is_read(self) -> None:
        body = ["0", "SECTION", "2", "ENTITIES", "0", "POLYLINE", "8", "outline", "70", "1"]
        for x, y in SQUARE[:3]:
            body += ["0", "VERTEX", "10", str(x), "20", str(y)]
        result = parse_dxf(_dxf(*body, "0", "SEQEND", "0", "ENDSEC", "0", "EOF"))
        self.assertEqual(len(result.outlines), 1)
        self.assertEqual(len(result.outlines[0].points), 3)
        self.assertTrue(result.outlines[0].closed)


class LineTests(unittest.TestCase):
    def test_four_lines_are_chained_into_one_loop(self) -> None:
        body = ["0", "SECTION", "2", "ENTITIES"]
        for start, end in ((SQUARE[0], SQUARE[1]), (SQUARE[2], SQUARE[3]),
                           (SQUARE[1], SQUARE[2]), (SQUARE[3], SQUARE[0])):
            body += ["0", "LINE", "8", "outline",
                     "10", str(start[0]), "20", str(start[1]),
                     "11", str(end[0]), "21", str(end[1])]
        result = parse_dxf(_dxf(*body, "0", "ENDSEC", "0", "EOF"))
        self.assertEqual(len(result.outlines), 1)
        self.assertEqual(len(result.outlines[0].points), 4)
        self.assertTrue(result.outlines[0].closed)


class SkippedTests(unittest.TestCase):
    def test_arcs_and_circles_are_reported_not_guessed(self) -> None:
        body = ["0", "SECTION", "2", "ENTITIES", "0", "CIRCLE", "10", "0", "20", "0", "40", "10",
                "0", "ARC", "10", "0", "20", "0", "40", "5", "50", "0", "51", "90"]
        result = parse_dxf(_dxf(*body, "0", "ENDSEC", "0", "EOF"))
        self.assertEqual(result.outlines, ())
        self.assertIn("CIRCLE", result.skipped)
        self.assertIn("ARC", result.skipped)
        self.assertTrue(any("暂不支持" in warning for warning in result.warnings))
        self.assertTrue(any("没有读到可用的闭合轮廓" in warning for warning in result.warnings))

    def test_an_empty_file_is_reported(self) -> None:
        result = parse_dxf("   \n")
        self.assertEqual(result.outlines, ())
        self.assertTrue(any("空的" in warning for warning in result.warnings))

    def test_junk_does_not_crash(self) -> None:
        result = parse_dxf("hello\nworld\nthis is not a dxf\n")
        self.assertEqual(result.outlines, ())
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
