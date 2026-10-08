# AI 辅助开发记录

课程范围为第二页“刀路规划功能开发训练”。使用个人 Fork `Hanakon-kun/toolpath-lab`，基线 main 为 `00e6d908a6544cd8a54af95980f4e1929d7a6626`，原功能分支 `feature/spiral-blender` 于 2026-10-08 按用户要求改名为学号 `U202310570`。现有 UG 刀路作业未修改。

## 工具与任务

Harness 为本机 Codex 桌面应用，实际模型 `gpt-6.1-sol`，来自本任务会话的 `turn_context.model` 字段。代码主要由 Codex 根据已批准方案编写和执行测试。项目级 `AGENTS.md` 固定分层、坐标单位、测试命令及静态工件范围。没有搭建新的 Agent，没有虚构人工编写或人工验收经历。

用户提出 Blender 展示需求，明确仅处理第二页作业，提供个人 Fork，确认不扩展材料切除、栅格方向优化或论文复现，最终授权完整方案。开发初期未提供姓名、学号；后续整合汇报时从用户提供的 UG PPT 中保留其学生信息。

任务提示词按阶段保存在 [ai-prompts.md](ai-prompts.md)。实际开发过程依次完成算法和时间轴、对比和 Blender 导出、跨程序验证和课程交付。Python 使用现有 numpy 和标准库；bpy 仅在 Blender 自带 Python 中运行。

## 实际发现与修正

- 基线 112 项测试通过。原示例直接运行时包查找路径有问题，补充项目根目录到 sys.path，使无界面示例可独立执行。
- 旧时间轴按重采样折线计算长度，可能跨越曲线顶点或折线转角。改为保留全部原始顶点，时间取原始累计弧长，4000 只作为额外采样预算。
- Blender 初版逐点 `keyframe_insert` 会合并近子帧，精度检查失败。批量写 F 曲线后，调用 `update()` 仍会去重；最终直接写入已排序坐标，不调用去重，保存重开后位置检查仍通过。
- Blender 5.1 的 FFmpeg 需先把 image_settings.media_type 设置为 VIDEO；修正后生成 1080p H.264 MP4。静态图片输出切回 IMAGE。
- 小尺度场景最初灯光过曝，降低灯光能量并用 AgX；复核斜视、加工中、俯视三张 Cycles 图片。
- 小窗口原横向布局压缩三维视图，增加窄窗口纵向布局。前端快移刀路保留原始 Z 高度，避免显示为加工面上的平线。

上述修正均由 Codex 在运行结果反馈后完成；学生尚需熟悉代码、亲自演示并检查学校提交信息。报告不将自动化检查写成教师或学生的人工确认。

## 2026 年 10 月 6 日验收

128 项 unittest 通过（基线 112 项，新增 16 项），覆盖策略、异常、覆盖采样、时间轴、ZIP/CSV 和 HTTP 回归。七种 Blender 5.1.2 工况均完成独立包建场景与重新打开校验；最大误差约 0.000260285 mm，小于 0.01 mm。该检查为有限采样点数值验证，不宣称对所有可能输入作形式证明。

标准工况预计工时：螺旋 125.171993 s、往复 121.057442 s、单向 134.921334 s。螺旋连续切削只有三段运动，但本工况没有比往复更快。栅格与螺旋的覆盖和边界处理不同，不能仅用工时判断工艺优劣。

EEVEE 生成 1920×1080、30 fps、10 倍速视频；Cycles 输出三张 1080p 图片，可编辑 .blend 保留双相机和内嵌数据。Word 报告与介绍 PPT 在功能开发和自动化验收完成后制作。

## 2026 年 10 月 8 日启动修复

用户反馈双击完整交付包的平台 BAT 出现截断命令、乱码和路径错误。复现确认旧 BAT 使用 UTF-8 中文内容及 LF，切换 Windows cmd 代码页时发生解析错误。此前检查了 Python 语法，但未实际运行交付 BAT，漏掉了这个问题。

平台和 Blender 入口统一由 `export/windows.py` 生成纯 ASCII、CRLF BAT，并改用 ASCII Python 文件名。增加解释器及 numpy 检测、全新目录自动解压源码、重复启动识别和 `platform-launch.log`。另发现 CLI 的 SO_REUSEADDR 端口探测在 Windows 下可能错误绑定已监听端口，移除探测时的该选项，验证占用后自动使用下一端口。

136 项 unittest 通过（新增 8 项启动回归），无界面规划示例通过。真实 cmd 在 936 与 65001 代码页、中文/空格/括号/&/! 目录下通过；独立源码 ZIP 从其他工作目录启动，网页、规划、对比、NC、Blender 导出均正常，重复启动及端口占用检查通过。网页新下载的 Blender ZIP 通过 BAT 建场景并在 Blender 5.1.2 重开验证，312 个位置探针最大误差约 0.000113716 mm。集成脚本为 `examples/verify_course_launcher.py`，实测摘要与日志随修正版交付包保存。未重新制作 Word、PPT 或渲染素材。

## 2026 年 10 月 8 日汇报整合与源码同步

用户要求汇报面向课程老师，以通俗语言说明功能与成果，并整合已完成的 UG 刀路编程 PPT。经用户确认 5–8 分钟、浅色蓝白、16 页方案后，Codex 新建整合版 PPT，保留 UG 六页的原有文字与图片，加入平台操作、策略对比、Blender 操作与渲染成果、真实 AI 分工和启动故障反馈过程。演讲备注按约 7 分钟安排，第 12 页嵌入原有 10 倍速视频。原始 UG 和独立功能介绍 PPT 保留。

制作后用 PowerPoint 重新打开、逐页查看并实测视频播放；在独立副本中修改原生图表数据并保存重开。上述检查针对当时生成版本；GitHub 同步时发现用户本地 `交付成果` 中的整合稿已调整为 13 页，封面信息也有修改，视频位于第 11 页。本次按该最新文件原样上传，不覆盖后续人工编辑。核对 ZIP 完整性、页数和视频内嵌状态；未把早期 16 页稿的全部视觉检查结论套用于后续修改版。`course-deliverables/manifest.json` 记录实际上传副本的字节数和 SHA-256。

用户随后授权更新自己的 GitHub 功能分支。同步前重新运行 136 项 unittest 和无界面规划示例，均通过；本次没有改动规划算法或重新渲染 Blender。最初沙箱执行因本机 HTTP 与临时目录访问限制出现错误，解除执行限制后全量检查通过。最新报告、整合 PPT、独立视频、场景、图片与实验摘要集中保存于 `course-deliverables/`。完整压缩交付包、运行日志与临时制作文件不纳入源码仓库。

## 可核实资料

- [ToolpathLab 基座说明](https://github.com/large-su/toolpath-lab/blob/main/README.md)
- [基座扩展机制](https://github.com/large-su/toolpath-lab/blob/main/docs/extending.md)
- [OpenAI 项目指令 AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Blender 5.1 Python API](https://docs.blender.org/api/5.1/bpy.types.bpy_struct.html#bpy.types.bpy_struct.keyframe_insert)
