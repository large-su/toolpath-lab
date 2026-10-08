# 参与开发

项目保持精简，目标是"一屏读完路由、一个文件加一个策略"，因此改动请优先做加法，避免不必要的重构。

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

一条命令跑完（CI 也是跑这一条，见 `.github/workflows/selfcheck.yml`）：

```bash
python selfcheck.py                # node 不在 PATH 上时跳过前端语法检查
python selfcheck.py --node <路径>  # 用自带的 node 跑前端与桌面壳语法
```

它依次做四件事，全过才返回 0：

1. `python -m unittest discover -s tests` 必须全绿；
2. `python examples/headless_plan.py` 能跑（库路径仍然可用），并写出 NC 文件；
3. `node --check` 检查 `electron/main.mjs` 与 `toolpath_lab/web/js/*.js`（目录里的模块自动覆盖）；
4. **HTTP 冒烟**：临时在随机端口起一个服务，把每个区域形状、每个策略、两种导出与
   400 / 404 / 422 错误路径都走一遍，跑完关掉。

想单独跑某一步，对应的命令是：

```bash
python -m unittest discover -s tests        # 必须全绿
python examples/headless_plan.py            # 库路径仍然可用
node --check electron/main.mjs              # 桌面壳语法
node --check toolpath_lab/web/js/main.js    # 前端语法（换成任一模块都可以）
```

改了界面就打开窗口点一遍：参数面板能生成、视图能切、播放能拖、导出能出文件。

### 没有浏览器时怎么验前端

`node --check` 只保证语法能过，而参数面板、播放联动、工件挤出这些**逻辑**其实都能在 Node 里跑
（本项目的几轮改动就是这么验的）。做法是写一个**临时脚本**，跑完删掉：

1. **把要测的模块拷成 `.mjs` 再 import**：仓库里没有 `package.json`，Node 会把 `.js` 当 CommonJS。
   `panel.js` / `api.js` / `playback.js` 都不 import 别的模块，直接拷到 `_tmp_ui/` 下改名即可；
   `viewport.js` 会 import `three` 与 `../vendor/*`，所以还要**造一个极小的 three 垫片**：
   `node_modules/three/{package.json,index.js}` 里只实现用到的那几个类（`Color`、`BufferGeometry`、
   `Float32BufferAttribute`、`Mesh`、`Group`、`Shape`/`Path`/`ExtrudeGeometry`、`Vector2/3`、
   `Box3`、`DoubleSide`…），并把 `_tmp_ui/vendor/*.js` 换成空的同名导出，免得把整个 three 拖进来。
2. **给它一个够用的假 DOM**：`document.createElement` 返回一个带 `children`/`appendChild`/`append`/
   `replaceChildren`/`addEventListener`/`classList`/`dataset`/`style` 的普通对象就够了；面板构造时
   会 `render()` 一次，所以 root 也要是这种元素。文件读取用 `{ arrayBuffer: async () => bytes }`
   顶替 `File`（`readDrawingText` 只用到它）。
3. **断言真实契约，而不是复述实现**：驱动真实方法（`panel.payload()`、`viewport._workpiece.call(...)`、
   `progressiveHeightMap(...)`），断言"发出去的请求长什么样""几何里有没有那个孔""同一个移动内不
   重建几何"这类**行为**，并尽量用**后端真实产出的 payload**（`catalog_payload()` / `describe()` 写
   成 JSON 再读进来），而不是手搓一份假的。
4. **只测逻辑，不测观感**：颜色、阴影、相机取景这些仍然要人眼看；脚本能证明的是"数据到几何这一步
   是对的"。跑完把临时目录（`_tmp_ui/`、`node_modules/`）一起删掉，别提交进仓库。

## 代码约定

- **依赖方向**：core 不导入其它层；planning / simulation / export 只依赖 core；server 组装全部；
  web 只通过 HTTP 说话。新增第三方依赖前先开 Issue 讨论。
- **参数**：任何面向用户的开关都写成 ParameterSpec，不要另建配置系统——它同时驱动界面与校验。
  暂时不开放的分支用 Choice(..., disabled=True) 标成"待拓展"，而不是删掉；标了 disabled 的取值
  参数层会直接拒绝（界面不可选、接口返回 400），所以"待拓展"不会变成"界面上藏起来但接口能用"。
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
