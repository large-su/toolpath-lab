"""Teaching geometry presets, copied into the existing editable tool fields.

No feed, material, vendor recommendation or hidden machining configuration is
attached to a preset. Every catalogue response gets fresh, validated values.
"""

from dataclasses import dataclass

from toolpath_lab.core.parameters import Choice, ParameterKind, spec
from toolpath_lab.core.tool import Tool, ToolKind, tool_parameters


@dataclass(frozen=True, slots=True)
class ToolPreset:
    """An immutable name and tool geometry, not a second request schema."""
    id: str
    label: str
    tool: Tool

    def describe(self):
        values = tool_parameters().coerce({
            "kind": self.tool.kind.value, "diameter_mm": self.tool.diameter_mm,
            "length_mm": self.tool.length_mm, "nose_radius_mm": self.tool.nose_radius_mm,
        })
        return {"id": self.id, "label": self.label, "values": values}


TOOL_LIBRARY = (
    ToolPreset("flat_d3", "平底刀 D3 · L25", Tool(ToolKind.FLAT, 3, 25)),
    ToolPreset("flat_d6", "平底刀 D6 · L30", Tool(ToolKind.FLAT, 6, 30)),
    ToolPreset("flat_d10", "平底刀 D10 · L40", Tool(ToolKind.FLAT, 10, 40)),
    ToolPreset("flat_d16", "平底刀 D16 · L60", Tool(ToolKind.FLAT, 16, 60)),
    ToolPreset("ball_d6", "球头刀 D6 · L30", Tool(ToolKind.BALL, 6, 30)),
    ToolPreset("ball_d10", "球头刀 D10 · L40", Tool(ToolKind.BALL, 10, 40)),
    ToolPreset("bull_d10_r2", "圆鼻刀 D10 · R2 · L40", Tool(ToolKind.BULL, 10, 40, 2)),
    ToolPreset("bull_d16_r3", "圆鼻刀 D16 · R3 · L60", Tool(ToolKind.BULL, 16, 60, 3)),
)


def tool_library():
    """Serialize without exposing mutable global preset state."""
    return [preset.describe() for preset in TOOL_LIBRARY]


def tool_preset_selector():
    """Declare the UI shortcut using the same ParameterSpec control contract."""
    return spec("__tool_preset__", "常用刀具库", ParameterKind.CHOICE, "custom", group="刀具",
                choices=(Choice("custom", "自定义（手动参数）"),)
                + tuple(Choice(p.id, p.label) for p in TOOL_LIBRARY),
                help="只填入刀具几何参数，仍可手动修改；不更改进给、曲面或走刀策略。单位 mm。")
