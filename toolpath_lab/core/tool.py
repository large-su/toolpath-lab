"""刀具几何与切削参数。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
残留高度、刀轴姿态也都从这里出发。除几何外，本模块还提供**切削参数推荐**：
按工件材料查表（切削线速度 Vc、每齿进给 fz），由刀具直径推算推荐主轴转速与进给，
对应 UG 数控编程的"切削三要素"选择。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any, Mapping

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


#: 参数目录里的刀具类型选项。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose"),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose"),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}

#: 工件材料 → 切削参数（硬质合金立铣刀的经验推荐值）。
#: Vc 为切削线速度（m/min），fz 为每齿进给（mm/z）；数值按常见加工工况取保守中值。
MATERIALS: dict[str, dict[str, Any]] = {
    "aluminum": {"label": "铝合金", "cutting_speed_m_per_min": 250.0, "feed_per_tooth_mm": 0.05},
    "steel": {"label": "碳钢", "cutting_speed_m_per_min": 80.0, "feed_per_tooth_mm": 0.03},
    "stainless": {"label": "不锈钢", "cutting_speed_m_per_min": 45.0, "feed_per_tooth_mm": 0.02},
    "titanium": {"label": "钛合金", "cutting_speed_m_per_min": 30.0, "feed_per_tooth_mm": 0.015},
    "copper": {"label": "铜", "cutting_speed_m_per_min": 120.0, "feed_per_tooth_mm": 0.04},
    "cast_iron": {"label": "铸铁", "cutting_speed_m_per_min": 60.0, "feed_per_tooth_mm": 0.03},
}
MATERIAL_CHOICES: tuple[Choice, ...] = tuple(
    Choice(key, data["label"]) for key, data in MATERIALS.items()
)
DEFAULT_MATERIAL = "aluminum"


def tool_parameters() -> ParameterSet:
    """刀具分组的参数声明（同时驱动界面与请求校验）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS, help="平底刀 / 球头刀 / 圆鼻刀三种刀具类型"),
            spec("material", "工件材料", K.CHOICE, DEFAULT_MATERIAL, group="刀具",
                 choices=MATERIAL_CHOICES,
                 help="用于推荐主轴转速与进给（硬质合金立铣刀经验值）"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("corner_mm", "刀尖圆角半径 Rc", K.FLOAT, 0.0, minimum=0.0, maximum=50.0,
                 step=0.5, unit="mm", group="刀具", help="仅圆鼻刀生效；球头刀的圆角恒等于刀具半径"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
            spec("stepover_mm", "行距（步距）", K.FLOAT, 1.0, minimum=0.1, maximum=50.0,
                 step=0.1, unit="mm", group="刀具",
                 help="相邻刀轨的间距；配合刀尖圆角几何估算加工表面残留高度"),
        )
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    corner_mm: float = 0.0
    stepover_mm: float = 1.0
    material: str = DEFAULT_MATERIAL

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if not isfinite(self.corner_mm) or self.corner_mm < 0:
            raise ParameterError("刀具圆角半径必须是有限非负数")
        if self.corner_mm >= self.radius_mm:
            raise ParameterError("刀具圆角半径必须小于刀具半径")
        if not isfinite(self.stepover_mm) or self.stepover_mm <= 0:
            raise ParameterError("行距必须是有限正数")
        if self.material not in MATERIALS:
            raise ParameterError(f"未知工件材料 {self.material!r}")

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
            corner_mm=float(params.get("corner_mm", 0.0)),
            stepover_mm=float(params.get("stepover_mm", 1.0)),
            material=str(params.get("material", DEFAULT_MATERIAL)),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径，圆鼻刀取名义圆角 Rc）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            return self.corner_mm
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_mm)
        return self.radius_mm

    # -- 切削参数推荐 -----------------------------------------------------
    @property
    def material_label(self) -> str:
        return MATERIALS[self.material]["label"]

    @property
    def cutting_speed_m_per_min(self) -> float:
        """该材料推荐的切削线速度 Vc（m/min）。"""

        return float(MATERIALS[self.material]["cutting_speed_m_per_min"])

    @property
    def feed_per_tooth_mm(self) -> float:
        """该材料推荐的每齿进给 fz（mm/z）。"""

        return float(MATERIALS[self.material]["feed_per_tooth_mm"])

    @property
    def flute_count(self) -> int:
        """经验齿数：球头刀取 2 刃；平底/圆鼻刀小径 2 刃、大径 4 刃。"""

        if self.kind is ToolKind.BALL:
            return 2
        return 4 if self.diameter_mm >= 8.0 else 2

    def recommended_spindle_speed_rpm(self) -> float:
        """推荐主轴转速 n = 1000·Vc / (π·D)（rpm）。"""

        return round(1000.0 * self.cutting_speed_m_per_min / (math.pi * self.diameter_mm), 0)

    def recommended_feed_mm_per_min(self) -> float:
        """推荐进给 F = n · z · fz（mm/min）。"""

        return round(
            self.recommended_spindle_speed_rpm() * self.flute_count * self.feed_per_tooth_mm,
            0,
        )

    # -- 残留高度 ---------------------------------------------------------
    def residual_height_mm(self, stepover: float | None = None) -> float:
        """估算相邻刀轨之间的残留高度（加工表面质量的核心指标）。

        平底刀在平面加工中刀轨完全覆盖，残留为零；
        球头刀按圆弧截面估算：h = R - sqrt(R^2 - (s/2)^2)；
        圆鼻刀底部有半径 R-Rc 的平底：行距不超过 2(R-Rc) 时无残留，
        超过后由刀尖圆角圆弧决定残留。
        """

        s = max(0.0, self.stepover_mm if stepover is None else stepover)
        if self.kind is ToolKind.FLAT:
            return 0.0
        radius = self.radius_mm
        if self.kind is ToolKind.BALL:
            if s >= 2.0 * radius:
                return radius
            return radius - math.sqrt(radius * radius - (s / 2.0) ** 2)
        # 圆鼻刀：平底半径 base = R - Rc，圆角半径 Rc
        corner = self.corner_radius_mm
        base = max(0.0, radius - corner)
        if s <= 2.0 * base:
            return 0.0
        if s >= 2.0 * radius:
            return corner
        half = (s - 2.0 * base) / 2.0
        if half >= corner:
            return corner
        return corner - math.sqrt(corner * corner - half * half)

    def recommended_stepover_mm(self, target_height: float = 0.02) -> float:
        """由允许残留高度反推最大行距（残留高度公式的反函数）。

        target_height 默认取 0.02 mm，是精加工常见的表面残留要求。
        """

        h = max(0.0, target_height)
        if self.kind is ToolKind.FLAT:
            return self.diameter_mm
        radius = self.radius_mm
        if self.kind is ToolKind.BALL:
            if h >= radius:
                return 2.0 * radius
            return 2.0 * math.sqrt(radius * radius - (radius - h) ** 2)
        corner = self.corner_radius_mm
        base = max(0.0, radius - corner)
        if h <= 0.0:
            return 2.0 * base
        if h >= corner:
            return 2.0 * radius
        return 2.0 * base + 2.0 * math.sqrt(corner * corner - (corner - h) ** 2)

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        return {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "corner_radius_mm": self.corner_radius_mm,
            "length_mm": self.length_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
            "stepover_mm": self.stepover_mm,
            "residual_height_mm": self.residual_height_mm(),
            "recommended_stepover_mm": self.recommended_stepover_mm(),
            "material": self.material,
            "material_label": self.material_label,
            "cutting_speed_m_per_min": self.cutting_speed_m_per_min,
            "feed_per_tooth_mm": self.feed_per_tooth_mm,
            "flute_count": self.flute_count,
            "recommended_spindle_speed_rpm": self.recommended_spindle_speed_rpm(),
            "recommended_feed_mm_per_min": self.recommended_feed_mm_per_min(),
        }
