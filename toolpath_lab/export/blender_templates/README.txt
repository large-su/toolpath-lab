ToolpathLab Blender 加工动画导出包

要求：Python 3.10+ 和 Blender 5.1（已在 5.1.2 验证）。
BAT 为纯 ASCII 内容及 Windows CRLF 换行，可放在中文或含空格的目录。
启动器依次检查 TOOLPATH_LAB_PYTHON、本目录 .venv、Codex 自带 Python、系统 Python 和常见 Conda 安装。
若找不到 Python，可设置 TOOLPATH_LAB_PYTHON 为完整 python.exe 路径。
1. 先解压整个 ZIP。不要直接在压缩包中运行脚本。
2. 双击 01_create_scene.bat，生成 machining.blend 并打开 Blender。
3. 按空格播放；在相机列表中选择 Camera_Oblique 或 Camera_Top。
4. 双击 02_render_preview.bat 输出 EEVEE 1080p MP4。
5. 双击 03_render_cycles.bat 输出 Cycles 1080p MP4，耗时随设备而变化。
命令行运行 01_create_scene.bat --no-open 可只建场景而不打开 Blender 窗口。

scene.json 中的坐标单位为毫米，Blender 内部换算为米。
Tool_Tip 是刀尖基准，位置关键帧为线性插值；保留时间戳与子帧。
原加工工时与演示播放倍率分别保存，视频中的倍率标签不会改变工时。
工件几何固定。本演示是刀具运动动画，不包含材料切除、加速度或切削力。
显示刀路颜色：橙色切削、黄色连接、蓝色快移；绿色为已经经过的轨迹。

如找不到 Blender，在命令提示符设置：
set "BLENDER_EXE=C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
然后在该窗口运行相应 .bat。

命令行（在 Blender 中执行 build_scene.py）：
blender --background --factory-startup --python-exit-code 1 --python build_scene.py -- --data scene.json --output machining.blend --render stills
可选：--render video、--engine CYCLES、--verify-only。
复验读取生成场景中的嵌入数据，并在 verify.json 中保存位置误差。
重复生成将更新本包内同名输出，请先另存自己编辑过的 .blend 场景。
