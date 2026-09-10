# 示例插件

这里放的是可以作为参考实现直接使用的刀路策略。主程序内置栅格刀路（往复与单向），
本目录提供其它策略的完整实现，复制进 `toolpath_lab/planning/` 即可启用。

| 插件 | 内容 |
| --- | --- |
| `contour_planner.py` | 环切（等距轮廓）策略，自带多边形等距偏置几何 |

## 启用方式

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/contour.py
```

然后在 `toolpath_lab/planning/__init__.py` 里加一行（导入顺序即界面上的排列顺序）：

```python
from toolpath_lab.planning import contour as _contour  # noqa: F401
```

重新启动后，界面"刀路"分组里会出现该策略，参数控件自动生成，
`POST /api/plan` 也会接受 `{"planner": {"id": "contour", ...}}`。

## 编写一个策略

1. 一个类，继承 `Planner`，用 `@PLANNERS.register` 注册；
2. 声明 `id` / `label` / `description` 与 `parameters`（ParameterSet）；
3. 实现 `plan(context) -> Toolpath`。

`context` 提供 `boundary`、`tool`、`region`、`parameters`、`to_positions()`、`cut_move()`、
`link_move()`、`rapid_between()`、`approach_move_down()`、`retract_move_up()` 与 `warn()`。
详见 [docs/extending.md](../../docs/extending.md)。
