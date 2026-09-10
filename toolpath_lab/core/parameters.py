"""声明式参数。

本工程里每一个能力（区域形状、刀路策略）都发布一个 ParameterSet。这一份声明同时驱动三件事：

1. **界面**：前端按声明渲染控件，所以"新增一个策略"只需要写 Python，不用碰 JavaScript；
2. **校验**：HTTP 适配层用同一份声明做类型与范围检查，手写请求也塞不进非法值；
3. **文档**：GET /api/catalog 把声明导出，docs/parameters.md 可以直接照抄。

因此新增一个参数 = 新增一行 ParameterSpec。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping

from toolpath_lab.core.errors import ParameterError


class ParameterKind(str, Enum):
    """单个参数的控件类型与校验方式。"""

    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    CHOICE = "choice"


@dataclass(frozen=True, slots=True)
class Choice:
    """CHOICE 型参数的一个选项。

    disabled=True 的选项会在界面上显示但不可选，用来表示"这个方向留给拓展"。
    """

    value: str
    label: str
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label, "disabled": self.disabled}


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """一个面向用户的参数声明。"""

    key: str
    label: str
    kind: ParameterKind
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    choices: tuple[Choice, ...] = ()
    group: str = "常规"
    help: str = ""
    visible_if: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.key.isidentifier():
            raise ValueError(f"参数键 {self.key!r} 必须是合法标识符")
        if self.kind is ParameterKind.CHOICE and not self.choices:
            raise ValueError(f"选项型参数 {self.key!r} 至少要有一个选项")

    def to_dict(self) -> dict[str, Any]:
        """给前端使用的 JSON 描述。"""

        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind.value,
            "default": self.default,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "unit": self.unit,
            "choices": [choice.to_dict() for choice in self.choices],
            "group": self.group,
            "help": self.help,
            "visible_if": {key: value for key, value in self.visible_if},
        }

    def coerce(self, raw: Any) -> Any:
        """校验 raw 并返回规范类型的值。"""

        if self.kind is ParameterKind.BOOL:
            return self._coerce_bool(raw)
        if self.kind is ParameterKind.CHOICE:
            return self._coerce_choice(raw)
        if self.kind is ParameterKind.INT:
            value = self._coerce_number(raw)
            if abs(value - round(value)) > 1e-9:
                raise ParameterError(f"参数 {self.key!r} 必须是整数（收到 {raw!r}）")
            result: float | int = int(round(value))
        else:
            result = self._coerce_number(raw)
        self._check_range(float(result), raw)
        return result

    def _coerce_bool(self, raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false", "1", "0"}:
            return raw.strip().lower() in {"true", "1"}
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        raise ParameterError(f"参数 {self.key!r} 必须是布尔值（收到 {raw!r}）")

    def _coerce_choice(self, raw: Any) -> str:
        value = str(raw)
        allowed = {choice.value for choice in self.choices}
        if value not in allowed:
            options = ", ".join(sorted(allowed))
            raise ParameterError(f"参数 {self.key!r} 只能是 [{options}] 之一（收到 {value!r}）")
        return value

    def _coerce_number(self, raw: Any) -> float:
        if isinstance(raw, bool):
            raise ParameterError(f"参数 {self.key!r} 必须是数字（收到 {raw!r}）")
        try:
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise ParameterError(f"参数 {self.key!r} 必须是数字（收到 {raw!r}）") from error
        if value != value or value in (float("inf"), float("-inf")):
            raise ParameterError(f"参数 {self.key!r} 必须是有限值（收到 {raw!r}）")
        return value

    def _check_range(self, value: float, raw: Any) -> None:
        if self.minimum is not None and value < self.minimum - 1e-12:
            unit = f" {self.unit}" if self.unit else ""
            raise ParameterError(
                f"参数 {self.key!r} 不能小于 {self.minimum:g}{unit}（收到 {raw!r}）"
            )
        if self.maximum is not None and value > self.maximum + 1e-12:
            unit = f" {self.unit}" if self.unit else ""
            raise ParameterError(
                f"参数 {self.key!r} 不能大于 {self.maximum:g}{unit}（收到 {raw!r}）"
            )


@dataclass(frozen=True, slots=True)
class ParameterSet:
    """一组按顺序排列、键唯一的 ParameterSpec。"""

    specs: tuple[ParameterSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        keys = [item.key for item in self.specs]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"参数键重复：{duplicates}")

    def __iter__(self) -> Iterator[ParameterSpec]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)

    def __add__(self, other: "ParameterSet") -> "ParameterSet":
        return ParameterSet(self.specs + other.specs)

    def spec(self, key: str) -> ParameterSpec:
        for item in self.specs:
            if item.key == key:
                return item
        raise ParameterError(f"未知参数 {key!r}")

    def defaults(self) -> dict[str, Any]:
        return {item.key: item.default for item in self.specs}

    def coerce(self, raw: Mapping[str, Any] | None) -> dict[str, Any]:
        """返回补全默认值、通过校验的参数字典。

        未知的键会被忽略（向后兼容新客户端），缺失的键取默认值，
        非法值抛 ParameterError 并指出是哪个键。
        """

        source: Mapping[str, Any] = raw or {}
        result: dict[str, Any] = {}
        for item in self.specs:
            if item.key in source and source[item.key] is not None:
                result[item.key] = item.coerce(source[item.key])
            else:
                result[item.key] = item.default
        return result

    def to_dicts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.specs]


def spec(
    key: str,
    label: str,
    kind: ParameterKind,
    default: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
    unit: str = "",
    group: str = "常规",
    help: str = "",
    choices: Iterable[Choice] = (),
    visible_if: Mapping[str, str] | None = None,
) -> ParameterSpec:
    """给各能力模块用的简写构造器。"""

    return ParameterSpec(
        key=key,
        label=label,
        kind=kind,
        default=default,
        minimum=minimum,
        maximum=maximum,
        step=step,
        unit=unit,
        group=group,
        help=help,
        choices=tuple(choices),
        visible_if=tuple((visible_if or {}).items()),
    )
