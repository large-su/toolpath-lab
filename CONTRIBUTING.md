# 参与开发

这个基座刻意保持小：目标是"一屏读完路由、一个文件加一个策略"。所以请优先**加法**，不要重构。

## 环境

```bash
pip install -r requirements.txt   # 后端只需要 numpy
npm install                       # 只为桌面窗口装 Electron

npm start                         # 独立窗口
python -m toolpath_lab            # 只用后端 + 浏览器（调试前端更方便）
```

前端不需要打包器：toolpath_lab/web 下的文件就是浏览器直接执行的文件，改完刷新即可
（静态响应带 Cache-Control: no-store）。three.js 以源码形式放在 web/vendor/，升级时替换
three.module.js、three.core.js、OrbitControls.js、RoomEnvironment.js 四个文件。

## 提交前自检

```bash
python -m unittest discover -s tests        # 必须全绿
python examples/headless_plan.py            # 库路径仍然可用
node --check electron/main.mjs              # 桌面壳语法
node --check toolpath_lab/web/js/main.js    # 前端语法（换成任一模块都可以）
```

改了界面就打开窗口点一遍：参数面板能生成、视图能切、播放能拖、导出能出文件。

## 代码约定

- **依赖方向**：core 不导入其它层；planning / simulation / export 只依赖 core；server 组装全部；
  web 只通过 HTTP 说话。新增第三方依赖前先开 Issue 讨论。
- **参数**：任何面向用户的开关都写成 ParameterSpec，不要另建配置系统——它同时驱动界面与校验。
  暂时不开放的分支用 Choice(..., disabled=True) 标成"待拓展"，而不是删掉。
- **单位与坐标**：毫米 / 秒 / 度（内部弧度）；右手系、Z 轴向上、XY 是加工平面。
- **错误类型**：ParameterError（用户输入）、PlanningError（几何不可行）、ToolpathLabError（其它）。
- **不变式**：区域边界逆时针且不含重复点；Toolpath 至少一段运动；Move 至少两个点。
  这些在 `__post_init__` 里校验，请不要绕过。
- **注释**：解释"为什么"，不要复述代码；文档字符串用英文，用户可见文案用中文。

## 新增能力的步骤

1. 写实现（策略 / 形状 / 导出，见 docs/extending.md）；
2. 注册并在对应的 `__init__.py` 中导入；
3. 补测试（几何用数值断言，策略断言刀轨数 / 方向 / 安全高度，接口参考 tests/test_api.py）；
4. 在 CHANGELOG.md 的"未发布"一节写一行；
5. 若改变了用户可见行为，同步更新 README 与 docs。

## Issue / PR

- 报告问题时请附上：/api/catalog 的版本号、POST /api/plan 的请求 JSON（或截图）、期望与实际结果。
- PR 请保持单一主题，说明动机与验证方式（跑了哪些测试、贴出关键输出）。
