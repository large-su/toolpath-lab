"""Importers for input formats -- the mirror of `export/`: read a drawing, hand back outlines.

Like `export/`, this layer depends on `core` only, and everything in it is a pure function over text
so it can be tested without files or HTTP.
"""

from __future__ import annotations

from toolpath_lab.importers.dxf import (
    IMPORT_PARAMETERS,
    ImportResult,
    Outline,
    parse_dxf,
)

__all__ = ["IMPORT_PARAMETERS", "ImportResult", "Outline", "parse_dxf"]
