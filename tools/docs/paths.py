"""文档工具链共用的路径解析。

配图来源有三种写法（见 content.py）：
    generated/xxx.png → 由 figures.py 生成，位于 --figures 目录（默认 <out>/figures）
    images/xxx.png    → 仓库自带，docs/images/
    assets/xxx.png    → 文档工具链自带，tools/docs/assets/
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO / "docs"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def bootstrap() -> Path:
    """把仓库根目录放进 sys.path，脚本可以按 `tools.docs.*` 互相导入。"""

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return REPO


def figures_dir(out: str | Path = "build/docs") -> Path:
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = REPO / out_path
    return out_path / "figures"


def out_dir(out: str | Path = "build/docs") -> Path:
    out_path = Path(out)
    return out_path if out_path.is_absolute() else REPO / out_path


def resolve_figure(spec: str, figure_dir: str | Path) -> Path:
    """把 content.py 里的图路径写法解析成真实文件路径。"""

    path = Path(spec)
    if path.is_absolute():
        return path
    prefix, _, rest = spec.partition("/")
    if prefix == "generated":
        return Path(figure_dir) / rest
    if prefix == "images":
        return DOCS_DIR / "images" / rest
    if prefix == "assets":
        return ASSETS_DIR / rest
    return Path(figure_dir) / spec
