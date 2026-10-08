"""DXF outline import: what is read, what is skipped, and what is reported."""

from __future__ import annotations

import unittest
from math import sqrt

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.importers import IMPORT_PARAMETERS, parse_dxf
from toolpath_lab.importers.dxf import MAX_CURVE_SEGMENTS


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


class CurveApproximationTests(unittest.TestCase):
    """Curves are reported by default and tessellated only when a chord tolerance is given."""

    @staticmethod
    def _circle(radius: float = 40.0, center: tuple[float, float] = (0.0, 0.0)) -> str:
        return _dxf("0", "SECTION", "2", "ENTITIES", "0", "CIRCLE", "8", "cut",
                    "10", str(center[0]), "20", str(center[1]), "40", str(radius),
                    "0", "ENDSEC", "0", "EOF")

    @staticmethod
    def _arc(radius: float = 5.0, start: float = 0.0, end: float = 90.0) -> str:
        return _dxf("0", "SECTION", "2", "ENTITIES", "0", "ARC", "8", "cut",
                    "10", "0", "20", "0", "40", str(radius),
                    "50", str(start), "51", str(end), "0", "ENDSEC", "0", "EOF")

    @staticmethod
    def _ellipse(semi_major: float = 30.0, ratio: float = 0.5) -> str:
        return _dxf("0", "SECTION", "2", "ENTITIES", "0", "ELLIPSE", "8", "cut",
                    "10", "0", "20", "0", "11", str(semi_major), "21", "0", "40", str(ratio),
                    "0", "ENDSEC", "0", "EOF")

    @staticmethod
    def _bulge(points: list[tuple[float, float]], radius: float, closed: bool = True) -> float:
        """Largest distance between the middle of a chord and the circle of that radius."""

        last = len(points) if closed else len(points) - 1
        return max(
            radius
            - sqrt(
                ((points[i][0] + points[(i + 1) % len(points)][0]) / 2.0) ** 2
                + ((points[i][1] + points[(i + 1) % len(points)][1]) / 2.0) ** 2
            )
            for i in range(last)
        )

    def test_without_a_tolerance_curves_are_still_reported(self) -> None:
        result = parse_dxf(self._circle())
        self.assertEqual(result.outlines, ())
        self.assertEqual(result.skipped, ("CIRCLE",))
        self.assertEqual(result.approximated, ())
        # The hint names the option instead of just saying "not supported".
        self.assertTrue(any("圆弧弦高容差" in warning for warning in result.warnings))

    def test_a_circle_is_tessellated_to_the_analytic_segment_count(self) -> None:
        result = parse_dxf(self._circle(radius=40.0), arc_tolerance_mm=0.05)
        self.assertEqual(result.skipped, ())
        self.assertEqual(result.approximated, ("CIRCLE",))
        outline = result.outlines[0]
        # 2*pi / (2*acos(1 - 0.05/40)) = 62.85 -> 63 points, every one of them on the circle.
        self.assertEqual(len(outline.points), 63)
        self.assertTrue(outline.closed)
        for x, y in outline.points:
            self.assertAlmostEqual(sqrt(x * x + y * y), 40.0, places=9)
        # The tolerance is respected, and not over-satisfied by an order of magnitude.
        bulge = self._bulge(outline.points, 40.0)
        self.assertLessEqual(bulge, 0.05)
        self.assertGreater(bulge, 0.025)

    def test_a_coarser_tolerance_gives_fewer_points(self) -> None:
        coarse = parse_dxf(self._circle(radius=40.0), arc_tolerance_mm=0.5).outlines[0]
        self.assertEqual(len(coarse.points), 20)  # 2*pi / (2*acos(1 - 0.5/40)) = 19.87 -> 20
        fine = parse_dxf(self._circle(radius=40.0), arc_tolerance_mm=0.05).outlines[0]
        self.assertGreater(len(fine.points), len(coarse.points))

    def test_an_arc_keeps_its_endpoints_and_stays_open(self) -> None:
        result = parse_dxf(self._arc(radius=5.0, start=0.0, end=90.0), arc_tolerance_mm=0.05)
        outline = result.outlines[0]
        # 90 degrees at r5 with 0.05 mm: 6 steps, so seven points including both ends.
        self.assertEqual(len(outline.points), 7)
        self.assertFalse(outline.closed)
        self.assertAlmostEqual(outline.points[0][0], 5.0, places=9)
        self.assertAlmostEqual(outline.points[0][1], 0.0, places=9)
        self.assertAlmostEqual(outline.points[-1][0], 0.0, places=9)
        self.assertAlmostEqual(outline.points[-1][1], 5.0, places=9)
        self.assertLessEqual(self._bulge(outline.points, 5.0, closed=False), 0.05)
        self.assertTrue(any("没有闭合" in warning for warning in result.warnings))

    def test_an_ellipse_uses_its_semi_axes(self) -> None:
        result = parse_dxf(self._ellipse(semi_major=30.0, ratio=0.5), arc_tolerance_mm=0.05)
        outline = result.outlines[0]
        self.assertTrue(outline.closed)
        # Sized on the major axis: 2*pi / (2*acos(1 - 0.05/30)) = 54.4 -> 55 points.
        self.assertEqual(len(outline.points), 55)
        xs = [point[0] for point in outline.points]
        ys = [point[1] for point in outline.points]
        # Every point is *on* the ellipse ((x/a)^2 + (y/b)^2 = 1), and the samples reach the axes to
        # within one step: the parameter grid does not have to land exactly on u = 0 or u = pi/2.
        for x, y in outline.points:
            self.assertAlmostEqual((x / 30.0) ** 2 + (y / 15.0) ** 2, 1.0, places=9)
        self.assertAlmostEqual(max(xs), 30.0, delta=0.05)
        self.assertAlmostEqual(min(xs), -30.0, delta=0.05)
        self.assertAlmostEqual(max(ys), 15.0, delta=0.05)
        self.assertAlmostEqual(min(ys), -15.0, delta=0.05)

    def test_the_answer_says_what_was_approximated(self) -> None:
        result = parse_dxf(self._circle(), arc_tolerance_mm=0.05)
        self.assertEqual(result.approximated, ("CIRCLE",))
        note = next(warning for warning in result.warnings if "弦高容差" in warning)
        self.assertIn("0.05 mm", note)
        self.assertIn("63 个点", note)

    def test_a_tessellated_curve_sits_next_to_the_polylines(self) -> None:
        body = ["0", "SECTION", "2", "ENTITIES", "0", "CIRCLE", "8", "cut",
                "10", "0", "20", "0", "40", "40",
                "0", "LWPOLYLINE", "8", "cut", "70", "1"]
        for x, y in SQUARE:
            body += ["10", str(x + 100), "20", str(y)]
        result = parse_dxf(_dxf(*body, "0", "ENDSEC", "0", "EOF"), arc_tolerance_mm=0.05)
        self.assertEqual(len(result.outlines), 2)
        self.assertEqual(sorted(len(outline.points) for outline in result.outlines), [4, 63])

    def test_the_segment_count_is_capped(self) -> None:
        result = parse_dxf(self._circle(radius=5000.0), arc_tolerance_mm=0.001)
        self.assertEqual(len(result.outlines[0].points), MAX_CURVE_SEGMENTS)
        self.assertTrue(any("封顶" in warning for warning in result.warnings))

    def test_a_malformed_curve_is_skipped_not_guessed(self) -> None:
        body = ["0", "SECTION", "2", "ENTITIES", "0", "ARC", "10", "0", "20", "0", "50", "0", "51", "90"]
        result = parse_dxf(_dxf(*body, "0", "ENDSEC", "0", "EOF"), arc_tolerance_mm=0.05)
        self.assertEqual(result.outlines, ())
        self.assertEqual(result.skipped, ("ARC",))

    def test_splines_stay_skipped_even_with_a_tolerance(self) -> None:
        body = ["0", "SECTION", "2", "ENTITIES", "0", "SPLINE", "10", "0", "20", "0"]
        result = parse_dxf(_dxf(*body, "0", "ENDSEC", "0", "EOF"), arc_tolerance_mm=0.05)
        self.assertEqual(result.outlines, ())
        self.assertEqual(result.skipped, ("SPLINE",))
        self.assertTrue(any("样条" in warning for warning in result.warnings))

    def test_the_tolerance_is_a_declared_parameter(self) -> None:
        declared = IMPORT_PARAMETERS.spec("arc_tolerance_mm")
        self.assertEqual(declared.default, 0.0)
        self.assertEqual((declared.minimum, declared.maximum), (0.0, 5.0))
        self.assertAlmostEqual(IMPORT_PARAMETERS.coerce({})["arc_tolerance_mm"], 0.0)
        self.assertAlmostEqual(
            IMPORT_PARAMETERS.coerce({"arc_tolerance_mm": 0.05})["arc_tolerance_mm"], 0.05
        )
        for bad in (-1.0, 6.0, "wide"):
            with self.subTest(tolerance=bad):
                with self.assertRaises(ParameterError):
                    IMPORT_PARAMETERS.coerce({"arc_tolerance_mm": bad})


if __name__ == "__main__":
    unittest.main()
