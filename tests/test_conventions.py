"""Convention checks taken from CONTRIBUTING.md and docs/extending.md.

Two conventions about language are written down in both documents:

- code comments and docstrings are written in English;
- everything the user sees (labels, help texts, notes, warnings) is written in Chinese.

Both are machine checkable, so they are checked here instead of being left to review. The first one
used to carry a list of files whose docstrings predated the convention; that sweep is finished, so
the rule now holds for every Python file in the repository without exception.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from toolpath_lab.planning import planner_catalog, run_plan
from toolpath_lab.core.region import build_region, region_catalog
from toolpath_lab.core.tool import Tool, ToolKind

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOTS = ("toolpath_lab", "tests", "examples")


def _chinese_characters(text: str) -> int:
    return sum(1 for character in text if "\u4e00" <= character <= "\u9fff")


def _english_offences(path: Path) -> int:
    """Chinese characters in the docstrings and comments of one file."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            total += _chinese_characters(ast.get_docstring(node) or "")
    for line in source.splitlines():
        if line.strip().startswith("#"):
            total += _chinese_characters(line)
    return total


def _source_files():
    for root in SOURCE_ROOTS:
        for path in sorted((REPOSITORY_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(REPOSITORY_ROOT).as_posix(), path


class DocstringLanguageTests(unittest.TestCase):
    def test_every_python_file_documents_in_english(self) -> None:
        offences = {
            name: _english_offences(path)
            for name, path in _source_files()
            if _english_offences(path)
        }
        self.assertEqual(
            offences,
            {},
            "文档字符串与注释应该是英文（CONTRIBUTING / docs/extending.md §5）；"
            f"这些文件里还有中文 {offences}",
        )


class UserFacingTextTests(unittest.TestCase):
    """The other half of the rule: everything the user sees is written in Chinese."""

    def test_catalog_labels_and_helps_are_chinese(self) -> None:
        entries = planner_catalog() + region_catalog()
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(item=entry["id"]):
                self.assertGreater(_chinese_characters(entry["label"]), 0)
                self.assertGreater(_chinese_characters(entry["description"]), 0)
                for parameter in entry["parameters"]:
                    self.assertGreater(
                        _chinese_characters(parameter["label"]), 0,
                        f"{entry['id']}.{parameter['key']} 的 label 应该是中文",
                    )
                    if parameter.get("choices"):
                        for choice in parameter["choices"]:
                            self.assertGreater(_chinese_characters(choice["label"]), 0)
                    if parameter.get("help"):
                        self.assertGreater(_chinese_characters(parameter["help"]), 0)

    def test_notes_and_warnings_are_chinese(self) -> None:
        tool = Tool(ToolKind.FLAT, diameter_mm=6.0, length_mm=30.0)
        cases = (
            # A stepover larger than the tool diameter makes the raster planner warn.
            ("raster", {"stepover_mm": 12.0}),
            # `contour` never warns: impossible geometry raises PlanningError instead.
            ("contour", {"stepover_mm": 6.0}),
            # An unreachable coverage target makes the adaptive planner warn.
            ("adaptive_contour", {"stepover_mm": 6.0, "coverage_target": 100.0}),
        )
        for planner_id, parameters in cases:
            with self.subTest(planner=planner_id):
                outcome = run_plan(
                    planner_id=planner_id,
                    tool=tool,
                    region=build_region("square", {"side_mm": 80.0}),
                    parameters=parameters,
                )
                self.assertTrue(outcome.toolpath.notes)
                for note in outcome.toolpath.notes:
                    self.assertGreater(_chinese_characters(note), 0, note)
                if planner_id != "contour":
                    self.assertTrue(outcome.warnings, "这两种情形都该给出中文提醒")
                for warning in outcome.warnings:
                    self.assertGreater(_chinese_characters(warning), 0, warning)


if __name__ == "__main__":
    unittest.main()
