"""STEP 读取层抛出的异常。

异常类型刻意分成三类，好让 HTTP 层能给出不同的状态码与提示：
格式错误（400）、文件过大（413）、以及"格式合法但本实现不支持"（422）。
"""

from __future__ import annotations

from toolpath_lab.core.errors import ToolpathLabError


class StepError(ToolpathLabError):
    """STEP 读取层所有异常的基类。"""


class StepFormatError(StepError, ValueError):
    """文件不是合法的 ISO 10303-21（缺 DATA 段、实例表语法错误等）。"""


class StepSizeError(StepError, ValueError):
    """文件超过配置的体积上限，或实体数量过多。"""


class StepUnsupportedError(StepError, RuntimeError):
    """文件本身合法，但不含本实现能够离散的几何（例如只有二维图纸）。"""
