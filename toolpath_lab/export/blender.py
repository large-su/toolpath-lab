"""Self-contained Blender exchange bundle; Blender is not a server dependency."""
from __future__ import annotations
import io
import json
from pathlib import Path
import zipfile
from toolpath_lab.core.parameters import Choice, ParameterKind as K, ParameterSet, spec
from toolpath_lab.core.path import Toolpath
from toolpath_lab.core.region import RegionShape
from toolpath_lab.core.tool import Tool, ToolKind
from toolpath_lab.core.errors import PlanningError
from toolpath_lab.export.windows import python_launcher_bat

BLENDER_OPTIONS = ParameterSet((
    spec("fps", "帧率", K.INT, 30, minimum=24, maximum=120, group="Blender", unit="fps"),
    spec("playback_speed", "播放倍率", K.FLOAT, 1.0, minimum=0.1, maximum=100,
         step=1, group="Blender", unit="×"),
    spec("camera", "相机", K.CHOICE, "oblique", group="Blender",
         choices=(Choice("oblique", "斜视"), Choice("top", "俯视"))),
    spec("engine", "渲染器", K.CHOICE, "BLENDER_EEVEE", group="Blender",
         choices=(Choice("BLENDER_EEVEE", "EEVEE 快速预览"), Choice("CYCLES", "Cycles 最终效果"))),
    spec("samples", "渲染采样", K.INT, 64, minimum=8, maximum=512, group="Blender"),
    spec("show_path", "显示刀路", K.BOOL, True, group="Blender"),
    spec("show_rapid", "显示快移", K.BOOL, True, group="Blender"),
))


def blender_bundle(tool: Tool, region: RegionShape, toolpath: Toolpath,
                   timeline: dict, options: dict | None = None) -> bytes:
    if tool.kind is not ToolKind.FLAT:
        raise PlanningError("Blender 导出目前仅支持平底刀")
    settings = BLENDER_OPTIONS.coerce(options)
    bounds = region.describe()["bounds_mm"]
    span = max(pair[1] - pair[0] for pair in bounds)
    manifest = {"schema": "toolpath-lab.blender", "schema_version": 1,
                "units": "mm", "axis": "Z_UP", "position_reference": "tool_tip",
                "tool": tool.describe(), "region": {**region.describe(),
                 "boundary": region.boundary().tolist(),
                 "thickness_mm": min(max(span * 0.09, 4), 24)},
                "toolpath": {"planner": toolpath.planner, "notes": toolpath.notes,
                 "statistics": toolpath.statistics(), "moves": [
                    {"kind": move.kind.value, "feed_mm_per_min": move.feed_mm_per_min,
                     "points": move.points.tolist()} for move in toolpath.moves]},
                "timeline": timeline, "settings": {**settings, "width": 1920, "height": 1080},
                "simulation": "kinematic_static_workpiece"}
    stream = io.BytesIO()
    templates = Path(__file__).with_name("blender_templates")
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("scene.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name in ("build_scene.py", "launch_blender.py", "README.txt"):
            archive.writestr(name, (templates / name).read_bytes())
        for filename, args in (("01_create_scene.bat", ()),
                               ("02_render_preview.bat", ("--render", "preview")),
                               ("03_render_cycles.bat", ("--render", "cycles"))):
            archive.writestr(filename, python_launcher_bat("launch_blender.py", args))
    return stream.getvalue()
