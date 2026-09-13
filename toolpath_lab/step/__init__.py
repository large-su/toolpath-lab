"""STEP（ISO 10303-21）读取层。

本包把 ``.step`` / ``.stp`` 文件读成一个可渲染、可继续做几何计算的三角网格，
只依赖标准库与 numpy——不引入 OpenCASCADE 之类的重型几何内核。

分层：

``lexer`` / ``parser``
    纯文本层：把 ``#123=ADVANCED_FACE(...)`` 这样的实例表解析成实体图。
``geometry``
    几何层：把实体图里的点、方向、坐标系、曲线、曲面解释成可求值的对象。
``tessellate``
    网格层：把每个面离散成三角形，附带面拓扑（用于拾取与特征识别）。
``reader``
    入口：``read_step(path)`` 返回 :class:`~toolpath_lab.step.tessellate.TessellatedModel`。

设计取舍见 ``docs/step.md``：解析覆盖最常见的实体集合，遇到不认识的实体只记录警告，
不抛异常——工业文件的实体集合远比任何实现都大。
"""

from __future__ import annotations

from toolpath_lab.step.errors import StepError, StepFormatError, StepSizeError
from toolpath_lab.step.parser import StepEntity, StepFile, parse_step
from toolpath_lab.step.reader import read_step, read_step_bytes
from toolpath_lab.step.tessellate import FaceRecord, TessellatedModel

__all__ = [
    "FaceRecord",
    "StepEntity",
    "StepError",
    "StepFile",
    "StepFormatError",
    "StepSizeError",
    "TessellatedModel",
    "parse_step",
    "read_step",
    "read_step_bytes",
]
