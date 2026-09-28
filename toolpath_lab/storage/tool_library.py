"""刀具库持久化：一个 JSON 文件就是一个刀库。

和 :mod:`toolpath_lab.storage.repository`（工程仓库）同样的思路：**纯文件、原子写入**。

* 刀具库是**全局**的（不挂在某个工程下）—— 一把 D10 平底刀在哪个零件上都是同一把，
  每个工程各存一份刀库只会造成"改了一处、另一处还是旧参数"的混乱；
* 一个文件装得下几百把刀（每把不到 1 KB），所以不需要每把刀一个目录；
* 写入同样是"先写临时文件再替换"，进程被强杀也不会留下半截 JSON。

文件内容::

    {
      "format": 1,
      "tools": [ { "id": "...", "name": "...", "kind": "...", "values": {...} } ]
    }

空库（文件还不存在或里没有一把刀）时会播下一组常用刀具作为起点：
新建刀具最怕面对一张空表单，有几把现成的刀可以"复制着改"体验完全不同。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from toolpath_lab.core.errors import ParameterError
from toolpath_lab.core.tool import (
    CORNER_KEY,
    DIAMETER_KEY,
    FLUTE_KEY,
    LENGTH_KEY,
    SHANK_KEY,
    TAPER_ANGLE_KEY,
    TEETH_KEY,
    TIP_ANGLE_KEY,
    TOOL_TYPE_LABELS,
    Tool,
    ToolRecord,
    ToolType,
    build_tool,
    new_tool_id,
    normalize_tool_type,
    tool_library_catalog,
)

logger = logging.getLogger(__name__)

#: 刀库文件格式版本。
FORMAT_VERSION = 1
#: 文件名。
LIBRARY_FILENAME = "tools.json"

#: 空库时的起始刀具（id 固定，方便脚本与测试引用）。
DEFAULT_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "id": "tool-flat-d6", "name": "D6 平底刀", "kind": ToolType.FLAT_END_MILL.value,
        "values": {DIAMETER_KEY: 6.0, FLUTE_KEY: 18.0, LENGTH_KEY: 50.0,
                   SHANK_KEY: 6.0, TEETH_KEY: 2},
        "note": "通用开粗/精铣，铝件常用 2 刃",
    },
    {
        "id": "tool-flat-d10", "name": "D10 平底刀", "kind": ToolType.FLAT_END_MILL.value,
        "values": {DIAMETER_KEY: 10.0, FLUTE_KEY: 25.0, LENGTH_KEY: 60.0,
                   SHANK_KEY: 10.0, TEETH_KEY: 4},
        "note": "钢件开粗主力",
    },
    {
        "id": "tool-ball-d6r3", "name": "D6 球头刀 R3", "kind": ToolType.BALL_END_MILL.value,
        "values": {DIAMETER_KEY: 6.0, FLUTE_KEY: 20.0, LENGTH_KEY: 55.0, SHANK_KEY: 6.0},
        "note": "曲面精加工",
    },
    {
        "id": "tool-bull-d10r1", "name": "D10R1 圆鼻刀", "kind": ToolType.BULL_NOSE_MILL.value,
        "values": {DIAMETER_KEY: 10.0, CORNER_KEY: 1.0, FLUTE_KEY: 25.0,
                   LENGTH_KEY: 60.0, SHANK_KEY: 10.0},
        "note": "陡壁与底面过渡处比平底刀耐用",
    },
    {
        "id": "tool-drill-d8", "name": "D8 麻花钻", "kind": ToolType.DRILL.value,
        "values": {DIAMETER_KEY: 8.0, TIP_ANGLE_KEY: 118.0, FLUTE_KEY: 40.0,
                   LENGTH_KEY: 90.0, SHANK_KEY: 8.0},
        "note": "标准 118° 顶角",
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:  # pragma: no cover - 清理临时文件
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


class ToolRepository:
    """刀具库的增删改查。"""

    def __init__(self, root: str | Path, *, seed_defaults: bool = True) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / LIBRARY_FILENAME
        self.seed_defaults = seed_defaults
        if seed_defaults and not self.path.is_file():
            try:
                self._write([self._seed_record(item) for item in DEFAULT_TOOLS])
            except OSError as error:  # pragma: no cover - 只读目录等
                logger.warning("刀具库初始化失败（只读目录？）：%s", error)

    # -- 读写 --------------------------------------------------------------
    @staticmethod
    def _seed_record(item: Mapping[str, Any]) -> ToolRecord:
        record = ToolRecord.create(
            str(item["name"]), str(item["kind"]), item.get("values"),
            tool_id=str(item["id"]), note=str(item.get("note") or ""),
        )
        return record

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("刀库文件读不出来（按空库处理）：%s", error)
            return []
        items = data.get("tools") if isinstance(data, dict) else None
        return list(items) if isinstance(items, list) else []

    def _write(self, records: Iterable[ToolRecord]) -> None:
        _atomic_write(self.path, json.dumps(
            {"format": FORMAT_VERSION, "tools": [item.to_payload() for item in records]},
            ensure_ascii=False, indent=2,
        ))

    def _load(self) -> list[ToolRecord]:
        records: list[ToolRecord] = []
        for item in self._read():
            record = self._from_payload(item)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _from_payload(item: Mapping[str, Any]) -> ToolRecord | None:
        """一条损坏的记录不该让整个刀库打不开：跳过并记日志。"""

        try:
            return ToolRecord.create(
                str(item.get("name") or "未命名刀具"),
                str(item.get("kind") or ToolType.FLAT_END_MILL.value),
                item.get("values") if isinstance(item.get("values"), Mapping) else {},
                tool_id=str(item.get("id") or new_tool_id()),
                note=str(item.get("note") or ""),
            )
        except (ParameterError, KeyError, TypeError, ValueError) as error:
            logger.warning("刀库里有一条记录读不出来，已跳过：%s", error)
            return None

    # -- 查询 --------------------------------------------------------------
    def list(self) -> list[ToolRecord]:
        """全部刀具（按名称排序，界面上顺序稳定）。"""

        return sorted(self._load(), key=lambda item: (item.name, item.tool_id))

    def get(self, tool_id: str) -> ToolRecord:
        key = str(tool_id or "").strip()
        for record in self._load():
            if record.tool_id == key:
                return record
        raise ParameterError(f"刀具不存在：{tool_id}")

    def find(self, tool_id: str) -> ToolRecord | None:
        try:
            return self.get(tool_id)
        except ParameterError:
            return None

    def exists(self, tool_id: str) -> bool:
        return self.find(tool_id) is not None

    def resolve(self, tool_id: str) -> Tool | None:
        """id → 计算用的刀具几何；没有这把刀时返回 None（调用方自行决定报错还是回退）。"""

        record = self.find(tool_id)
        return None if record is None else build_tool(record)

    def catalog(self) -> dict[str, Any]:
        """刀具类型的参数声明（界面用它生成新建/编辑表单）。"""

        return tool_library_catalog()

    def payload(self) -> dict[str, Any]:
        """界面开关一次刀具库就要用到的全部内容：刀具列表 + 类型目录。"""

        return {"tools": [item.to_payload() for item in self.list()], "catalog": self.catalog()}

    # -- 增删改 ------------------------------------------------------------
    def create(self, name: str, kind: str, values: Mapping[str, Any] | None = None,
               *, note: str = "") -> ToolRecord:
        """新建一把刀具。

        名称可以重复（车间里"D6 平底刀"有两把很常见），id 由后端生成保证唯一；
        但名称不能为空 —— 列表里认不出是谁就没法用了。
        """

        records = self._load()
        record = ToolRecord.create(
            str(name).strip(), kind, values, tool_id=self._unique_id(records), note=note
        )
        stamp = _now()
        record = ToolRecord(
            tool_id=record.tool_id, name=record.name, kind=record.kind,
            values=record.values, note=record.note, created_at=stamp, updated_at=stamp,
        )
        records.append(record)
        self._write(records)
        # 回读一遍再返回：写盘与返回的必须是同一份数据，否则调用方手里的时间戳
        # 与文件里的对不上（列表刷新后看着"没改过"）。
        return self.get(record.tool_id)

    def update(self, tool_id: str, **changes: Any) -> ToolRecord:
        """改一把刀具（名称 / 类型 / 参数 / 备注，给什么改什么）。"""

        records = self._load()
        key = str(tool_id or "").strip()
        for index, record in enumerate(records):
            if record.tool_id != key:
                continue
            updated = record.with_updates(**{
                field: changes[field] for field in ("name", "kind", "values", "note")
                if field in changes
            })
            stamp = _now()
            updated = ToolRecord(
                tool_id=updated.tool_id, name=updated.name, kind=updated.kind,
                values=updated.values, note=updated.note,
                created_at=record.created_at or stamp, updated_at=stamp,
            )
            records[index] = updated
            self._write(records)
            return self.get(updated.tool_id)
        raise ParameterError(f"刀具不存在：{tool_id}")

    def delete(self, tool_id: str) -> bool:
        records = self._load()
        key = str(tool_id or "").strip()
        kept = [item for item in records if item.tool_id != key]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def duplicate(self, tool_id: str, *, name: str = "") -> ToolRecord:
        """复制一把刀具（改参数不动原刀，比从头建快得多）。"""

        source = self.get(tool_id)
        records = self._load()
        clone = ToolRecord.create(
            name or self._unique_name(records, f"{source.name} 副本"),
            source.kind, source.values, tool_id=self._unique_id(records),
            note=source.note,
        )
        stamp = _now()
        clone = ToolRecord(
            tool_id=clone.tool_id, name=clone.name, kind=clone.kind, values=clone.values,
            note=clone.note, created_at=stamp, updated_at=stamp,
        )
        records.append(clone)
        self._write(records)
        return self.get(clone.tool_id)

    def restore_defaults(self) -> list[ToolRecord]:
        """把出厂刀具补回来（已经被改过/删过的同名同 id 的刀不覆盖）。"""

        records = self._load()
        existing = {item.tool_id for item in records}
        added = [self._seed_record(item) for item in DEFAULT_TOOLS if item["id"] not in existing]
        if added:
            self._write([*records, *added])
        return self.list()

    def export_payload(self) -> dict[str, Any]:
        """整库导出（备份 / 跨机器搬刀具）。"""

        return {"format": FORMAT_VERSION, "tools": self._read()}

    def import_payload(self, payload: Mapping[str, Any], *, replace: bool = False) -> int:
        """导入一份刀库；返回导入的刀具数。

        同名同类型同尺寸的刀具视为"已经有了"而跳过，因此反复导入同一个文件不会
        把刀库撑成一堆重复项。
        """

        items = payload.get("tools") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            raise ParameterError("导入的刀库缺少 tools 列表")
        records = [] if replace else self._load()
        signature = {_signature(item) for item in records}
        added: list[ToolRecord] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            record = self._from_payload(item)
            if record is None:
                continue
            key = _signature(record)
            if key in signature:
                continue
            signature.add(key)
            added.append(record)
        if added or replace:
            self._write([*records, *added])
        return len(added)

    # -- 内部 --------------------------------------------------------------
    @staticmethod
    def _unique_id(records: Iterable[ToolRecord]) -> str:
        existing = {item.tool_id for item in records}
        while True:
            candidate = new_tool_id()
            if candidate not in existing:
                return candidate

    @staticmethod
    def _unique_name(records: Iterable[ToolRecord], base: str) -> str:
        existing = {item.name for item in records}
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def backup(self, directory: str | Path) -> Path | None:
        """把刀库文件复制一份到指定目录（返回副本路径）。"""

        if not self.path.is_file():
            return None
        target = Path(directory) / f"tools-{_now()[:19].replace(':', '')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, target)
        return target


def _signature(record: ToolRecord) -> tuple[Any, ...]:
    """判重用的指纹：名称 + 类型 + 全部尺寸参数。"""

    values = record.values
    return (
        record.name.strip().lower(),
        record.kind,
        round(float(values.get(DIAMETER_KEY, 0.0)), 3),
        round(float(values.get(CORNER_KEY, 0.0)), 3),
        round(float(values.get(LENGTH_KEY, 0.0)), 3),
    )


def default_library_dir(data_dir: str | Path) -> Path:
    """刀库放在数据目录下的 ``tools`` 子目录里（与 ``projects`` 平级）。"""

    return Path(data_dir) / "tools"


__all__ = [
    "DEFAULT_TOOLS",
    "FORMAT_VERSION",
    "LIBRARY_FILENAME",
    "TOOL_TYPE_LABELS",
    "ToolRepository",
    "default_library_dir",
    "normalize_tool_type",
]
