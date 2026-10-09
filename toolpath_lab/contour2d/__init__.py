"""2.5D 轮廓加工层（pyclipper 引擎）。

职责边界（**不要越界**）：

* 本包**只做 2D 轮廓**：布尔运算、偏置（刀具半径补偿）、环切刀路。
* 不读模型文件、不碰 BRep、不做三角网格、不做 Z 向分层 —— 那些属于 ``brep``（OCP）。
* 输入永远是"已经落到某个 Z 高度上的平面轮廓"；把 BRep 变成这种轮廓是 OCP 层的活。

坐标系约定：XY 平面，单位 mm，右手系，逆时针为正（外环 CCW、孔环 CW）。
"""

from toolpath_lab.contour2d.clipper import ContourClipper
from toolpath_lab.contour2d.pocket import PocketResult, RingPass, ring_passes, rotate_to
from toolpath_lab.contour2d.polygon import Polygon2D, Region2D

__all__ = [
    "ContourClipper",
    "Polygon2D",
    "PocketResult",
    "Region2D",
    "RingPass",
    "ring_passes",
    "rotate_to",
]
