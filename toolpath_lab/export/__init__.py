"""导出器：把一次规划结果写成文件。

每个格式都是一个**纯函数** `toolpath_to_xxx(toolpath, ...) -> str`，
不碰 I/O、不依赖 HTTP，因此在单元测试里可以直接比对字符串。

- `toolpath_to_gcode`：NC 程序（G21/G90/G17 + G0/G1 带 F），给控制器；
- `toolpath_to_csv`：逐点刀点表，给人看表和接只认点表的下位机；
- `toolpath_to_json`：无损快照，给脚本消费。

新增格式：写一个纯函数，在这里导出，再在 `server/app.py` 里加一个路由分支。
"""

from toolpath_lab.export.csv_points import COLUMNS as CSV_COLUMNS
from toolpath_lab.export.csv_points import toolpath_to_csv
from toolpath_lab.export.gcode import toolpath_to_gcode
from toolpath_lab.export.json_toolpath import toolpath_to_json

#: 界面上「导出」下拉里用的名字 -> (格式 id, 文件后缀)
EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "gcode": ("gcode", ".nc"),
    "csv": ("csv", ".csv"),
    "json": ("json", ".json"),
}

__all__ = [
    "CSV_COLUMNS",
    "EXPORT_FORMATS",
    "toolpath_to_csv",
    "toolpath_to_gcode",
    "toolpath_to_json",
]