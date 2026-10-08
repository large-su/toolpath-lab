# 五轴自适应等残留高度

## 使用与范围

这是独立的 `five_axis_adaptive` 策略，保留原来的三轴“自适应等残留高度”与固定切宽“五轴曲面刀路”。先选择球头刀，再在策略中选择“五轴自适应等残留高度”。当前组合策略仅支持球头刀；其他刀具会返回可操作的错误提示，不会静默使用不适用的残留模型。

曲面可以选自由曲面或多尺度复合面；区域支持现有参数化区域或导入模型的 XY 投影。导入三角网格仍不参与真实曲面接触计算。

推荐初始参数：

- 球头刀 D6、长度 30 mm，40 或 80 mm 方形区域；自由曲面幅值 4 mm、X/Y 波长 80/60 mm。
- 目标残留高度 0.20 mm，最小步距 0.8 mm、最大步距 6 mm，往复模式，方向 0°，进给 600 mm/min。
- 前倾 12°、侧倾 0°，开启姿态平滑，范围 6 mm、最大偏差 5°、仿真刀轴角速度 30°/s。
- 若毛坯上方仍有大量余料，另外勾选先分层粗加工，每层切深 2 mm、余量 0.5 mm，并开启材料切除和刀身检测。

显示中可同时看到自适应步距颜色与刀轴标记；右上角显示目标残留、实际步距范围、姿态平滑统计和“球头刀·估算”。关闭步距着色只改变显示，关闭姿态平滑只关闭姿态滤波/角速度约束，两者不会关闭自适应刀路布局。可用“跳至精加工”查看粗加工后的组合策略。

最后一刀会对齐区域边界，因此边界补刀与上一刀的间距可能小于“最小步距”；实际步距统计包含这段间距。边界处出现极密或红色刀线，不表示所有内部区域都使用了该小步距。

## 组合实现

1. `AdaptiveScallopPlanner` 按球头刀弓高与高度场曲率估算横向步距，生成名义曲面刀点。平滑强度、前倾/侧倾不会改变这一步的扫描线布局。
2. 共用的 `orient_surface_passes` 在这些采样点上计算法向加前倾/侧倾刀轴，并执行有最大偏差的距离滤波，沿用原五轴的接触补偿契约。球头刀在当前教学契约中保留原名义 XYZ 刀点。
3. 刀间连接始终先抬刀再在安全高度转向，即使关闭平滑或选择平面也不会贴着工件摆动。开启平滑时沿用原五轴的时间角速度上限；原地转向也保留非零时间。
4. `metadata.adaptive` 对每条实际精加工刀线提供步距，用于着色；`metadata.orientation_smoothing` 提供姿态指标；`metadata.five_axis_adaptive.estimate_only` 明确标记估算性质。前置粗加工保留这些元数据，并在转入精加工的连接上沿用角速度上限。

凹形投影中，同一扫描层可能产生多条独立刀线，其步距着色按各自所在层映射，不按刀线序号错配后续层的步距。

## 不能据此保证的内容

“目标残留高度”是球头刀弓高加高度场曲率修正的输入，**不是实际测量的残留高度，也不是严格误差上限**。没有进行倾斜刀具完整扫掠体的残料/过切求解，也未计算球头与曲面真实三维接触点或工业 CL 点补偿。刀轴平滑不等于自动避碰，材料仿真仍为 XY 圆形足迹削料近似。极小目标被最小步距限幅时，可能达不到目标；最小步距大于平坦理论步距时会明确提醒。

此组合不保证高曲率、复杂倾角或短刀具无干涉。保持刀身检测；原有机床轴限位、奇异点、RTCP 与后处理器限制仍适用。导出 NC 包含变化的演示 A/B 姿态，但仿真角速度上限仍为 `simulation-only`，不是机床旋转轴限速程序。

## 接口与验证

`POST /api/plan` 示例：

```json
{
  "tool": { "kind": "ball", "diameter_mm": 6, "length_mm": 30 },
  "region": { "shape": "square", "parameters": { "side_mm": 40 } },
  "surface": { "type": "freeform", "parameters": { "amplitude_mm": 4 } },
  "planner": {
    "id": "five_axis_adaptive",
    "parameters": {
      "target_scallop_mm": 0.2, "min_stepover_mm": 0.8, "max_stepover_mm": 6,
      "lead_deg": 12, "side_tilt_deg": 0, "smooth_orientation": true
    }
  },
  "roughing": { "enabled": true, "depth_mm": 2, "allowance_mm": 0.5 }
}
```

- `python -m unittest tests.test_five_axis_adaptive tests.test_roughing tests.test_orientation_smoothing`：布局保留、刀轴变化、姿态约束、粗加工连续性、凹区域步距映射及受限刀具。
- `node tests/check_roughing_integration.mjs`：本地服务下，使用真实前端高度场与刀身检测遍历组合策略的默认粗加工示例。
- `node tests/browser_five_axis_adaptive.mjs`：已有 Edge/Playwright 环境下，验证合并参数、着色、平滑开关、粗加工跳转和剩余毛坯。可用 `TOOLPATH_TEST_URL` / `TOOLPATH_BROWSER_MODULE` 配置。

原算法与平滑说明见 [自适应等残留高度](adaptive-scallop.md)、[五轴姿态](five-axis.md) 和 [分层粗加工](roughing.md)。
