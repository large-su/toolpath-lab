"""工程持久化。

一个工程 = 模型 + 毛坯 + 参数 + 工序树，存在用户数据目录下的 JSON 文件里
（``.npz`` 单独放网格）。没有数据库依赖，工程文件可以直接备份与查看。
"""

from __future__ import annotations

from toolpath_lab.storage.repository import FORMAT_VERSION, Project, ProjectRepository

__all__ = ["FORMAT_VERSION", "Project", "ProjectRepository"]
