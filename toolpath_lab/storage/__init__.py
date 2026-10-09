"""工程与刀具库的持久化。

一个工程 = 模型 + 毛坯 + 参数 + 工序树，存在用户数据目录下的 JSON 文件里
（``.npz`` 单独放网格）。刀具库是**全局**的一份 ``tools.json``，与工程并列。
没有数据库依赖，这两类文件都可以直接备份与查看。
"""

from __future__ import annotations

from toolpath_lab.storage.repository import FORMAT_VERSION, Project, ProjectRepository
from toolpath_lab.storage.tool_library import ToolRepository, default_library_dir

__all__ = [
    "FORMAT_VERSION",
    "Project",
    "ProjectRepository",
    "ToolRepository",
    "default_library_dir",
]
