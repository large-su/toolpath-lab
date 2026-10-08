"""加工面（surfaces）。

到目前为止，加工面都是固定的 XY 平面（Z = 0）。这一层把"加工面"变成一个可替换的对象：
它只需要回答一个问题——**给定 XY，加工面的高度 Z 是多少**：

    z = surface.heights(xy)

于是同一个栅格刀路策略既能加工平面，也能加工斜面、波浪面，或者导入的 STL 模型：

- flat  平面：Z = 常量（与旧行为完全一致）；
- slope 斜面：Z 沿某个方向线性抬升；
- wave  波浪面：Z 沿某个方向正弦起伏，用来看刀路跟随曲面的效果；
- model 导入模型：Z 来自模型的 Z-map 高度场（见 core/mesh.py）。

刀轴恒为 +Z（三轴），所以曲面是靠"抬高刀尖"来跟随的；刀路在 XY 上仍按区域轮廓裁剪。
曲面上的切宽按 XY 投影计算，实际切宽会随表面倾角变化——策略里会给出这条提醒。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

import numpy as np
from numpy.typing import NDArray

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.mathutil import direction_2d
from toolpath_lab.core.mesh import (
    DEFAULT_RESOLUTION_MM,
    HEIGHT_PICKS,
    PICK_LABELS,
    PICK_TOP,
    StoredModel,
)
from toolpath_lab.core.parameters import (
    Choice,
    ParameterKind as K,
    ParameterSet,
    field_values,
    spec,
)
from toolpath_lab.core.registry import Registry

SURFACES: Registry[type["Surface"]] = Registry("surface")

#: 在区域上取"最高点/最低点"时的探测网格分辨率（每边的节点数）。
_PROBE_NODES = 24


@dataclass(frozen=True, slots=True)
class Surface:
    """所有加工面的基类。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()
    #: 需要先导入模型才能构造（例如 model 曲面）。
    needs_model: ClassVar[bool] = False

    def heights(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        """(N, 2) 的 XY → (N,) 的加工面高度 Z。"""

        raise NotImplementedError

    @property
    def is_planar(self) -> bool:
        """平面加工面：刀路只需两个端点，不需要离散。"""

        return False

    def sample_extremes(self, polygon: NDArray[np.float64]) -> tuple[float, float]:
        """加工面在给定区域上的 (最低, 最高) 高度，用于确定安全平面。"""

        probes = probe_points(polygon)
        if probes.size == 0:
            return (0.0, 0.0)
        heights = np.asarray(self.heights(probes), dtype=np.float64)
        heights = heights[np.isfinite(heights)]
        if heights.size == 0:
            return (0.0, 0.0)
        return (float(heights.min()), float(heights.max()))

    def warnings(self) -> tuple[str, ...]:
        """构造加工面时发现的问题（例如 Z-map 被放宽），随响应返回。"""

        return ()

    def note(self) -> str:
        """一句中文说明，写进刀路 notes。"""

        return f"加工面：{self.label}"

    def to_params(self) -> dict[str, Any]:
        return field_values(self)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.id,
            "label": self.label,
            "description": self.description,
            "parameters": self.to_params(),
            "is_planar": self.is_planar,
            "note": self.note(),
            "warnings": list(self.warnings()),
        }


def probe_points(polygon: NDArray[np.float64], nodes: int = _PROBE_NODES) -> NDArray[np.float64]:
    """区域轮廓顶点 + 包围盒上的粗网格：用来估计加工面的高度范围。"""

    points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    if points.size == 0:
        return np.zeros((0, 2), dtype=np.float64)
    x_min, x_max = float(points[:, 0].min()), float(points[:, 0].max())
    y_min, y_max = float(points[:, 1].min()), float(points[:, 1].max())
    xs = np.linspace(x_min, x_max, max(int(nodes), 2))
    ys = np.linspace(y_min, y_max, max(int(nodes), 2))
    grid_x, grid_y = np.meshgrid(xs, ys)
    grid = np.column_stack((grid_x.ravel(), grid_y.ravel()))
    return np.vstack((points, grid))


def _offset_spec() -> Any:
    """所有加工面共有的 Z 向偏置（正值留余量，负值多切一点）。"""

    return spec(
        "z_offset_mm", "Z 偏置", K.FLOAT, 0.0, minimum=-100.0, maximum=100.0,
        step=0.5, unit="mm", group="曲面",
        help="在加工面上整体抬升（留余量）或下降（多切一点）的量",
    )


@SURFACES.register
@dataclass(frozen=True, slots=True)
class FlatSurface(Surface):
    """平面加工面：Z = z_offset（默认 0，也就是原来的行为）。"""

    z_offset_mm: float = 0.0

    id: ClassVar[str] = "flat"
    label: ClassVar[str] = "平面"
    description: ClassVar[str] = "默认加工面：XY 平面上的一层，刀路只需两个端点"
    parameters: ClassVar[ParameterSet] = ParameterSet((_offset_spec(),))

    @property
    def is_planar(self) -> bool:
        return True

    def heights(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        return np.full(planar.shape[0], float(self.z_offset_mm), dtype=np.float64)

    def sample_extremes(self, polygon: NDArray[np.float64]) -> tuple[float, float]:
        return (float(self.z_offset_mm), float(self.z_offset_mm))

    def note(self) -> str:
        return f"加工面：平面（Z = {self.z_offset_mm:g} mm），两轴半加工"


@SURFACES.register
@dataclass(frozen=True, slots=True)
class SlopeSurface(Surface):
    """斜面：沿 direction_deg 方向按 tilt_deg 线性抬升。"""

    tilt_deg: float = 10.0
    direction_deg: float = 0.0
    z_offset_mm: float = 0.0

    id: ClassVar[str] = "slope"
    label: ClassVar[str] = "斜面"
    description: ClassVar[str] = "按倾角线性倾斜的平面，用来看刀路在斜面上的抬刀"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("tilt_deg", "倾角", K.FLOAT, 10.0, minimum=-80.0, maximum=80.0,
                 step=1.0, unit="°", group="曲面"),
            spec("direction_deg", "倾斜方向", K.FLOAT, 0.0, minimum=0.0, maximum=360.0,
                 step=5.0, unit="°", group="曲面", help="高度沿这个方向升高"),
            _offset_spec(),
        )
    )

    def heights(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        axis = direction_2d(self.direction_deg)
        along = planar @ axis
        return float(self.z_offset_mm) + math.tan(math.radians(self.tilt_deg)) * along

    def note(self) -> str:
        return (
            f"加工面：斜面（倾角 {self.tilt_deg:g}°，倾斜方向 {self.direction_deg:g}°），"
            "三轴联动"
        )


@SURFACES.register
@dataclass(frozen=True, slots=True)
class WaveSurface(Surface):
    """波浪面：沿 direction_deg 方向的正弦起伏。"""

    amplitude_mm: float = 5.0
    wavelength_mm: float = 40.0
    direction_deg: float = 0.0
    z_offset_mm: float = 0.0

    id: ClassVar[str] = "wave"
    label: ClassVar[str] = "波浪面"
    description: ClassVar[str] = "正弦起伏的曲面，用来观察刀路随曲面做三轴抬降"
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("amplitude_mm", "振幅", K.FLOAT, 5.0, minimum=0.0, maximum=100.0,
                 step=0.5, unit="mm", group="曲面", help="波峰相对中面的高度"),
            spec("wavelength_mm", "波长", K.FLOAT, 40.0, minimum=1.0, maximum=1000.0,
                 step=1.0, unit="mm", group="曲面"),
            spec("direction_deg", "起伏方向", K.FLOAT, 0.0, minimum=0.0, maximum=360.0,
                 step=5.0, unit="°", group="曲面"),
            _offset_spec(),
        )
    )

    def heights(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        axis = direction_2d(self.direction_deg)
        along = planar @ axis
        wave = np.sin(2.0 * math.pi * along / max(float(self.wavelength_mm), 1e-6))
        return float(self.z_offset_mm) + float(self.amplitude_mm) * wave

    def sample_extremes(self, polygon: NDArray[np.float64]) -> tuple[float, float]:
        half = abs(float(self.amplitude_mm))
        return (float(self.z_offset_mm) - half, float(self.z_offset_mm) + half)

    def note(self) -> str:
        return (
            f"加工面：波浪面（振幅 {self.amplitude_mm:g} mm、波长 {self.wavelength_mm:g} mm、"
            f"起伏方向 {self.direction_deg:g}°），三轴联动"
        )


@SURFACES.register
@dataclass(frozen=True, slots=True)
class ModelSurface(Surface):
    """导入模型：高度来自模型的 Z-map（top = 最高面，bottom = 最低面）。"""

    resolution_mm: float = DEFAULT_RESOLUTION_MM
    pick: str = PICK_TOP
    z_offset_mm: float = 0.0
    _model: StoredModel | None = field(default=None, repr=False, compare=False)

    id: ClassVar[str] = "model"
    label: ClassVar[str] = "导入模型"
    description: ClassVar[str] = "用导入模型的表面（Z-map）作为加工面，刀路沿模型三轴抬降"
    needs_model: ClassVar[bool] = True
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("resolution_mm", "Z-map 分辨率", K.FLOAT, DEFAULT_RESOLUTION_MM,
                 minimum=0.05, maximum=10.0, step=0.05, unit="mm", group="曲面",
                 help="高度场的采样间距：越小越贴合模型，内存与耗时越高"),
            spec("pick", "取面方式", K.CHOICE, PICK_TOP, group="曲面",
                 choices=tuple(Choice(value, label) for value, label in HEIGHT_PICKS),
                 help="最高面用于凸台/外表面，最低面用于凹腔/型腔底面"),
            _offset_spec(),
        )
    )

    def __post_init__(self) -> None:
        if self._model is None:
            raise ParameterError("导入模型加工面需要先选择一个模型")
        # 提前光栅化一次：参数或模型有问题时立刻失败，而不是等到规划中途。
        self._model.height_field(self.resolution_mm, self.pick)

    def heights(self, points_xy: NDArray[np.float64]) -> NDArray[np.float64]:
        planar = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        if planar.size == 0:
            return np.zeros(0, dtype=np.float64)
        field = self._model.height_field(self.resolution_mm, self.pick)  # type: ignore[union-attr]
        return field.heights(planar) + float(self.z_offset_mm)

    def sample_extremes(self, polygon: NDArray[np.float64]) -> tuple[float, float]:
        """只统计区域包围盒内的节点：安全平面不必高到模型另一头去。"""

        field = self._model.height_field(self.resolution_mm, self.pick)  # type: ignore[union-attr]
        points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
        if points.size == 0:
            return (float(self.z_offset_mm), float(self.z_offset_mm))
        x_min, x_max = float(points[:, 0].min()), float(points[:, 0].max())
        y_min, y_max = float(points[:, 1].min()), float(points[:, 1].max())
        mask_x = (field.xs >= x_min) & (field.xs <= x_max)
        mask_y = (field.ys >= y_min) & (field.ys <= y_max)
        values = field.zs[np.ix_(mask_y, mask_x)]
        if values.size == 0:
            low, high = field.z_range_mm
        else:
            low, high = float(values.min()), float(values.max())
        return (low + float(self.z_offset_mm), high + float(self.z_offset_mm))

    def warnings(self) -> tuple[str, ...]:
        if self._model is None:
            return ()
        return self._model.field_warnings(self.resolution_mm, self.pick)

    def note(self) -> str:
        model = self._model
        name = model.name if model is not None else "?"
        return (
            f"加工面：导入模型 {name}（Z-map 分辨率 {self.resolution_mm:g} mm，"
            f"{PICK_LABELS.get(self.pick, self.pick)}），三轴联动"
        )

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        model = self._model
        described["model"] = model.describe() if model is not None else None
        if model is not None:
            described["height_field"] = model.height_field(
                self.resolution_mm, self.pick
            ).describe()
        return described


def build_surface(
    kind: str,
    raw_parameters: Mapping[str, Any] | None = None,
    *,
    model: StoredModel | None = None,
) -> Surface:
    """由接口参数构造一个已注册的加工面。"""

    cls = SURFACES.get(kind)
    values = cls.parameters.coerce(raw_parameters)
    if getattr(cls, "needs_model", False):
        if model is None:
            raise ParameterError(
                f"加工面 {kind!r} 需要先导入模型：请在请求里给出 model.id，"
                "或先用 POST /api/models 上传 STL"
            )
        return cls(_model=model, **values)
    return cls(**values)


def surface_catalog() -> list[dict[str, Any]]:
    return SURFACES.catalog()
