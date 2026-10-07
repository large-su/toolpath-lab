# 示例插件

这里放的是可以作为参考实现直接使用的刀路策略。主程序内置栅格刀路（往复与单向）与
环切刀路，本目录保留一份与内置环切同源的**模板**，用来演示"一个策略需要写什么"。

| 插件 | 内容 |
| --- | --- |
| `contour_planner.py` | 环切（等距轮廓）策略，自带多边形等距偏置几何；与内置 `toolpath_lab/planning/contour.py` 同源，id 为 `contour_demo` |

## 怎么用这个模板

环切**已经内置**（`toolpath_lab/planning/contour.py`，id `contour`），所以不需要再复制它才能用。
模板的 id 特意写成 `contour_demo`，直接启用不会和内置策略撞车。两种用法：

**照着写自己的策略**（推荐）

1. 复制成 `toolpath_lab/planning/my_strategy.py`；
2. 改掉 `id` / `label` / `description`——注册表遇到重复 id 会抛 `RegistryError`；
3. 在 `toolpath_lab/planning/__init__.py` 里加一行（导入顺序即界面上的排列顺序）：

```python
from toolpath_lab.planning import my_strategy as _my_strategy  # noqa: F401
```

**想直接在界面上看到这个示例**

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/contour_demo.py
```

```python
# toolpath_lab/planning/__init__.py
from toolpath_lab.planning import contour_demo as _contour_demo  # noqa: F401
```

两种用法的结果都一样：重启后界面"刀路"分组里会出现该策略，参数控件自动生成，
`POST /api/plan` 也会接受 `{"planner": {"id": "...", ...}}`。

## 编写一个策略

1. 一个类，继承 `Planner`，用 `@PLANNERS.register` 注册；
2. 声明 `id` / `label` / `description` 与 `parameters`（ParameterSet）；
3. 实现 `plan(context) -> Toolpath`。

`context` 提供 `boundary`、`tool`、`region`、`parameters`、`to_positions()`、`cut_move()`、
`link_move()`、`rapid_between()`、`approach_move_down()`、`retract_move_up()` 与 `warn()`。
详见 [docs/extending.md](../../docs/extending.md)。
