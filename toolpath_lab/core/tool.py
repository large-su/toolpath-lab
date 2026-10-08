"""刀具几何。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
将来接入球头/圆鼻刀时，残留高度、刀轴姿态也都从这里出发。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

当前支持平底刀、球头刀和圆鼻刀。圆鼻刀通过鼻圆角半径描述底部圆弧，
三种刀具的加工面足迹半径会参与刀路边界偏置。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any, Mapping

import numpy as np

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    spec,
)


class ToolKind(str, Enum):
    """本工程建模的刀具类型。"""

    FLAT = "flat"
    BALL = "ball"
    BULL = "bull"


#: 参数目录里的刀具类型选项；disabled 的项在界面上不可选。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose"),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose"),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS, help="球头刀以刀尖接触，圆鼻刀可设置鼻圆角半径"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("nose_radius_mm", "鼻圆角 Rn", K.FLOAT, 2.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具", visible_if={"kind": ToolKind.BULL.value},
                 help="圆鼻刀底部圆角半径，必须小于刀具半径"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    nose_radius_mm: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.nose_radius_mm) or self.nose_radius_mm < 0:
            raise ParameterError("鼻圆角半径必须是有限非负数")
        if self.kind is ToolKind.BULL and self.nose_radius_mm >= self.radius_mm:
            raise ParameterError("圆鼻刀鼻圆角半径必须小于刀具半径")

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            nose_radius_mm=float(params.get("nose_radius_mm", 0.0)),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return self.nose_radius_mm
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return self.radius_mm - self.corner_radius_mm
        return self.radius_mm

    @property
    def cutting_length_mm(self) -> float:
        """教学刀具模型的轴向切削段，与前端绘制/检测一致。"""

        return min(self.length_mm, self.diameter_mm if self.kind is ToolKind.BALL
                   else min(self.length_mm * 0.65, self.radius_mm * 6))

    @property
    def shank_radius_mm(self) -> float:
        return self.radius_mm * 1.25

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "nose_radius_mm": self.corner_radius_mm if self.kind is ToolKind.BULL else 0.0,
            "footprint_radius_mm": self.footprint_radius_mm,
            "cutting_length_mm": self.cutting_length_mm,
            "shank_radius_mm": self.shank_radius_mm,
        }

    def orientation_clearance_mm(self, tool_axis: Any, surface_normal: Any) -> float:
        """Return a conservative lift for a tilted flat/bull cutter.

        A ball nose is tangent at its tip and needs no lift.  For a flat or
        bull nose cutter, tilting the tool makes one side of its bottom face
        lower than the nominal contact point.  Lifting by ``R sin(theta)``
        keeps the circular footprint tangent to the local surface plane.
        This is a local geometric guard, not a full swept-volume collision
        solver for high-curvature surfaces.
        """

        if self.kind is ToolKind.BALL:
            return 0.0
        axis = np.asarray(tool_axis, dtype=np.float64).reshape(3)
        normal = np.asarray(surface_normal, dtype=np.float64).reshape(3)
        axis_norm = float(np.linalg.norm(axis))
        normal_norm = float(np.linalg.norm(normal))
        if axis_norm <= 1e-9 or normal_norm <= 1e-9:
            return 0.0
        alignment = float(np.dot(axis, normal) / (axis_norm * normal_norm))
        sine = float(np.sqrt(max(0.0, 1.0 - np.clip(alignment, -1.0, 1.0) ** 2)))
        return self.radius_mm * sine
