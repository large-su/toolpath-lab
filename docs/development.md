# 功能开发说明 —— 材料切除仿真 · 螺旋刀路 · 刀路评价

> 对应课程作业第 2 项「刀路规划功能开发训练」（可选加分项）
> 开发分支：`feat/removal-sim-and-spiral`　开发对象：ToolpathLab（https://github.com/large-su/toolpath-lab）
> 作者：陈栎霏　U202411196

---

## 1. 做了什么（对照作业给出的可选方向）

作业给出的七个方向里，本次选择其中三项作为主线，并自选一项作为"创造性延伸"：

| 作业方向 | 本次实现 | 落点 |
| --- | --- | --- |
| ② 完善刀具建模形态 | 启用球头刀 / 圆鼻刀，新增圆角半径参数，新增"轴向切深下的咬入半径"与刀尖轮廓 | `core/tool.py` |
| ③ 完善可选曲面类型 | 新增区域形状「圆角矩形」（半径取到短边一半即为跑道形） | `core/region.py` |
| ① 新增刀路规划模式 | 新增「螺旋刀路」策略：圆形区域为解析阿基米德螺线（单段切削、零抬刀），其他形状为等距环 + 切向连接 | `planning/spiral.py` |
| ⑤ 新增材料切除仿真 | Z-map 高度场仿真：按刀尖形状与切深逐段切料，输出覆盖率、残余面积/体积、残余高度、区域外切出面积 | `simulation/removal.py` |
| ★ 自选延伸 | **刀路质量评价与多策略对比**：把"覆盖率（仿真）+ 相对效率 + 切削行程占比"合成可复现的评分表，并给出切宽建议值 | `evaluation/` |

一句话串起来：**先把刀具和区域建模补全，再用新的螺旋策略多一种"走法"，然后让材料切除仿真给出"加工结果"，
最后用评价把"结果 + 效率"变成可比较、可排序的数字——于是"选哪条刀路"从经验判断变成可验证的结论。**

---

## 2. 为什么这么做（问题意识）

原基座能给出的结论只有"刀路有多长、要多少时间"。但加工中真正决定成败的是**有没有切净**：

- 切宽开大 → 走刀少了，但相邻两条刀线之间留下残余脊线；
- 换球头刀铣平面 → 刀尖咬入半径远小于刀具半径，看上去"走了满刀"，实际漏切一大片；
- 抬刀多的策略（单向）→ 工时与接刀痕都上去了，而长度统计里看不出来。

这些问题在原基座里**没有任何数据能反映**，只能靠人在机床上试。本次开发把"结果"量化出来，
并用同一个仿真结果去比较不同策略——这正是 UG 里"余量 / 切削负载"经验的可编程版本。

---

## 3. 新增与修改的文件

```
core/tool.py            修改：三种刀具全部启用；corner_radius_mm 参数（按类型归一化）；
                              cutting_footprint_radius_mm(ap)（咬入半径）；profile_mm()（刀尖轮廓）
core/region.py          修改：新增 RoundedRectangleRegion（rounded_rect）
planning/geometry2d.py  修改：把偏置几何提升为公共工具（inward_normals / offset_polygon /
                              resample_ring / points_inside_polygon），供螺旋策略与仿真共用
planning/spiral.py      新增：螺旋刀路策略（解析螺线 + 等距环两分支，含"外圈清边"）
planning/__init__.py    修改：注册 spiral
simulation/removal.py   新增：Z-map 材料切除仿真（RemovalReport）
simulation/__init__.py  修改：导出 simulate_removal
evaluation/__init__.py  新增：评价包入口
evaluation/compare.py   新增：evaluate_strategies / score_toolpath / format_table
evaluation/defaults.py  新增：默认候选名单 + suggest_stepover（切宽建议）
server/schema.py        修改：新增 EvaluateRequest（请求校验）
server/app.py           修改：新增 POST /api/evaluate
examples/evaluate_strategies.py        新增：命令行对比示例
examples/render_development_figures.py 新增：生成汇报演示图（5 张）
figures/*.png           新增：演示图（刀具形态 / 路网对比 / 切除仿真 / 评分 / 覆盖率-切宽）
tests/test_development_features.py     新增：30 项测试（刀具、区域、螺旋、仿真、评价、接口）
tests/test_tool.py / test_api.py / test_planners.py / test_region.py
                        修改：跟随新能力更新"范围断言"（见第 7 节）
```

---

## 4. 关键实现

### 4.1 刀具建模：从"足迹半径"到"咬入半径"

原代码只有 `footprint_radius_mm`（用于刀路相对轮廓的偏置）：平底刀 = R、球头刀 = 0、圆鼻刀 = R − Rc。
这只回答"刀路能走到哪"，不回答"刀切掉多少"。新增：

$$r_{\text{cut}}(a_p)=\begin{cases}
R & \text{平底刀}\\[2pt]
\sqrt{2Ra_p-a_p^{2}} & \text{球头刀}\ (a_p<R)\\[2pt]
(R-R_c)+\sqrt{2R_c a_p-a_p^{2}} & \text{圆鼻刀}\ (a_p<R_c)
\end{cases}$$

- 球头刀：只有低于已加工面的球带吃刀，`ap → 0` 时咬入半径也趋于 0（这就是"球头刀铣平面会漏切"的定量解释）；
- 圆鼻刀：`ap < Rc` 时底面尚未接触，只有圆角部分切削，因此咬入半径 = 底面半径 + 圆角弦长。

配套：`corner_radius_mm` 成为参数并按类型归一化（平底 0、球头 = R、圆鼻取输入且必须 < R），
`profile_mm()` 输出刀尖轮廓折线供三维显示与文档绘图。

### 4.2 螺旋刀路：两种分支 + 一个不显眼但要命的细节

- **圆形区域**：解析阿基米德螺线 `r(θ) = pitch·θ/2π`，`pitch = r_max / ceil(r_max / ae)`，
  因此**实际切宽不会超过设定值**（等切削负载）；全程只有 3 段运动：下刀 + 1 段切削 + 抬刀。
- **其他形状**：等距偏置环 + 圈间切向连接（不抬刀）；圆角矩形与方形都走这一分支。

> **开发中发现的问题**：单条螺线的最后一圈半径是**渐变**的——只有在一个方位角上真正到达 `r_max`，
> 于是靠外的环带会漏切。材料切除仿真把它直接照了出来：圆形 Ø60、步距 7.5 mm 时覆盖率只有 **81.3%**，
> 未切区域恰好是 25–30 mm 的环带。
> **修正**：螺线到顶后补一整圈（半径恒为 `r_max`）的"外圈清边"，覆盖率升到 **100%**，
> 总行程只多一圈——这也正是实际 CAM 里"螺旋 + 轮廓清边"的做法。参数 `finish_pass` 可关闭以复现该现象。

### 4.3 材料切除仿真（Z-map）

1. 以区域包围盒外扩一个刀具半径建立网格，单元记录"目标面之上剩余高度"，初值 = 轴向切深 `ap`；
2. 把每条切削运动按最大弦长离散，逐点用**咬入半径**做圆盘扫掠，命中单元高度归零；
3. 统计：覆盖率、残余面积、残余体积、最大/平均残余高度、材料去除率、**区域外切出面积**（过切）与网格规模。

模型假设写在模块文档里（平面加工、不建模让刀与刀具跳动），分辨率默认 0.5 mm，一次仿真百毫秒级。

### 4.4 刀路质量评价

$$\text{总分}=0.5\,\text{Quality}+0.3\,\text{Efficiency}+0.2\,\text{Air}$$

- **Quality** = 覆盖率（材料切除仿真给出）× 100 —— "加工到位没有"；
- **Efficiency** = 最快策略工时 / 本策略工时 × 100 —— 用相对值避免量纲不可比；
- **Air** = 主切削长度 / 总长度 × 100 —— 惩罚抬刀与连接行程。

权重与被仿真参数一并写进结果（`weighting` / `simulation`），任何一次比较都可复现；
`/api/evaluate` 与 `examples/evaluate_strategies.py` 共用同一份实现。

---

## 5. 使用方式

```bash
# 库调用
python -c "from toolpath_lab.evaluation import *; ..."   # 见 examples/evaluate_strategies.py

# 命令行对比（默认：平底刀 D10、方形 60、ap1，切宽自动建议）
python examples/evaluate_strategies.py
python examples/evaluate_strategies.py --region circle --diameter-region 60
python examples/evaluate_strategies.py --tool ball --depth 0.2      # 球头刀浅切

# 生成汇报演示图
python examples/render_development_figures.py

# HTTP 接口
curl -X POST http://127.0.0.1:8770/api/evaluate -H "Content-Type: application/json" -d '{
  "tool": {"kind": "flat", "diameter_mm": 10},
  "region": {"shape": "circle", "parameters": {"diameter_mm": 60}},
  "simulation": {"resolution_mm": 1.0, "axial_depth_mm": 1.0}
}'
```

界面侧无需改动：区域形状与策略都是注册表驱动，`/api/catalog` 会自动带上「圆角矩形」与「螺旋刀路」，
参数控件由声明生成；`corner_radius_mm` 只在选择圆鼻刀时出现（`visible_if`）。

---

## 6. 结果与验证

以 Ø60 圆形区域、平底刀 D10、`ap = 1 mm`、切宽 7.5 mm 为例（`examples/evaluate_strategies.py` 输出）：

| 策略 | 覆盖率 | 工时 /s | 抬刀次数 | 空程 /mm | 质量 | 效率 | 行程 | 总分 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 栅格（往复） | 98.9% | 27.8 | 2 | 69 | 98.9 | 99.7 | 81.8 | **95.7** |
| 螺旋 | 100.0% | 35.6 | 2 | 10 | 100.0 | 77.7 | 97.9 | 92.9 |
| 栅格（单向） | 97.9% | 27.7 | 9 | 371 | 97.9 | 100.0 | 45.5 | 88.0 |

结论与工程经验一致：**单向切削工时最短但空程与抬刀最多；螺旋覆盖率最高、行程最干净但路径最长；
往复是折中项**。换球头刀、`ap = 0.2 mm` 时，建议切宽从 7.5 mm 降到 2.1 mm，
否则覆盖率会掉到 50% 上下（见 `figures/fig5_coverage_vs_stepover.png`）。

测试：`python -m unittest discover -s tests` → **142 项全部通过**（原有 112 项 + 本次新增 30 项）。

---

## 7. 关于既有测试的修改（如实说明）

本次扩展改变了基座的能力范围，因此有 5 处"范围断言"必须跟着改，逻辑未削弱：

| 原测试 | 原断言 | 现在 |
| --- | --- | --- |
| `test_catalog_exposes_the_simplified_scope` | 策略只有 raster、区域只有方/圆 | 策略 raster+spiral，区域含 rounded_rect，刀具参数含 corner_radius_mm |
| `test_disabled_tool_kinds_are_published` | 球头/圆鼻刀为 disabled | 三种刀具全部可选 |
| `test_only_the_raster_strategy_is_registered` | 注册表 == ["raster"] | == ["raster", "spiral"] |
| `test_only_square_and_circle_are_registered` | == ["circle", "square"] | == ["circle", "rounded_rect", "square"] |
| `test_unknown_planner_is_a_bad_request` | 用 `"spiral"` 当"未注册策略" | 改用 `"trochoidal"`（spiral 已注册） |

开发过程中还修过两处**测试预期本身写错**的地方（不是代码 bug）：
球头刀"覆盖必然差"的断言在 `ap = 1 mm` 时不成立（咬入半径 3 mm 恰好覆盖 6 mm 切宽），改到 `ap = 0.2 mm`；
`overcut > 0` 的断言对平底刀不成立，因为栅格刀路本就按足迹半径内缩——于是把它拆成一对更有意义的断言：
平底刀**不越界**（overcut == 0）与球头刀**会越界**（足迹半径 0 而咬入半径 > 0）。

---

## 8. 局限与后续

- **只做平面**：加工面为 Z = 0，尚无曲面/3D 区域（对应作业方向 ③ 的"曲面"仍可继续做）；
- **不建模进给方向偏摆与刀具跳动**：切除包络取水平截面圆，五轴姿态（方向 ④）与力学留给后续；
- **环切分支在窄颈处会提前结束**（偏置环断开），与 `examples/plugins/contour_planner.py` 的限制一致；
- **前端未接**：三维视图尚未绘制螺旋的渐变色与仿真高度场（接口已就绪，属于纯前端工作量）；
- **评价权重是工程经验值**：0.5/0.3/0.2 可按工况传参，也欢迎用实际加工数据回归权重。

## 9. 开发方式（对应作业"构建自己的 AI 编程工具"）

本次开发由 **LLM + Agent 工具链**完成：需求拆解与方案设计、代码生成与重构、
单元测试编写与回归、演示图渲染与结果核对，均通过对话式 Agent（ZCode）驱动，
并保留人工评审环节——每一步的结论都要求**可执行的证据**（测试通过、仿真数值、渲染图）。
遇到的两次真实"翻车"（螺旋外圈漏切、球头刀断言不成立）都记录在第 4.2 与第 7 节，
正是这种"让证据说话"的流程把它们暴露出来的。
