"""策略注册表。

导入 toolpath_lab.planning 会导入策略模块，模块里的类装饰器把策略登记到 PLANNERS。
新策略因此只需要"放一个文件 + 在 __init__.py 里导入一行"，接口、界面都会自动包含它。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from toolpath_lab.core.registry import Registry

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from toolpath_lab.planning.base import Planner

PLANNERS: "Registry[type[Planner]]" = Registry("planner")


def planner_catalog() -> list[dict[str, Any]]:
    """所有已注册策略的 JSON 描述。"""

    return PLANNERS.catalog()
