# 课程汇报与成果

更新日期：2026-10-08。源码分支为 `U202310570`，由原 `feature/spiral-blender` 分支改名而来。

## 查看成果

| 文件 | 用途 |
| --- | --- |
| [整合版汇报 PPT](数控课程作业汇报_UG与刀路功能开发整合版.pptx) | 当前本地最新 13 页版本，包含 UG 加工过程、平台功能成果和 AI 协作；第 11 页内嵌演示视频 |
| [功能开发报告](刀路规划功能开发报告.docx) | 功能、实现、验证、使用方法与 AI 辅助开发记录；保留本地现有版本 |
| [独立演示视频](demo/animation_blender_eevee.mp4) | 1920×1080、30 fps、约 12.57 秒、10 倍速，PPT 播放备用 |
| [Blender 场景](demo/machining.blend) | 在 Blender 5.1.2 中打开、播放、调整相机或继续渲染 |
| [斜视图](demo/overview.png)、[加工中视图](demo/machining.png)、[俯视图](demo/top.png) | 已有 Cycles 渲染成果 |
| [平台截图](demo/platform_spiral.png) | 螺旋刀路操作界面 |
| [对比 CSV](data/comparison.csv)、[对比 JSON](data/comparison.json) | 标准工况三种走刀方式的统计数据 |
| [NC 示例](data/spiral.nc) | 标准圆形螺旋刀路的 G-code |
| [Blender 验收摘要](data/blender-verification.json) | 七种工况的建场景及重开位置检查记录 |
| [启动修复验收](data/launcher-verification.json) | 双代码页、特殊路径、重复启动及端口占用等检查；移除本机临时目录字段 |
| [视频信息](data/media.json) | 视频帧数、分辨率及时长 |
| [文件清单](manifest.json) | 本次同步的成果文件大小与 SHA-256 |

GitHub 可能无法直接预览 PPT、Word、视频或 Blender 文件；点击文件页的下载按钮，或下载整个分支 ZIP 后在本机打开。

最初按确认方案制作 16 页整合稿，并完整保留 UG 六页文字和图片；本次上传用户本地进一步修改的 13 页版本，不将其还原为早期稿件。两份原始 PPT 仍保存在本地。原整合稿备注按约 7 分钟编写，当前删页后的汇报时长请以实际练习为准。AI 部分记录真实需求、方案确认、开发、检查和反馈修改，代码主要由 Codex 根据用户确认方案编写。

## 当前功能与验证

- 圆形连续螺旋：支持外向内、内向外，调整切宽与进给并播放刀具运动。
- 策略对比：相同工况比较螺旋、往复栅格、单向栅格，导出 CSV。
- Blender 导出：生成独立 ZIP，自动建立刀具、工件、轨迹、相机和灯光。
- Windows 启动修复：统一 ASCII / CRLF BAT，处理解释器查找、重复启动和端口占用。
- 本次同步前 136 项 unittest 通过，无界面规划示例通过。七种 Blender 工况检查为此前实测，本次未重复渲染；最大采样误差约 0.000260285 mm。

标准工况为圆形直径 80 mm、刀径 6 mm、刀长 30 mm、切宽 3 mm、进给 800 mm/min。预计工时分别为螺旋 125.17 s、往复 121.06 s、单向 134.92 s；螺旋在该例中并非最快。Blender 展示刀具移动与轨迹，工件保持固定几何，不是材料切除仿真。

## 运行与复查

在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt
python -m toolpath_lab
```

按终端显示的地址在浏览器打开平台。桌面 Electron 入口与详细安装方式见[项目说明](../README.md)。

```powershell
python -m unittest discover -s tests
python examples/headless_plan.py
```

开发记录见 [AI 辅助开发记录](../docs/ai-development.md)，Blender 导出操作见 [Blender 使用说明](../docs/blender-guide.md)。报告包含早期验收信息，当前测试数与启动修复以本页和最新开发记录为准。

## 后续更新自己的分支

在本地仓库 `toolpath-lab` 中执行（不要在外层交付文件夹执行）：

```powershell
git switch U202310570
git pull --ff-only origin U202310570
git status
# 修改源码或将最新成果复制到 course-deliverables 后，先完成相应检查。
git add <本次要提交的文件或目录>
git diff --cached --stat
git commit -m "Describe the latest changes"
git push origin U202310570
```

若自己修改了成果文件，发布前同步更新 `manifest.json` 中对应条目的字节数和 SHA-256。个人分支推送无需给上游仓库创建 Pull Request；老师要求上游提交时再另行处理。
