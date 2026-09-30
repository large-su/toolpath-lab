"""CAM 规划层：从"选中的面 + 加工参数"生成刀路。

分层与原有 ``planning`` 完全一致——本包不依赖 HTTP 与界面，可以单独调用、单独测试：

``parameters``
    加工参数声明（进给、转速、切深、步距、余量、刀具半径、安全高度……）与刀具构造；
``boundary``
    把选中的平面面变成加工区域：外轮廓 + 岛屿，以及用于区域运算的栅格掩码；
``face_mill``
    平面铣：分层往复把一块平面区域铣平；
``pocket_mill``
    型腔铣：按层做等距环切（contour-parallel），并把岛屿绕开；
``service``
    一次工序的完整流程：解析请求 → 拾取特征 → 规划 → 统计，供 HTTP 层调用。
"""

from __future__ import annotations

from toolpath_lab.cam.boundary import MachiningRegion, region_from_face
from toolpath_lab.cam.parameters import (
    CAM_FIXED,
    cam_parameters,
    controller_parameters,
    tool_from_cam_parameters,
)
from toolpath_lab.cam.service import (
    CAMOperationRequest,
    CAMOperationResult,
    execute_operation,
    planning_catalog,
)

__all__ = [
    "CAMOperationRequest",
    "CAMOperationResult",
    "CAM_FIXED",
    "MachiningRegion",
    "cam_parameters",
    "controller_parameters",
    "execute_operation",
    "planning_catalog",
    "region_from_face",
    "tool_from_cam_parameters",
]
