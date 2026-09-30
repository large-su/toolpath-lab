"""刀轴高度：让刀**贴着斜面/曲面底走**而不扎进去。

底面是水平面时，"这一层切到 Z"就够了。底面一旦倾斜或弯曲，同一层刀路在不同位置
高度不同，必须逐点算出刀轴该停在哪里；算错的两种后果都很严重：

* 抬得不够 → 刀底切进底面（过切，零件报废）；
* 抬得太多 → 留一层没切掉的台阶。

这一节把"刀在斜面上要抬多少"变成一条可验证的公式。设：

* ``P(offset)``：刀具截面轮廓 —— 距刀轴 ``offset`` 处，刀体比刀尖最低点高多少。
  平底刀 ``P ≡ 0``；球头刀 ``P(o) = r − √(r² − o²)``；圆鼻刀按圆角分段；
* ``g``：底面的高度梯度（``∂Z/∂x, ∂Z/∂y``，朝上为正）。

刀体上"距刀轴 offset、沿梯度方向"的那一点，比刀轴在 (x, y) 处的**刀尖高度**低
``offset · |g| − P(offset)``。要让它不低于底面高度，刀轴必须比该点的底面高度再抬高这么多。
取整个足迹上的最大值，就得到一个只在底面比刀尖高时才生效的保守抬升：

    lift = max(0, max_over_offsets( offset · |g| − P(offset) ))

三条性质都对得上直觉，也在测试里逐条验证：

* 水平面（``g = 0``）→ ``P ≥ 0`` → lift = 0，与老行为完全一致；
* 平底刀（``P ≡ 0``）在坡度 θ 上 → lift = ``r·tanθ``，正是刀底边缘贴住斜面的最小抬升；
* 球头刀 → lift ≈ ``r·tanθ/2``（球面分担掉一半），比平底刀抬得少，符合实际。

抬升只在"该点的底面高于刀尖"时叠加，所以粗加工层（刀尖远在底面之上）不受影响，
只有最底下几层与精修层会真正贴到面上去。
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.tool import Tool, ToolKind

#: 沿足迹扫描的分数位置（0 = 刀轴，1 = 刀底边缘）。
#: 刀体轮廓是凸的，"最贴面"的点通常出现在边缘或某个中间位置，取这几档取最大值足够。
FOOTPRINT_FRACTIONS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
#: 抬升留量系数：再乘一个安全系数，抵消离散（高度场 + 网格光栅化）带来的误差。
LIFT_SAFETY = 1.05


def cutter_profile(tool: Tool) -> tuple[float, float]:
    """刀体轮廓参数 ``(Rc, Rf)``：圆角半径与"平底段外沿"半径。

    * 平底刀：``(0, R)`` —— 整个底面是平的，边缘处比刀尖高 0；
    * 球头刀：``(R, 0)`` —— 没有平底段，圆角一直延伸到刀轴；
    * 圆鼻刀：``(Rc, R − Rc)``。

    于是刀体在距轴 ``o`` 处相对刀尖的高度是::

        P(o) = 0                                  (o ≤ Rf，平底段)
        P(o) = Rc − √(Rc² − (o − Rf)²)            (Rf < o ≤ Rf + Rc = R，圆角段)
    """

    radius = float(tool.radius_mm)
    kind = tool.kind
    if kind is ToolKind.BALL:
        return radius, 0.0
    if kind is ToolKind.BULL:
        corner = float(tool.corner_radius_mm)
        corner = min(max(corner, 0.0), radius)
        return corner, max(0.0, radius - corner)
    return 0.0, radius


def profile_height(tool: Tool, offset: float) -> float:
    """``P(offset)``：距刀轴 ``offset`` 处刀体比刀尖高多少。"""

    corner, flat = cutter_profile(tool)
    radius = flat + corner
    if offset <= flat + 1e-12:
        return 0.0
    if corner <= 1e-12:
        # 平底刀在半径之外没有刀体
        return float("inf") if offset > radius + 1e-12 else 0.0
    inner = max(0.0, corner ** 2 - (offset - flat) ** 2)
    return float(corner - math.sqrt(inner))


def required_lift(tool: Tool, gradient: NDArray[np.float64] | tuple[float, float]) -> float:
    """底面坡度为 ``gradient`` 时，刀轴相对"刀尖贴住该点底面"需要再抬多少（mm）。

    只与坡度的**大小**有关（各向同性的圆足迹），与走刀方向无关。
    """

    gx, gy = float(gradient[0]), float(gradient[1])
    slope = math.hypot(gx, gy)
    if slope <= 1e-9:
        return 0.0
    radius = float(tool.radius_mm)
    best = 0.0
    for fraction in FOOTPRINT_FRACTIONS:
        offset = fraction * radius
        height = profile_height(tool, offset)
        if not math.isfinite(height):
            continue
        best = max(best, offset * slope - height)
    return max(0.0, best) * LIFT_SAFETY


def axis_z_for_point(floor_z: float, gradient: NDArray[np.float64] | tuple[float, float],
                     tool: Tool) -> float:
    """某个 XY 处的刀轴 Z：刀尖贴住该点的底面，再加上防过切的抬升。"""

    return float(floor_z) + required_lift(tool, gradient)


def plane_gradient(normal: NDArray[np.float64] | tuple[float, float, float]
                   ) -> NDArray[np.float64]:
    """由法向求高度梯度 ``(−nx/nz, −ny/nz)``。"""

    nx, ny, nz = float(normal[0]), float(normal[1]), float(normal[2])
    if abs(nz) < 1e-9:  # pragma: no cover - 法向朝上时不会发生
        return np.zeros(2, dtype=np.float64)
    return np.asarray([-nx / nz, -ny / nz], dtype=np.float64)


__all__ = [
    "FOOTPRINT_FRACTIONS",
    "LIFT_SAFETY",
    "axis_z_for_point",
    "cutter_profile",
    "plane_gradient",
    "profile_height",
    "required_lift",
]
