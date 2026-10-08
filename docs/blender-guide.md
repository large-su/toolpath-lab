# 圆形螺旋与 Blender 使用说明

本扩展用于课程刀路规划和运动展示。刀具运动与轨迹可渲染，工件保持固定形状。默认模型为恒定进给，不包含材料切除、真实机床动力学或切削力。

## 在平台生成

1. 安装原项目 Python 依赖，运行 `python -m toolpath_lab`，打开终端显示的本地地址。
2. 点击“螺旋示例”，自动设置圆形直径 80 mm、平底刀直径 6 mm、刀长 30 mm、切宽 3 mm、进给 800 mm/min、采样步长 0.5 mm。
3. 播放、拖动进度或切换俯视。可修改径向方向为“由内向外”，再生成。
4. 点击“策略对比”查看三种策略，导出 CSV。当前栅格策略最小切宽 0.5 mm，因此对比切宽不能低于该值。
5. “导出 NC”使用原 G-code 接口。此处加工面为 Z=0、安全高度 5 mm，实际加工前必须结合机床、夹具和工艺检查程序。

螺旋只支持圆形平底刀。切宽不得超过刀具直径，区域直径必须大于刀具直径。超出十万刀点时，可增大采样步长或切宽。步长只控制最大采样弧长，0.01 mm 弦误差还会进一步加密。

## 下载与建场景

1. 在“Blender 导出设置”选择帧率、倍率、相机和渲染器。默认 30 fps、1 倍速。课程示例视频用 10 倍速，画面标注 10x。
2. 点击“导出 Blender”，将 ZIP 解压到普通可写目录，**不要直接在压缩包中运行**。
3. 双击 `01_create_scene.bat`，脚本自动查找 Blender，构建 `machining.blend`，验证刀尖位置并启动 Blender 打开场景。
4. 在 Blender 中按空格播放；顶部时间线范围已设置。小键盘 0 进入相机视图；无小键盘可用“视图 → 相机 → 活动相机”。
5. `02_render_preview.bat` 用 EEVEE 渲染 MP4；`03_render_cycles.bat` 用 Cycles 渲染 MP4，时间更长。

启动 BAT 会检查 Python 3.10+，可使用本目录 .venv、Codex 自带 Python、系统 Python 或常见 Conda 安装；也可用 `TOOLPATH_LAB_PYTHON` 指定解释器。BAT 保持纯 ASCII 与 CRLF，支持中文和含空格的目录。命令行运行 `01_create_scene.bat --no-open` 可只建场景、不打开窗口。如没有外部 Python，可在 PowerShell 中直接使用 Blender 命令：

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.1\blender.exe' --background --factory-startup --python-exit-code 1 --python .\build_scene.py -- --data .\scene.json --output .\machining.blend
```

追加 `--render video --engine BLENDER_EEVEE` 可渲染视频；追加 `--render stills --engine CYCLES` 可渲染 `overview.png`、`machining.png` 和 `top.png`。自定义安装路径时设环境变量 `BLENDER_EXE` 或直接替换上述可执行文件路径。

## 场景与数据

ZIP 含 `scene.json`、`build_scene.py`、`launch_blender.py`、三个 BAT 和中文 README。JSON schema 为 `toolpath-lab.blender`、版本 1，含区域、刀具、原始运动段、时间轴、统计与设置。Blender 将毫米乘 0.001 转为米，保持 Z 轴向上，`Tool_Tip` 空物体代表刀尖；刀具网格作为其子物体。

位置帧号为 `1 + time_s × fps / playback_speed`。三轴位置曲线均为 LINEAR，保留子帧；不用 Blender 自动缓动。斜视和俯视相机分别为 `Camera_Oblique`、`Camera_Top`。橙色为切削、黄色为连接、蓝色为快移、绿色为已走轨迹。刀具槽纹仅为外观模型，规划仍以名义圆形足迹计算。

为了避免 Blender 合并很近的子帧，脚本批量写入已排序 F 曲线，不调用会去重的 `keyframe_insert` 或 `fcurve.update`。场景内嵌 `ToolpathLab_Data.json`，重新打开后可独立校验：

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.1\blender.exe' --background .\machining.blend --python-exit-code 1 --python .\build_scene.py -- --verify-only --output .\machining.blend
```

验证写入 `verify.json`，同时检查位置误差不超过 0.01 mm、统计与时间轴时长误差不超过 0.000001 s。视频整数帧的结束时间因向上取整可能比映射的精确终点略晚，最后位置保持不动。

## 可重复验收

```powershell
python -m unittest discover -s tests -v
python examples/create_blender_demo.py --output exports/demo
python examples/verify_blender.py --blender 'C:\Program Files\Blender Foundation\Blender 5.1\blender.exe' --output exports/verification
```

第三条会验证外向内、内向外、小区域、刀具接近区域尺寸、6 mm 切宽、方形栅格和圆形栅格，分别建场景并重新打开。实测数据见课程交付目录。

## 完整交付包启动与排错

先解压完整课程 ZIP，再双击 `启动平台.bat`；保留同目录 `launch_platform.py` 和 `toolpath-lab源码.zip`。首次启动会自动解压随包源码，重复启动会识别已有的课程服务。端口被其他程序占用时，自动尝试后续端口，以终端显示的地址为准。运行期间保留终端窗口。

在交付目录执行 `启动平台.bat --check` 可检查 Python、numpy、源码和螺旋规划，不打开网页。详细错误保存为同目录 `platform-launch.log`。不能在 ZIP 预览窗口中只运行 BAT；请把整个交付包解压到可写文件夹。

维护交付包：运行 `python examples/update_course_launchers.py <交付目录>` 同步平台和 Blender 启动入口；然后更新源码 ZIP 和完整 ZIP。Windows 集成验证用 `python examples/verify_course_launcher.py <交付目录> --output exports/launcher-qa --blender <blender.exe完整路径>`，包括全新解压目录、两种代码页、真实 BAT、HTTP、端口占用、重复启动及下载后 Blender 建场景和重开。
