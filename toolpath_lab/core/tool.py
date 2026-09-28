"""刀具几何与刀具库数据模型。

刀具不是装饰：栅格刀路的边界偏置量由"刀具在加工面上的足迹半径"决定，
将来接入球头/圆鼻刀时，残留高度、刀轴姿态也都从这里出发。

===========  ==================  =================  =======================
类型         底面半径 Rf        圆角半径 Rc        足迹半径（用于偏置）
===========  ==================  =================  =======================
flat         R                   -                  R
ball         -                   R                  0
bull         R - Rc              Rc                 R - Rc
===========  ==================  =================  =======================

本模块有两层：

1. :class:`Tool` —— **计算用**的刀具几何。刀路、仿真、NC 头注释都只认它，
   它不知道刀具库的存在，也不知道刀具叫什么名字；
2. :class:`ToolRecord` —— **刀具库里的一个条目**：id + 自定义名称 + 类型 + 参数值。
   它只负责"存"，需要几何时由 :func:`build_tool` 换成 :class:`Tool`。

界面上的刀具类型目录（UG NX 风格的铣刀 / 钻头 / 丝锥 / 铰刀 / 镗刀）由
:data:`TOOL_TYPES` 与 :func:`tool_type_parameters` 声明，前端据此自动生成控件，
与项目其它部分"一份声明驱动界面、校验与文档"的做法一致。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
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
    """刀路算法认识的刀具几何类型。"""

    FLAT = "flat"
    BALL = "ball"
    BULL = "bull"


class ToolType(str, Enum):
    """刀具库里的刀具类型（比几何类型多，因为钻头/丝锥要各自的参数）。"""

    FLAT_END_MILL = "flat_end_mill"
    BALL_END_MILL = "ball_end_mill"
    BULL_NOSE_MILL = "bull_nose_mill"
    TAPER_MILL = "taper_mill"
    DRILL = "drill"
    TAP = "tap"
    REAMER = "reamer"
    BORING_BAR = "boring_bar"


#: 参数目录里的刀具类型选项；disabled 的项在界面上不可选。
TOOL_KINDS: tuple[Choice, ...] = (
    Choice(ToolKind.FLAT.value, "平底刀 Flat end mill"),
    Choice(ToolKind.BALL.value, "球头刀 Ball nose（待拓展）", disabled=True),
    Choice(ToolKind.BULL.value, "圆鼻刀 Bull nose（待拓展）", disabled=True),
)

TOOL_KIND_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_KINDS}

#: 刀具库的类型目录。顺序就是界面下拉框的顺序：铣刀在前，孔加工刀具在后。
TOOL_TYPES: tuple[Choice, ...] = (
    Choice(ToolType.FLAT_END_MILL.value, "平底刀 Flat end mill"),
    Choice(ToolType.BALL_END_MILL.value, "球头刀 Ball nose"),
    Choice(ToolType.BULL_NOSE_MILL.value, "圆鼻刀 Bull nose"),
    Choice(ToolType.TAPER_MILL.value, "锥度铣刀 Taper mill"),
    Choice(ToolType.DRILL.value, "钻头 Drill"),
    Choice(ToolType.TAP.value, "丝锥 Tap"),
    Choice(ToolType.REAMER.value, "铰刀 Reamer"),
    Choice(ToolType.BORING_BAR.value, "镗刀 Boring bar"),
)

TOOL_TYPE_LABELS: dict[str, str] = {choice.value: choice.label for choice in TOOL_TYPES}

#: 每种类型对应的**刀路几何**。钻头/丝锥等孔加工刀具在刀路里按平底圆柱处理
#: （2.5 轴只开放平底刀，曲面刀路认 flat/ball/bull 三种）。
TOOL_KIND_BY_TYPE: dict[str, ToolKind] = {
    ToolType.FLAT_END_MILL.value: ToolKind.FLAT,
    ToolType.BALL_END_MILL.value: ToolKind.BALL,
    ToolType.BULL_NOSE_MILL.value: ToolKind.BULL,
    ToolType.TAPER_MILL.value: ToolKind.FLAT,
    ToolType.DRILL.value: ToolKind.FLAT,
    ToolType.TAP.value: ToolKind.FLAT,
    ToolType.REAMER.value: ToolKind.FLAT,
    ToolType.BORING_BAR.value: ToolKind.FLAT,
}

#: 库里保存的参数键（与 CAM 参数分开命名，避免和"加工参数"里的同名键串味）。
DIAMETER_KEY = "diameter_mm"
FLUTE_KEY = "flute_length_mm"
LENGTH_KEY = "length_mm"
CORNER_KEY = "corner_radius_mm"
TIP_ANGLE_KEY = "tip_angle_deg"
TAPER_ANGLE_KEY = "taper_angle_deg"
PITCH_KEY = "pitch_mm"
TEETH_KEY = "teeth"
SHANK_KEY = "shank_diameter_mm"

#: 刀具库条目的 id：只允许这些字符，避免路径穿越（与工程 id 同样的规则）。
TOOL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def tool_parameters() -> ParameterSet:
    """实验台的刀具参数声明（区域 + 栅格刀路那条老链路）。"""

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolKind.FLAT.value, group="刀具",
                 choices=TOOL_KINDS, help="球头刀与圆鼻刀留作拓展，启用方式见 docs/extending.md"),
            spec("diameter_mm", "刀具直径 D", K.FLOAT, 6.0, minimum=1.0, maximum=100.0,
                 step=0.5, unit="mm", group="刀具"),
            spec("length_mm", "刀具长度 L", K.FLOAT, 30.0, minimum=2.0, maximum=300.0,
                 step=1.0, unit="mm", group="刀具", help="参与三维显示，也是将来做碰撞检查的输入"),
        )
    )


def tool_type_parameters() -> ParameterSet:
    """刀具库的**全量**参数声明。

    所有类型的参数都在这里，界面上按 ``kind`` 联动显示（``visible_if``）：
    选"钻头"就只出现直径、刃长、总长、顶角，选"丝锥"再多出螺距。
    这样新增一种刀具类型只需要在这里加一行声明 + 一个 ``visible_if``。
    """

    return ParameterSet(
        (
            spec("kind", "刀具类型", K.CHOICE, ToolType.FLAT_END_MILL.value, group="类型",
                 choices=TOOL_TYPES, help="不同类型的参数与适用工序不同"),
            spec(DIAMETER_KEY, "刀具直径 D", K.FLOAT, 6.0, minimum=0.2, maximum=500.0,
                 step=0.5, unit="mm", group="几何", help="铣刀的外径 / 钻头与丝锥的公称直径"),
            spec(CORNER_KEY, "刀尖圆角 R", K.FLOAT, 0.0, minimum=0.0, maximum=250.0,
                 step=0.5, unit="mm", group="几何",
                 visible_if={"kind": ToolType.BULL_NOSE_MILL.value},
                 help="圆鼻刀的圆角半径；0 就是平底刀"),
            spec(TIP_ANGLE_KEY, "顶角", K.FLOAT, 118.0, minimum=10.0, maximum=180.0,
                 step=1.0, unit="°", group="几何",
                 visible_if={"kind": ToolType.DRILL.value},
                 help="钻头两主切削刃的夹角，标准麻花钻 118°"),
            spec(TAPER_ANGLE_KEY, "锥度", K.FLOAT, 0.0, minimum=0.0, maximum=89.0,
                 step=0.5, unit="°", group="几何",
                 visible_if={"kind": ToolType.TAPER_MILL.value},
                 help="锥度铣刀的半锥角：每侧每 1 mm 高度增大的半径"),
            spec(PITCH_KEY, "螺距 P", K.FLOAT, 1.25, minimum=0.05, maximum=20.0,
                 step=0.05, unit="mm", group="几何",
                 visible_if={"kind": ToolType.TAP.value},
                 help="丝锥的螺距，攻丝进给 = 主轴转速 × 螺距"),
            spec(TEETH_KEY, "刃数 Z", K.INT, 2, minimum=1, maximum=24, step=1,
                 unit="齿", group="几何",
                 visible_if={"kind": ToolType.FLAT_END_MILL.value},
                 help="决定每齿进给；塑料与铝常用 2 刃，钢件常用 4 刃"),
            spec(FLUTE_KEY, "刃长", K.FLOAT, 20.0, minimum=0.5, maximum=400.0,
                 step=1.0, unit="mm", group="几何", help="有效切削长度"),
            spec(LENGTH_KEY, "总长 L", K.FLOAT, 40.0, minimum=1.0, maximum=600.0,
                 step=1.0, unit="mm", group="几何", help="参与三维显示，也是碰撞检查的输入"),
            spec(SHANK_KEY, "刀柄直径", K.FLOAT, 6.0, minimum=0.2, maximum=500.0,
                 step=0.5, unit="mm", group="几何", help="夹持部位直径，一般等于刃部直径"),
        )
    )


def tool_type_parameters_for(kind: str) -> ParameterSet:
    """某一种刀具类型**真正用得上**的参数（界面按类型只显示这些）。"""

    normalize_tool_type(kind)
    kept = []
    for item in tool_type_parameters().specs:
        rules = dict(item.visible_if)
        if rules and rules.get("kind", kind) != kind:
            continue
        kept.append(item)
    return ParameterSet(tuple(kept))


def tool_type_defaults(kind: str) -> dict[str, Any]:
    """某种刀具类型的默认参数值（``kind`` 就是它自己）。"""

    tool_type = normalize_tool_type(kind)
    values = tool_type_parameters_for(tool_type).defaults()
    # 声明里的 kind 默认值是全量声明的第一个选项，这里必须按类型覆盖，
    # 否则界面上"新建球头刀"会带着 kind=flat_end_mill 提交。
    values["kind"] = tool_type
    return values


def normalize_tool_type(kind: Any) -> str:
    """校验刀具类型，返回规范值。"""

    value = str(kind or "").strip()
    allowed = {choice.value for choice in TOOL_TYPES}
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ParameterError(f"未知的刀具类型 {kind!r}；可选：{options}")
    return value


def normalize_tool_values(kind: str, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """把一份刀具参数校验成规范值（补默认、限幅、按类型归一）。

    归一的三条规则（都是"让几何自洽"而不是"报错"）：

    * 圆角半径不能超过刀具半径 —— 否则圆鼻刀会算出一个不存在的形状；
    * 刃长不能超过总长 —— 界面上的两个输入框很容易输反；
    * 刀柄直径缺省跟随刃部直径。
    """

    tool_type = normalize_tool_type(kind)
    source = dict(values or {})
    # 只收本类型**认识**的键（按类型自己那份声明校验），避免"切了类型但旧参数还挂着"
    # 的脏数据进库，也不会拿别的类型的取值范围去卡当前类型。
    known = {item.key for item in tool_type_parameters_for(tool_type).specs}
    coerced: dict[str, Any] = dict(tool_type_parameters_for(tool_type).coerce(
        {key: value for key, value in source.items() if key in known}
    ))
    # 用不到的键一律归零：它们可能不满足自己的取值范围（例如顶角最小 10°），
    # 若把 0 写进去，下次读出来再校验就会失败。
    diameter = float(coerced[DIAMETER_KEY])
    radius = diameter / 2.0
    for key, default in (
        (CORNER_KEY, 0.0), (TIP_ANGLE_KEY, 0.0), (TAPER_ANGLE_KEY, 0.0),
        (PITCH_KEY, 0.0), (TEETH_KEY, 0), (SHANK_KEY, diameter),
    ):
        coerced.setdefault(key, default)
    coerced["kind"] = tool_type
    if tool_type == ToolType.BULL_NOSE_MILL.value:
        coerced[CORNER_KEY] = round(min(float(coerced[CORNER_KEY]), radius), 6)
    else:
        coerced[CORNER_KEY] = 0.0
    if tool_type != ToolType.DRILL.value:
        coerced[TIP_ANGLE_KEY] = 0.0
    if tool_type != ToolType.TAPER_MILL.value:
        coerced[TAPER_ANGLE_KEY] = 0.0
    if tool_type != ToolType.TAP.value:
        coerced[PITCH_KEY] = 0.0
    if tool_type != ToolType.FLAT_END_MILL.value:
        coerced[TEETH_KEY] = 0
    flute = float(coerced[FLUTE_KEY])
    length = float(coerced[LENGTH_KEY])
    if flute > length:
        # 输反了：把刃长压到总长，而不是报错打断用户
        coerced[FLUTE_KEY] = length
        flute = length
    if tool_type == ToolType.DRILL.value:
        # 钻头的"刃长"就是有效钻孔深度，不该超过总长的一半多一点；这里只保证不为零。
        coerced[FLUTE_KEY] = max(flute, 0.5)
    return coerced


def tool_geometry_values(record: "ToolRecord") -> dict[str, Any]:
    """刀具条目 → CAM 工序参数里的刀具几何键。

    刀路计算、仿真与 NC 都从工序参数里读刀具，所以"选中刀具"的落点就是这几个键：
    它让刀具库与原有参数链路（``tool_kind`` / ``tool_diameter_mm`` /
    ``tool_length_mm``）对上，不需要给每一条计算路径都加一个"刀具"入参。
    """

    values = record.values
    return {
        "tool_kind": TOOL_KIND_BY_TYPE[record.kind].value,
        "tool_diameter_mm": float(values.get(DIAMETER_KEY, 6.0)),
        "tool_length_mm": float(values.get(LENGTH_KEY, 40.0)),
        "tool_flute_mm": float(values.get(FLUTE_KEY, 0.0)),
    }


def build_tool(record: "ToolRecord") -> "Tool":
    """刀具条目 → 计算用的刀具几何。"""

    values = record.values
    return Tool(
        kind=TOOL_KIND_BY_TYPE[record.kind],
        diameter_mm=float(values.get(DIAMETER_KEY, 6.0)),
        length_mm=float(values.get(LENGTH_KEY, 40.0)),
        flute_length_mm=float(values.get(FLUTE_KEY, 0.0)) or None,
        corner_radius=float(values.get(CORNER_KEY, 0.0)),
        tip_angle_deg=float(values.get(TIP_ANGLE_KEY, 0.0)) or None,
    )


def tool_library_catalog() -> dict[str, Any]:
    """刀具库的**类型目录**：类型列表 + 每种类型自己的参数声明与默认值。

    纯声明（不碰任何用户数据），所以界面即使读不到刀库文件也能知道能新建哪些刀具、
    每种刀要填哪些参数。刀具**列表**由 :class:`~toolpath_lab.storage.tool_library.ToolRepository`
    单独提供。
    """

    specs = tool_type_parameters()
    return {
        "types": [
            {
                "id": choice.value,
                "label": choice.label,
                "parameters": [
                    item.to_dict() for item in specs.specs
                    if not item.visible_if or dict(item.visible_if).get("kind") == choice.value
                ],
                "defaults": tool_type_defaults(choice.value),
            }
            for choice in specs.spec("kind").choices
        ],
        "parameters": specs.to_dicts(),
        "defaults": specs.defaults(),
    }


def new_tool_id(prefix: str = "tool") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class Tool:
    """一把经过校验的刀具**几何**。"""

    kind: ToolKind = ToolKind.FLAT
    diameter_mm: float = 6.0
    length_mm: float = 30.0
    #: 刃长（可选）：三维显示与 NC 注释用，不参与刀路几何。
    flute_length_mm: float | None = None
    #: 刀尖圆角半径。``None`` 表示按类型推导（球头刀 = 半径，其余 = 0）。
    corner_radius: float | None = None
    #: 顶角（钻头）：只进 NC 注释，不参与三轴刀路。
    tip_angle_deg: float | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.diameter_mm) or self.diameter_mm <= 0:
            raise ParameterError("刀具直径必须是有限正数")
        if not isfinite(self.length_mm) or self.length_mm <= 0:
            raise ParameterError("刀具长度必须是有限正数")
        if self.flute_length_mm is not None:
            if not isfinite(self.flute_length_mm) or self.flute_length_mm <= 0:
                raise ParameterError("刃长必须是有限正数")
        if self.corner_radius is not None:
            if not isfinite(self.corner_radius) or self.corner_radius < 0:
                raise ParameterError("刀尖圆角半径不能是负数")
            if self.kind is not ToolKind.BULL and self.corner_radius > 1e-9:
                raise ParameterError("只有圆鼻刀可以设置刀尖圆角半径")

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Tool":
        """由界面/接口的参数字典构造刀具（实验台那条链路）。"""

        return cls(
            kind=ToolKind(str(params["kind"])),
            diameter_mm=float(params["diameter_mm"]),
            length_mm=float(params["length_mm"]),
        )

    @property
    def radius_mm(self) -> float:
        return self.diameter_mm / 2.0

    @property
    def corner_radius_mm(self) -> float:
        """刀尖圆角半径（平底刀为 0，球头刀等于半径，圆鼻刀取设定值）。"""

        if self.kind is ToolKind.BALL:
            return self.radius_mm
        if self.kind is ToolKind.BULL:
            if self.corner_radius is None:
                return 0.0
            return min(float(self.corner_radius), self.radius_mm)
        return 0.0

    @property
    def footprint_radius_mm(self) -> float:
        """刀具在加工面上的足迹半径，即刀路相对区域轮廓的偏置量。"""

        if self.kind is ToolKind.BALL:
            return 0.0
        if self.kind is ToolKind.BULL:
            return max(0.0, self.radius_mm - self.corner_radius_mm)
        return self.radius_mm

    def describe(self) -> dict[str, Any]:
        """界面与接口使用的摘要。"""

        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "kind_label": TOOL_KIND_LABELS[self.kind.value],
            "diameter_mm": self.diameter_mm,
            "radius_mm": self.radius_mm,
            "length_mm": self.length_mm,
            "footprint_radius_mm": self.footprint_radius_mm,
            "corner_radius_mm": self.corner_radius_mm,
        }
        if self.flute_length_mm is not None:
            payload["flute_mm"] = self.flute_length_mm
        if self.tip_angle_deg:
            payload["tip_angle_deg"] = self.tip_angle_deg
        return payload


@dataclass(frozen=True, slots=True)
class ToolRecord:
    """刀具库里的一条刀具记录（可持久化的实体）。

    记录**只存参数值**，几何由 :func:`build_tool` 现算：这样以后改几何规则
    （例如换一种圆鼻刀的足迹算法）时，库里已有的刀具自动跟着变，
    不会留下"存的是旧算法的结果"的历史包袱。
    """

    tool_id: str
    name: str
    kind: str
    values: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not TOOL_ID_PATTERN.match(str(self.tool_id)):
            raise ParameterError(f"非法的刀具 id：{self.tool_id!r}")
        if not str(self.name).strip():
            raise ParameterError("刀具名称不能为空")

    @classmethod
    def create(cls, name: str, kind: str, values: Mapping[str, Any] | None = None,
               *, tool_id: str = "", note: str = "") -> "ToolRecord":
        """构造一条记录：校验类型与参数、补齐全套默认值。"""

        tool_type = normalize_tool_type(kind)
        normalized = normalize_tool_values(tool_type, values)
        return cls(
            tool_id=tool_id or new_tool_id(),
            name=str(name).strip(),
            kind=tool_type,
            values=normalized,
            note=str(note or ""),
        )

    @property
    def label(self) -> str:
        """列表里显示的摘要：名称 + 类型 + 主尺寸。"""

        diameter = float(self.values.get(DIAMETER_KEY, 0.0))
        if self.kind == ToolType.TAP.value:
            extra = f"P{float(self.values.get(PITCH_KEY, 0.0)):g}"
        elif self.kind == ToolType.DRILL.value:
            extra = f"{float(self.values.get(TIP_ANGLE_KEY, 0.0)):g}°"
        elif self.kind == ToolType.BULL_NOSE_MILL.value:
            extra = f"R{float(self.values.get(CORNER_KEY, 0.0)):g}"
        else:
            extra = ""
        head = f"{self.name} · {TOOL_TYPE_LABELS[self.kind]} · D{diameter:g}"
        return f"{head} {extra}".strip()

    @property
    def tool_kind(self) -> ToolKind:
        return TOOL_KIND_BY_TYPE[self.kind]

    def to_payload(self) -> dict[str, Any]:
        """接口与前端用的完整描述。"""

        return {
            "id": self.tool_id,
            "name": self.name,
            "kind": self.kind,
            "kind_label": TOOL_TYPE_LABELS[self.kind],
            "values": dict(self.values),
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "label": self.label,
            "tool": build_tool(self).describe(),
        }

    def with_updates(self, **changes: Any) -> "ToolRecord":
        """返回一份改了名字/类型/参数/备注的新记录（id 与时间戳由仓库维护）。"""

        name = str(changes.get("name", self.name)).strip()
        kind = normalize_tool_type(changes.get("kind", self.kind))
        raw_values = changes.get("values")
        values = normalize_tool_values(
            kind, {**self.values, **dict(raw_values)} if isinstance(raw_values, Mapping)
            else self.values
        )
        note = str(changes.get("note", self.note) or "")
        return ToolRecord(
            tool_id=self.tool_id,
            name=name or self.name,
            kind=kind,
            values=values,
            note=note,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


__all__ = [
    "CORNER_KEY",
    "DIAMETER_KEY",
    "FLUTE_KEY",
    "LENGTH_KEY",
    "PITCH_KEY",
    "SHANK_KEY",
    "TAPER_ANGLE_KEY",
    "TEETH_KEY",
    "TIP_ANGLE_KEY",
    "TOOL_ID_PATTERN",
    "TOOL_KINDS",
    "TOOL_KIND_BY_TYPE",
    "TOOL_KIND_LABELS",
    "TOOL_TYPE_LABELS",
    "TOOL_TYPES",
    "Tool",
    "ToolKind",
    "ToolRecord",
    "ToolType",
    "build_tool",
    "new_tool_id",
    "normalize_tool_type",
    "normalize_tool_values",
    "tool_geometry_values",
    "tool_library_catalog",
    "tool_parameters",
    "tool_type_defaults",
    "tool_type_parameters",
    "tool_type_parameters_for",
]
