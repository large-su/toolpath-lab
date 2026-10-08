"""毛坯（stock）。

开粗之前先要知道"材料从哪里开始切"。这里把毛坯做成最小的抽象：一个轴对齐长方体，
只需要回答它的六个边界与顶面高度：

- none  不使用毛坯（默认，等价于以前的行为）；
- model 模型的最小六面体包容体（轴对齐包围盒），可整体外扩、顶面抬高。

毛坯只提供"材料在哪、多高"，具体怎么分层走刀由策略决定（见 planning/raster.py 的切深参数）。
这样将来要加圆柱毛坯或"铸件毛坯"，只要再注册一个返回 box() 的类即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.parameters import (
    ParameterKind as K,
    ParameterSet,
    field_values,
    spec,
)
from toolpath_lab.core.registry import Registry

STOCKS: Registry[type["Stock"]] = Registry("stock")

#: box() 的六个边界 (x_min, x_max, y_min, y_max, z_min, z_max) 的类型别名。
Box = tuple[float, float, float, float, float, float]


@dataclass(frozen=True, slots=True)
class Stock:
    """所有毛坯的基类。"""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[ParameterSet] = ParameterSet()
    #: 需要先导入模型才能构造（例如模型包容体）。
    needs_model: ClassVar[bool] = False

    def box(self) -> Box | None:
        """毛坯的轴对齐范围；None 表示不使用毛坯。"""

        return None

    @property
    def is_set(self) -> bool:
        return self.box() is not None

    @property
    def top_mm(self) -> float:
        """毛坯顶面高度：分层粗加工就是从这一层开始往下切。"""

        bounds = self._require_box()
        return bounds[5]

    @property
    def bottom_mm(self) -> float:
        return self._require_box()[4]

    def _require_box(self) -> Box:
        bounds = self.box()
        if bounds is None:
            raise ParameterError("这一份配置没有毛坯：请先在「毛坯」里选择模型包容体")
        return bounds

    def note(self) -> str:
        return f"毛坯：{self.label}"

    def to_params(self) -> dict[str, Any]:
        return field_values(self)

    def describe(self) -> dict[str, Any]:
        bounds = self.box()
        described: dict[str, Any] = {
            "kind": self.id,
            "label": self.label,
            "description": self.description,
            "parameters": self.to_params(),
            "is_set": bounds is not None,
            "note": self.note(),
            "bounds_mm": None,
            "size_mm": None,
            "top_mm": None,
            "bottom_mm": None,
        }
        if bounds is not None:
            x_min, x_max, y_min, y_max, z_min, z_max = bounds
            described.update(
                {
                    "bounds_mm": [
                        [round(x_min, 4), round(x_max, 4)],
                        [round(y_min, 4), round(y_max, 4)],
                        [round(z_min, 4), round(z_max, 4)],
                    ],
                    "size_mm": [
                        round(x_max - x_min, 4),
                        round(y_max - y_min, 4),
                        round(z_max - z_min, 4),
                    ],
                    "top_mm": round(z_max, 4),
                    "bottom_mm": round(z_min, 4),
                }
            )
        return described


@STOCKS.register
@dataclass(frozen=True, slots=True)
class NoStock(Stock):
    """不使用毛坯：刀路直接沿加工面走，不做分层。"""

    id: ClassVar[str] = "none"
    label: ClassVar[str] = "不使用毛坯"
    description: ClassVar[str] = "不做分层，刀路直接沿加工面走（切深不起作用）"

    def note(self) -> str:
        return "未定义毛坯，按单层走刀"


@STOCKS.register
@dataclass(frozen=True, slots=True)
class ModelStock(Stock):
    """模型的最小六面体包容体（轴对齐包围盒）。

    这是最省事也最常用的一种毛坯：导入零件之后，包住它的最小长方体就是毛坯，
    尺寸不用手工量。机器实际上是从这个长方体的顶面往下把材料切掉的。
    """

    margin_xy_mm: float = 0.0
    margin_top_mm: float = 0.0
    _model: Any = field(default=None, repr=False, compare=False)

    id: ClassVar[str] = "model"
    label: ClassVar[str] = "模型包容体"
    description: ClassVar[str] = "模型的最小六面体包容体（轴对齐包围盒），可外扩与抬高顶面"
    needs_model: ClassVar[bool] = True
    parameters: ClassVar[ParameterSet] = ParameterSet(
        (
            spec("margin_xy_mm", "XY 外扩", K.FLOAT, 0.0, minimum=0.0, maximum=100.0,
                 step=1.0, unit="mm", group="毛坯",
                 help="毛坯四周相对零件放大多少（0 就是紧贴零件的最小包容体）"),
            spec("margin_top_mm", "顶面抬高", K.FLOAT, 0.0, minimum=0.0, maximum=100.0,
                 step=0.5, unit="mm", group="毛坯",
                 help="毛坯顶面相对零件最高点抬高多少，相当于留出加工余量"),
        )
    )

    def box(self) -> Box:
        if self._model is None:
            raise ParameterError("模型包容体需要先导入模型")
        x_min, x_max, y_min, y_max, z_min, z_max = self._model.mesh.bounds
        margin = float(self.margin_xy_mm)
        return (
            x_min - margin, x_max + margin,
            y_min - margin, y_max + margin,
            z_min, z_max + float(self.margin_top_mm),
        )

    def note(self) -> str:
        x_min, x_max, y_min, y_max, _, z_max = self.box()
        size = (x_max - x_min, y_max - y_min, z_max - self.bottom_mm)
        return (
            "毛坯：模型包容体 "
            f"{size[0]:g} × {size[1]:g} × {size[2]:g} mm，顶面 Z = {z_max:g} mm"
        )


def build_stock(
    kind: str,
    raw_parameters: Mapping[str, Any] | None = None,
    *,
    model: Any = None,
) -> Stock:
    """由接口参数构造一个已注册的毛坯。"""

    cls = STOCKS.get(kind)
    values = cls.parameters.coerce(raw_parameters)
    if getattr(cls, "needs_model", False):
        if model is None:
            raise ParameterError(
                f"毛坯 {kind!r} 需要先导入模型：请在请求里给出 model.id，"
                "或先用 POST /api/models 上传 STL"
            )
        return cls(_model=model, **values)
    return cls(**values)


def stock_catalog() -> list[dict[str, Any]]:
    return STOCKS.catalog()
