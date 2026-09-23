"""Entry point for ``python -m toolpath_lab``."""

from __future__ import annotations

import sys

from toolpath_lab.cli import main

if __name__ == "__main__":
    sys.exit(main())
