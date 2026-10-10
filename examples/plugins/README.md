# 示例插件

这里放的是可以作为参考实现的刀路策略模板。

**环切（`contour_planner.py`）已经转正为主程序内置功能**：
`toolpath_lab/planning/contour.py`，界面「刀路 → 策略」里选「环切」。
本目录保留的是一个**未注册的模板版本**（`SketchPlanner`），用于演示"从零写一个策略"要处理什么。

| 位置 | 内容 |
| --- | --- |
| `toolpath_lab/planning/contour.py` | 内置环切策略（正式功能） |
| `examples/plugins/contour_planner.py` | 未注册的策略模板 + `self_check()` 自检函数 |

模板刻意没有 `@PLANNERS.register`，所以复制到 `planning/` 也不会和内置版本撞 id。

## 启用一个新策略

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/my_planner.py
```

改掉类名、`id`、`label`，然后在 `toolpath_lab/planning/__init__.py` 里加一行
（导入顺序即界面上排列顺序）：

```python
from toolpath_lab.planning import my_planner as _my_planner  # noqa: F401
```

重新启动后，界面「刀路」分组里会出现该策略，参数控件自动生成，
`POST /api/plan` 也会接受 `{"planner": {"id": "my_planner", ...}}`。

## 不注册也能验证

模板里的 `self_check()` 直接跑一遍刀路，确认代码是好的——注册只影响"界面上能不能看到"：

```bash
python -c "from examples.plugins.contour_planner import self_check; print(self_check())"
```

## 现成的几何

等距偏置相关几何在 `toolpath_lab/planning/geometry2d.py`，内置的 `spiral` 与
`contour` 共用，写新策略时不用再抄一遍：

| 函数 | 用途 |
| --- | --- |
| `offset_polygon(polygon, distance)` | 多边形向内/向外等距偏置，退化为空返回 `None` |
| `collect_rings(boundary, start, stepover)` | 逐圈偏置并返回 `(各圈, 是否自交提前结束)` |
| `resample_ring(polygon, step_mm)` | 按等弧长重采样闭合环 |
| `scanline_intervals(polygon, level)` | 直线与多边形求交，栅格刀路用 |
| `polygon_inradius(polygon)` | 内切半径估计，用来判断中心还有没有材料 |

## 编写一个策略

1. 一个类，继承 `Planner`，用 `@PLANNERS.register` 注册；
2. 声明 `id` / `label` / `description` 与 `parameters`（ParameterSet）；
3. 实现 `plan(context) -> Toolpath`。

`context` 提供 `boundary`、`tool`、`region`、`parameters`、`to_positions()`、`cut_move()`、
`link_move()`、`rapid_between()`、`approach_move_down()`、`retract_move_up()` 与 `warn()`。
详见 [docs/extending.md](../../docs/extending.md)。