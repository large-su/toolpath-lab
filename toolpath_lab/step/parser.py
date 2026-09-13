"""ISO 10303-21（STEP Part 21）实例表解析。

一个 STEP 文件长这样::

    ISO-10303-21;
    HEADER;
    FILE_DESCRIPTION((''),'2;1');
    FILE_NAME('part.step','2024-01-01T00:00:00',(''),(''),'','','');
    FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 }'));
    ENDSEC;
    DATA;
    #1=APPLICATION_CONTEXT('core data for automotive mechanical design processes');
    #10=CARTESIAN_POINT('',(0.,0.,0.));
    #11=DIRECTION('',(0.,0.,1.));
    ENDSEC;
    END-ISO-10303-21;

本模块只做**一件事**：把这堆文本变成 ``#id -> (关键字, 参数)`` 的表。
参数的取值有五种：引用（``#n``）、字符串、枚举（``.T.``）、数字、列表（嵌套）。
"这个实体在几何上是什么意思"是 ``geometry`` 模块的事。

解析器容忍工业文件里常见的脏数据：多余的 ``;``、未知的段、注释块 ``/* ... */``、
以及一行里塞多个实体。单条实例语法错误只计一次警告并跳过，不影响其它实例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from toolpath_lab.step.errors import StepFormatError, StepSizeError

#: 单个文件允许的最大实体数量，防止畸形文件耗尽内存。
MAX_ENTITIES = 2_000_000
#: 实例参数允许的最大嵌套深度。
MAX_DEPTH = 32

#: 引用用这个包装类型表示，解开引用表时才能和字符串区分开。
class Ref(int):
    """指向另一个实例的引用（``#n``）。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"#{int(self)}"


class EnumValue(str):
    """STEP 枚举值，例如 ``.T.`` / ``.CARTESIAN.``（去掉两端的点）。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f".{str(self)}."


class Derived(str):
    """STEP 里的 ``*``：**派生属性**，值由其它属性算出来，文件里不写。

    必须占住参数位置，不能当空白跳过：``ORIENTED_EDGE('NONE', *, *, #310, .F.)``
    里的两个 ``*`` 是派生属性 edge_start / edge_end，真正要用的是第 4 个参数
    edge_element；一旦把 ``*`` 吞掉，后面的下标就整体错位。

    继承 ``str`` 是为了兼容"这里可能是个字符串"的旧判断；:func:`as_ref` 会把它
    当成非引用（它显式排除了 ``str``），因此不会被误认成实例编号。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "*"


@dataclass(frozen=True, slots=True)
class StepEntity:
    """一条 ``#id = KEYWORD(args)`` 实例。"""

    id: int
    keyword: str
    arguments: tuple[Any, ...] = ()
    #: 复杂实例（``#1=(A(...)B(...))``）里各分量的关键字；普通实例为空。
    components: tuple[str, ...] = ()

    @property
    def is_complex(self) -> bool:
        return bool(self.components)

    def arg(self, index: int, default: Any = None) -> Any:
        """按下标取参数，越界返回 default（工业文件里可选参数常被省略）。"""

        if 0 <= index < len(self.arguments):
            return self.arguments[index]
        return default

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "keyword": self.keyword, "arity": len(self.arguments)}


@dataclass(slots=True)
class StepFile:
    """一份解析完成的 STEP 文件。"""

    entities: dict[int, StepEntity] = field(default_factory=dict)
    header: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    schemas: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    #: 出现过的实体关键字 -> 数量，用于向上层报告"这个文件里有什么"。
    keyword_counts: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.entities)

    def get(self, entity_id: Any) -> StepEntity | None:
        try:
            return self.entities[int(entity_id)]
        except (KeyError, TypeError, ValueError):
            return None

    def by_keyword(self, *keywords: str) -> Iterator[StepEntity]:
        """按关键字遍历实例，保持文件中的出现顺序。"""

        wanted = set(keywords)
        for entity in self.entities.values():
            if entity.keyword in wanted:
                yield entity

    def count(self, keyword: str) -> int:
        return self.keyword_counts.get(keyword, 0)

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < 50:
            self.warnings.append(message)

    def describe(self) -> dict[str, Any]:
        """给接口用的摘要：文件里有什么、解析有没有出问题。"""

        return {
            "entity_count": len(self.entities),
            "schemas": list(self.schemas),
            "header": {
                key: [_plain(value) for value in values] for key, values in self.header.items()
            },
            "keywords": dict(
                sorted(self.keyword_counts.items(), key=lambda item: -item[1])[:24]
            ),
            "warnings": list(self.warnings),
        }


def _plain(value: Any) -> Any:
    """把解析时的包装类型还原成普通 JSON 可序列化的值。"""

    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Ref):
        return f"#{int(value)}"
    if isinstance(value, EnumValue):
        return f".{str(value)}."
    return value


class _Scanner:
    """一次性字符扫描器。"""

    __slots__ = ("text", "position", "length")

    def __init__(self, text: str) -> None:
        self.text = text
        self.position = 0
        self.length = len(text)

    def eof(self) -> bool:
        return self.position >= self.length

    def peek(self) -> str:
        return self.text[self.position] if self.position < self.length else ""

    def skip_blanks(self) -> None:
        """跳过空白与 ``/* */`` 注释。"""

        text = self.text
        while self.position < self.length:
            char = text[self.position]
            if char in " \t\r\n\f\v":
                self.position += 1
                continue
            if char == "/" and self.position + 1 < self.length and text[self.position + 1] == "*":
                end = text.find("*/", self.position + 2)
                self.position = self.length if end < 0 else end + 2
                continue
            return


def _scan_string(scanner: _Scanner) -> str:
    """读一个单引号字符串，``''`` 表示一个单引号。"""

    scanner.position += 1  # 开引号
    chunks: list[str] = []
    text = scanner.text
    while scanner.position < scanner.length:
        char = text[scanner.position]
        if char == "'":
            if scanner.position + 1 < scanner.length and text[scanner.position + 1] == "'":
                chunks.append("'")
                scanner.position += 2
                continue
            scanner.position += 1
            return "".join(chunks)
        chunks.append(char)
        scanner.position += 1
    raise StepFormatError("字符串没有闭合的单引号")


def _scan_enum(scanner: _Scanner) -> EnumValue:
    scanner.position += 1  # 开点
    end = scanner.text.find(".", scanner.position)
    if end < 0:
        raise StepFormatError("枚举值没有闭合的点")
    value = scanner.text[scanner.position:end].upper()
    scanner.position = end + 1
    return EnumValue(value)


def _scan_reference(scanner: _Scanner) -> Ref:
    scanner.position += 1  # '#'
    start = scanner.position
    text = scanner.text
    while scanner.position < scanner.length and text[scanner.position].isdigit():
        scanner.position += 1
    if scanner.position == start:
        raise StepFormatError("'#' 后面没有实例编号")
    return Ref(int(text[start:scanner.position]))


def _scan_number(scanner: _Scanner) -> int | float:
    text = scanner.text
    start = scanner.position
    if text[scanner.position] in "+-":
        scanner.position += 1
    while scanner.position < scanner.length and text[scanner.position].isdigit():
        scanner.position += 1
    if scanner.position < scanner.length and text[scanner.position] == ".":
        scanner.position += 1
        while scanner.position < scanner.length and text[scanner.position].isdigit():
            scanner.position += 1
    if scanner.position < scanner.length and text[scanner.position] in "eE":
        scanner.position += 1
        if scanner.position < scanner.length and text[scanner.position] in "+-":
            scanner.position += 1
        while scanner.position < scanner.length and text[scanner.position].isdigit():
            scanner.position += 1
    literal = text[start:scanner.position]
    if "." in literal or "e" in literal or "E" in literal:
        try:
            return float(literal)
        except ValueError as error:  # pragma: no cover - 上面的扫描已保证格式
            raise StepFormatError(f"非法实数 {literal!r}") from error
    try:
        return int(literal)
    except ValueError as error:  # pragma: no cover
        raise StepFormatError(f"非法整数 {literal!r}") from error


def _scan_identifier(scanner: _Scanner) -> str:
    text = scanner.text
    start = scanner.position
    while scanner.position < scanner.length:
        char = text[scanner.position]
        if char.isalnum() or char in "_$":
            scanner.position += 1
        else:
            break
    if scanner.position == start:
        raise StepFormatError(f"位置 {start} 出现无法解析的字符 {text[start:start + 1]!r}")
    return text[start:scanner.position]


def _scan_value(scanner: _Scanner, depth: int = 0) -> Any:
    scanner.skip_blanks()
    if depth > MAX_DEPTH:
        raise StepFormatError(f"参数嵌套超过 {MAX_DEPTH} 层")
    if scanner.eof():
        raise StepFormatError("参数列表意外结束")
    char = scanner.peek()
    if char == "(":
        return _scan_list(scanner, depth + 1)
    if char == "'":
        return _scan_string(scanner)
    if char == "#":
        return _scan_reference(scanner)
    if char == ".":
        return _scan_enum(scanner)
    if char == "*":
        # 派生属性标记。**必须**产出一个占位的值，参数下标才不会错位。
        scanner.position += 1
        return Derived("*")
    if char in "+-" or char.isdigit():
        return _scan_number(scanner)
    return _scan_identifier(scanner)


def _scan_list(scanner: _Scanner, depth: int) -> list[Any]:
    scanner.position += 1  # '('
    items: list[Any] = []
    while True:
        scanner.skip_blanks()
        if scanner.eof():
            raise StepFormatError("列表没有闭合的右括号")
        if scanner.peek() == ")":
            scanner.position += 1
            return items
        if scanner.peek() == ",":
            # 空参数（``(1,,2)``）：工业文件里偶有出现，补一个 None。
            items.append(None)
            scanner.position += 1
            continue
        items.append(_scan_value(scanner, depth))
        scanner.skip_blanks()
        if scanner.peek() == ",":
            scanner.position += 1


def _scan_keyword(scanner: _Scanner) -> str:
    return _scan_identifier(scanner).upper()


def _find_section(text: str, name: str) -> int:
    """找出 ``NAME;`` 段起始标记的结束位置。

    标记必须出现在一行的开头——不能只做子串搜索：AP214 的 schema 名是
    ``AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 }``，里面也含 "DATA"，早先的
    子串实现会从这一段开始解析，于是整个实例表都被跳过。
    """

    target = name.upper() + ";"
    upper = text.upper()
    position = 0
    while True:
        index = upper.find(target, position)
        if index < 0:
            return -1
        line_start = upper.rfind("\n", 0, index) + 1
        if upper[line_start:index].strip() == "":
            return index + len(target)
        position = index + 1


def parse_step(text: str, *, max_entities: int = MAX_ENTITIES) -> StepFile:
    """解析 STEP 文本，返回实体表。只做语法层，不解释几何。"""

    if not isinstance(text, str):
        raise StepFormatError("STEP 内容必须是文本")
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped.upper().startswith("ISO-10303-21"):
        raise StepFormatError("不是 STEP 文件：缺少 ISO-10303-21 起始标记")
    if _find_section(stripped, "DATA") < 0:
        raise StepFormatError("不是 STEP 文件：找不到 DATA 段")

    result = StepFile()
    _parse_header(stripped, result)
    _parse_data(stripped, result, max_entities=max_entities)
    if not result.entities:
        raise StepFormatError("DATA 段里没有任何实体实例")
    return result


def _parse_header(text: str, result: StepFile) -> None:
    start = _find_section(text, "HEADER")
    if start < 0:
        return
    end = _find_section(text, "ENDSEC")
    if end < 0:
        return
    section = text[start:end]
    # HEADER 段里既有 `KEYWORD(...)` 也有纯字符串参数（例如 FILE_NAME 的日期），
    # 因此**逐行**解析：只处理 `关键字(` 开头的行，其余行原样跳过。
    # 早期版本用 _scan_string 之外的扫描器顺序读取，遇到字符串就整段放弃，
    # 结果 FILE_SCHEMA 之后的字段全部丢掉。
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or "(" not in stripped:
            continue
        name, _, rest = stripped.partition("(")
        keyword = name.strip().upper()
        if not keyword or not keyword.replace("_", "").isalnum():
            continue
        try:
            scanner = _Scanner("(" + rest)
            arguments = _scan_list(scanner, 1)
        except StepFormatError:
            continue
        result.header[keyword] = tuple(arguments)
        if keyword == "FILE_SCHEMA" and arguments:
            schemas = arguments[0]
            if isinstance(schemas, list):
                result.schemas = tuple(str(item) for item in schemas)
            elif schemas is not None:
                result.schemas = (str(schemas),)


def _parse_data(text: str, result: StepFile, *, max_entities: int) -> None:
    # 注意：FILE_SCHEMA 里也有 "DATA" 字样（例如 AP214 的 schema 名），
    # 因此必须匹配 `DATA;` 这个完整标记，而不能只找 "DATA"。
    start = _find_section(text, "DATA")
    if start < 0:
        return
    scanner = _Scanner(text[start:])
    entities = result.entities
    counts = result.keyword_counts
    errors = 0

    while True:
        scanner.skip_blanks()
        if scanner.eof():
            break
        if scanner.peek() != "#":
            # DATA 段之间可能有 `DATA(...)` 的其它段头，或者 ENDSEC 之后的内容。
            word = ""
            try:
                word = _scan_identifier(scanner).upper()
            except StepFormatError:
                scanner.position += 1
                continue
            if word == "ENDSEC":
                # 只解析第一段 DATA；后面的段（极少见）也一并读，遇到 END 就停。
                continue
            if word in {"END", "ISO"}:
                break
            if word in {"DATA", "HEADER", "ANCHOR", "REFERENCE", "SIGNATURE"}:
                while not scanner.eof() and scanner.peek() != ";":
                    scanner.position += 1
                continue
            continue
        try:
            entity = _parse_instance(scanner)
        except StepFormatError as error:
            errors += 1
            result.warn(f"跳过一条无法解析的实例：{error}")
            _recover(scanner)
            continue
        if entity is None:
            break
        if entity.id in entities:
            result.warn(f"实例 #{entity.id} 重复定义，后一次覆盖前一次")
        entities[entity.id] = entity
        counts[entity.keyword] = counts.get(entity.keyword, 0) + 1
        if len(entities) > max_entities:
            raise StepSizeError(
                f"实体数量超过上限 {max_entities}，请拆分或简化模型"
            )

    if errors:
        result.warn(f"共有 {errors} 条实例没有解析成功（已跳过）")


def _recover(scanner: _Scanner) -> None:
    """语法错误后跳到下一条实例（下一个 ``#`` 或分号）。"""

    text = scanner.text
    while scanner.position < scanner.length:
        char = text[scanner.position]
        if char == ";":
            scanner.position += 1
            return
        if char == "#":
            return
        scanner.position += 1


def _parse_instance(scanner: _Scanner):  # noqa: ANN202 - 返回 StepEntity | None
    entity_id = int(_scan_reference(scanner))
    scanner.skip_blanks()
    if scanner.peek() != "=":
        raise StepFormatError(f"实例 #{entity_id} 后面缺少 '='")
    scanner.position += 1
    scanner.skip_blanks()

    if scanner.peek() == "(":
        # 复杂实例：#1=(A(...)B(...))
        scanner.position += 1
        components: list[str] = []
        merged: list[Any] = []
        while True:
            scanner.skip_blanks()
            if scanner.eof():
                raise StepFormatError(f"复杂实例 #{entity_id} 没有闭合")
            if scanner.peek() == ")":
                scanner.position += 1
                break
            name = _scan_keyword(scanner)
            scanner.skip_blanks()
            if scanner.peek() != "(":
                raise StepFormatError(f"复杂实例 #{entity_id} 的分量 {name} 缺少参数")
            arguments = _scan_list(scanner, 1)
            components.append(name)
            merged.extend(arguments)
        _expect_semicolon(scanner, entity_id)
        return StepEntity(entity_id, components[0] if components else "", tuple(merged),
                          tuple(components))

    keyword = _scan_keyword(scanner)
    scanner.skip_blanks()
    if scanner.peek() != "(":
        raise StepFormatError(f"实例 #{entity_id}（{keyword}）后面缺少参数列表")
    arguments = _scan_list(scanner, 1)
    _expect_semicolon(scanner, entity_id)
    return StepEntity(entity_id, keyword, tuple(arguments))


def _expect_semicolon(scanner: _Scanner, entity_id: int) -> None:
    scanner.skip_blanks()
    if scanner.peek() != ";":
        raise StepFormatError(f"实例 #{entity_id} 没有以 ';' 结束")
    scanner.position += 1


# -- 参数读取小工具（geometry 层大量使用） --------------------------------

def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    return int(round(as_float(value, float(default))))


def as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def as_ref(value: Any) -> int | None:
    """把参数读成实例引用。

    注意：Python 里 ``True`` 是 ``int`` 的子类，而 :class:`EnumValue` 是 ``str`` 的
    子类，所以两个都必须显式排除——否则 ``ADVANCED_FACE(bounds, surface, .T.)`` 会被
    当成"面几何是实例 #True"。这类错误只会在某些面（而不是全部）上出现，非常难查。
    """

    if isinstance(value, Ref):
        return int(value)
    if isinstance(value, bool) or isinstance(value, (EnumValue, str)):
        return None
    if isinstance(value, int):
        return int(value)
    return None


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, EnumValue):
        upper = str(value).upper()
        if upper in {"T", "TRUE"}:
            return True
        if upper in {"F", "FALSE"}:
            return False
    if isinstance(value, bool):
        return value
    return default


def as_numbers(value: Any) -> list[float]:
    """把 ``(1.,2.,3.)`` 这样的参数读成浮点列表。"""

    return [as_float(item) for item in as_sequence(value)]


def as_matrix(value: Any) -> list[list[float]]:
    """典型的 3x3 矩阵在 STEP 里被拆成三个方向向量。"""

    return [as_numbers(item) for item in as_sequence(value)]


__all__ = [
    "EnumValue",
    "MAX_ENTITIES",
    "Ref",
    "StepEntity",
    "StepFile",
    "as_bool",
    "as_float",
    "as_int",
    "as_matrix",
    "as_numbers",
    "as_ref",
    "as_sequence",
    "parse_step",
]
