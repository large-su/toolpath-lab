"""BRep 层的错误类型。

上层的 HTTP 状态码直接按这些类型映射，所以它们要能区分"文件坏了"、
"文件太大"和"文件里没有可加工的几何"三种情况。
"""

from __future__ import annotations


class BrepError(RuntimeError):
    """BRep 层的基类错误。"""


class BrepFormatError(BrepError, ValueError):
    """文件格式不对、读不出来、或者根本不是实体模型（HTTP 400）。

    同时继承 ``ValueError``：格式问题本质就是"传进来的值不对"，
    这样调用方原有的 ``except ValueError`` 也照样能接住，不用改一圈。
    """


class BrepSizeError(BrepError):
    """文件或几何规模超限（HTTP 413）。"""


class BrepUnsupportedError(BrepError):
    """读出来了，但没有可离散/可加工的几何（HTTP 422）。"""


__all__ = ["BrepError", "BrepFormatError", "BrepSizeError", "BrepUnsupportedError"]
