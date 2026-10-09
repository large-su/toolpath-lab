"""2D 轮廓的数据结构。

这一层**不依赖 pyclipper**：它只描述几何，方便在没有 clipper 的地方（测试、序列化、
前端载荷）也能用。与 pyclipper 的整数坐标互转放在 :mod:`clipper` 里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from numpy.typing import NDArray

#: 面积小于这个值（mm²）的环按"退化"丢掉：偏置到快消失时会产生大量针状碎环。
MIN_RING_AREA_MM2 = 1e-6


def _as_points(points: Iterable[Sequence[float]]) -> NDArray[np.float64]:
    array = np.asarray(list(points), dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"轮廓必须是 (N,2) 的点列，收到 {array.shape}")
    if array.shape[0] < 3:
        raise ValueError(f"闭合轮廓至少要 3 个点，收到 {array.shape[0]} 个")
    # 丢掉重复的收尾点：内部统一按"不重复首点"存，避免偏置时多出一段零长边
    if np.allclose(array[0], array[-1]):
        array = array[:-1]
    if array.shape[0] < 3:
        raise ValueError("首尾点重合后不足 3 个有效点")
    return array


@dataclass(frozen=True, slots=True)
class Polygon2D:
    """一条闭合的 2D 折线（不重复首尾点）。

    方向有意义：**外环逆时针（CCW，面积 > 0）、孔环顺时针（CW，面积 < 0）**。
    偏置和布尔的符号全都建立在这个约定上，方向反了会出现"孔越偏越大"的经典错误。
    """

    points: NDArray[np.float64]

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", _as_points(self.points))

    # -- 基本量 ------------------------------------------------------------
    @property
    def area(self) -> float:
        """带符号面积（shoelace）。CCW 为正。"""

        x = self.points[:, 0]
        y = self.points[:, 1]
        return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    @property
    def length(self) -> float:
        """周长（mm）。"""

        delta = np.roll(self.points, -1, axis=0) - self.points
        return float(np.linalg.norm(delta, axis=1).sum())

    @property
    def is_ccw(self) -> bool:
        return self.area > 0.0

    def bounds(self) -> tuple[float, float, float, float]:
        """(x_min, y_min, x_max, y_max)。"""

        return (float(self.points[:, 0].min()), float(self.points[:, 1].min()),
                float(self.points[:, 0].max()), float(self.points[:, 1].max()))

    def centroid(self) -> tuple[float, float]:
        """面积质心（不是顶点平均）。"""

        x = self.points[:, 0]
        y = self.points[:, 1]
        cross = x * np.roll(y, -1) - np.roll(x, -1) * y
        total = float(cross.sum())
        if abs(total) < 1e-12:
            return (float(x.mean()), float(y.mean()))
        cx = float(np.sum((x + np.roll(x, -1)) * cross) / (3.0 * total))
        cy = float(np.sum((y + np.roll(y, -1)) * cross) / (3.0 * total))
        return (cx, cy)

    def contains(self, point: Sequence[float]) -> bool:
        """射线法判点是否在环内（不考虑方向）。"""

        px, py = float(point[0]), float(point[1])
        x = self.points[:, 0]
        y = self.points[:, 1]
        xn, yn = np.roll(x, -1), np.roll(y, -1)
        # 分两步算，避免 numpy 对布尔数组做原地赋值时的副本语义坑
        cond = ((y > py) != (yn > py))
        with np.errstate(divide="ignore", invalid="ignore"):
            x_cross = x + (py - y) * (xn - x) / np.where(yn == y, 1e-300, yn - y)
        return bool(np.any(cond & (px < x_cross)))

    # -- 方向 --------------------------------------------------------------
    def reversed(self) -> "Polygon2D":
        return Polygon2D(self.points[::-1].copy())

    def normalized(self, *, ccw: bool) -> "Polygon2D":
        """把方向统一成 ccw 指定的绕向。"""

        return self if self.is_ccw == ccw else self.reversed()

    # -- 转换 --------------------------------------------------------------
    def tolist(self) -> list[list[float]]:
        return [[round(float(px), 4), round(float(py), 4)] for px, py in self.points]

    @classmethod
    def rectangle(cls, x0: float, y0: float, x1: float, y1: float) -> "Polygon2D":
        """矩形（CCW）。"""

        return cls(np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64))

    @classmethod
    def circle(cls, cx: float, cy: float, radius: float, *, segments: int = 64) -> "Polygon2D":
        """圆按正多边形离散（CCW）。``segments`` 越大越接近真圆。"""

        angles = np.linspace(0.0, 2.0 * np.pi, max(8, int(segments)), endpoint=False)
        return cls(np.column_stack((cx + radius * np.cos(angles), cy + radius * np.sin(angles))))


@dataclass(frozen=True, slots=True)
class Region2D:
    """带孔的加工区域：一个外环 + 若干孔/岛屿。

    "孔"和"岛屿"是同一件事：外环以内、被挖掉的部分。它的方向应当是 CW（负面积）。
    """

    outer: Polygon2D
    holes: tuple[Polygon2D, ...] = ()

    def __post_init__(self) -> None:
        # 方向在这里统一，调用方不必操心；后续偏置的符号才能真正可靠
        object.__setattr__(self, "outer", self.outer.normalized(ccw=True))
        object.__setattr__(self, "holes",
                           tuple(hole.normalized(ccw=False) for hole in self.holes))

    @property
    def area(self) -> float:
        """净面积 = 外环 - 各孔。"""

        return abs(self.outer.area) - sum(abs(hole.area) for hole in self.holes)

    def rings(self) -> tuple[Polygon2D, ...]:
        return (self.outer, *self.holes)

    def bounds(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.outer.bounds()
        for hole in self.holes:
            hx0, hy0, hx1, hy1 = hole.bounds()
            x0, y0 = min(x0, hx0), min(y0, hy0)
            x1, y1 = max(x1, hx1), max(y1, hy1)
        return (x0, y0, x1, y1)

    def tolist(self) -> dict[str, object]:
        return {"outer": self.outer.tolist(), "holes": [h.tolist() for h in self.holes]}

    @classmethod
    def rectangle(cls, x0: float, y0: float, x1: float, y1: float,
                  holes: Iterable[Polygon2D] = ()) -> "Region2D":
        return cls(Polygon2D.rectangle(x0, y0, x1, y1), tuple(holes))
