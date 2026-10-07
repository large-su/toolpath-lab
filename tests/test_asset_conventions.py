"""Convention checks for the non-Python sources: the web assets and the Electron shell.

The rule is the one from CONTRIBUTING.md and docs/extending.md section 5: code comments and docstrings
are written in English, while everything the user sees stays Chinese. Python files are checked through
their AST in tests/test_conventions.py; here a small scanner picks the comments out of .js / .mjs /
.css / .html files, because those have no AST to walk. Vendored third party code (web/vendor) is
excluded: it is not ours to translate.
"""

from __future__ import annotations

import pathlib
import unittest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSET_SUFFIXES = {".js": "js", ".mjs": "js", ".css": "css", ".html": "html"}

#: Files whose comments are still Chinese. Remove an entry as soon as its file is translated; never
#: add one. Empty since the sweep finished: every asset comment is English now.
_PENDING_ENGLISH: tuple[str, ...] = ()


def _scan(source: str, kind: str) -> list[str]:
    """Comment texts of one asset. Strings are skipped so a "//" inside a URL is not a comment."""

    found: list[str] = []
    i = 0
    size = len(source)
    while i < size:
        char = source[i]
        following = source[i + 1] if i + 1 < size else ""
        if kind == "html" and source.startswith("<!--", i):
            end = source.find("-->", i + 4)
            end = size if end < 0 else end
            found.append(source[i + 4 : end])
            i = end + 3
            continue
        if kind == "html" and source.startswith(("<script", "<style"), i):
            tag = "script" if source.startswith("<script", i) else "style"
            open_end = source.find(">", i)
            close = source.find(f"</{tag}", open_end)
            close = size if close < 0 else close
            inner = kind if open_end < 0 else None
            found.extend(_scan(source[open_end + 1 : close], "js" if tag == "script" else "css"))
            i = close
            continue
        if kind in ("js", "css") and char == "/" and following == "*":
            end = source.find("*/", i + 2)
            end = size if end < 0 else end
            found.append(source[i + 2 : end])
            i = end + 2
            continue
        if kind == "js" and char == "/" and following == "/":
            end = source.find("\n", i)
            end = size if end < 0 else end
            found.append(source[i + 2 : end])
            i = end
            continue
        if char in "\"'":
            quote = char
            i += 1
            while i < size:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return found


def _chinese_characters(text: str) -> int:
    return sum(1 for character in text if "\u4e00" <= character <= "\u9fff")


def _asset_offences(path: pathlib.Path) -> int:
    kind = ASSET_SUFFIXES[path.suffix]
    source = path.open("r", encoding="utf-8", newline="").read()
    return sum(_chinese_characters(chunk) for chunk in _scan(source, kind))


def _asset_files():
    for path in sorted((REPOSITORY_ROOT / "toolpath_lab" / "web").rglob("*")):
        if path.is_file() and path.suffix in ASSET_SUFFIXES and "vendor" not in path.parts:
            yield path.relative_to(REPOSITORY_ROOT).as_posix(), path
    shell = REPOSITORY_ROOT / "electron" / "main.mjs"
    yield shell.relative_to(REPOSITORY_ROOT).as_posix(), shell


class AssetCommentLanguageTests(unittest.TestCase):
    def test_no_asset_breaks_the_english_rule(self) -> None:
        offences = {
            name: _asset_offences(path)
            for name, path in _asset_files()
            if _asset_offences(path) and name not in _PENDING_ENGLISH
        }
        self.assertEqual(
            offences,
            {},
            "web 资源与 Electron 壳的注释也应该用英文（CONTRIBUTING / docs/extending.md §5）；"
            f"这些文件里还有中文 {offences}",
        )

    def test_the_pending_list_only_shrinks(self) -> None:
        """A file that has been translated must be removed from the list, or this fails."""

        stale = [name for name in _PENDING_ENGLISH if _asset_offences(REPOSITORY_ROOT / name) == 0]
        self.assertEqual(stale, [], f"这些文件已无中文注释，请从清单里划掉：{stale}")
        self.assertEqual(len(_PENDING_ENGLISH), len(set(_PENDING_ENGLISH)))

    def test_the_interface_text_is_still_chinese(self) -> None:
        """The other half of the rule: what the user reads is Chinese, comments are not."""

        page = (REPOSITORY_ROOT / "toolpath_lab" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertGreater(_chinese_characters(page), 20)


if __name__ == "__main__":
    unittest.main()
