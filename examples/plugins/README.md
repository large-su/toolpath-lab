# 示例插件

主程序刻意只保留最基础的能力，剩下的都留给大家练手。这里放的是**完整的参考实现**，
复制进主程序就能用——这正是"基座"的用法。

| 插件 | 内容 | 练手方向 |
| --- | --- | --- |
| `contour_planner.py` | 环切（等距轮廓）策略，自带多边形等距偏置几何 | 凹形状的"环断裂"、螺旋插补、变切宽 |

## 启用方式

```bash
cp examples/plugins/contour_planner.py toolpath_lab/planning/contour.py
```

然后在 `toolpath_lab/planning/__init__.py` 里加一行（导入顺序就是界面上的排序）：

```python
from toolpath_lab.planning import contour as _contour  # noqa: F401
```

重启程序，界面"刀路"分组里就会出现"环切(示例插件)"，参数控件自动生成，
`POST /api/plan` 也立刻接受 `{"planner": {"id": "contour", ...}}`。

## 自己写一个策略需要什么

1. 一个类，继承 `Planner`，用 `@PLANNERS.register` 注册；
2. 声明 `id` / `label` / `description` 与 `parameters`（ParameterSet）；
3. 实现 `plan(context) -> Toolpath`。

`context` 提供：`boundary`（逆时针轮廓）、`tool`、`region`、`parameters`、
`to_positions()`、`cut_move()`、`link_move()`、`rapid_between()`、
`approach_move_down()`、`retract_move_up()`、`warn()`。
详见 [docs/extending.md](../../docs/extending.md)。
