@echo off
rem ---------------------------------------------------------------------------
rem  ToolpathLab 启动器（Windows）
rem
rem  双击本文件即可：
rem    1. 找一个带 numpy 的 Python 3.10+（没有就建本地 .venv 并安装）；
rem    2. 找 Node.js 并确保 Electron 已安装（首次会自动 npm install）；
rem    3. 用 Electron 打开独立窗口（没有 Node.js 时退回浏览器方式）。
rem  参数会原样传给后端，例如 start.bat --port 8800（仅在退回浏览器方式时生效，
rem  桌面模式下端口由 Electron 自动挑一个空闲的）。
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title ToolpathLab

set "PYTHON="

rem -- 1. 本项目的虚拟环境 ------------------------------------------------------
if not exist ".venv\Scripts\python.exe" goto :probe_python
".venv\Scripts\python.exe" -c "import numpy" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=.venv\Scripts\python.exe"
    goto :python_ready
)
echo [提示] .venv 缺少 numpy，尝试安装 ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not errorlevel 1 (
    set "PYTHON=.venv\Scripts\python.exe"
    goto :python_ready
)
echo [提示] .venv 不可用，继续查找其它 Python 解释器。

:probe_python
rem -- 2. 已经带 numpy 的解释器 -------------------------------------------------
for %%C in (python python3) do call :probe_numpy %%C
if defined PYTHON goto :python_ready
call :probe_numpy "py -3"
if defined PYTHON goto :python_ready
call :probe_numpy "%USERPROFILE%\miniconda3\python.exe"
if defined PYTHON goto :python_ready
call :probe_numpy "%USERPROFILE%\anaconda3\python.exe"
if defined PYTHON goto :python_ready
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :probe_numpy "%%D\python.exe"
if defined PYTHON goto :python_ready
call :probe_numpy "%ProgramData%\miniconda3\python.exe"
if defined PYTHON goto :python_ready

rem -- 3. 否则取第一个可用的解释器，稍后建虚拟环境 --------------------------------
for %%C in (python python3) do call :probe_any %%C
if defined PYTHON goto :make_venv
call :probe_any "py -3"
if defined PYTHON goto :make_venv

echo.
echo [错误] 没有找到 Python 3.10 及以上版本。请先安装：
echo        https://www.python.org/downloads/   安装时请勾选 Add python.exe to PATH
echo        或安装 Anaconda / Miniconda。
echo.
pause
exit /b 1

:probe_numpy
"%~1" -c "import numpy" >nul 2>nul
if errorlevel 1 goto :eof
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PYTHON=%~1"
goto :eof

:probe_any
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PYTHON=%~1"
goto :eof

:make_venv
echo [1/3] 使用解释器: %PYTHON%
echo [2/3] 未检测到 numpy，正在创建本地虚拟环境 .venv 并安装依赖 ...
%PYTHON% -m venv .venv
if errorlevel 1 goto :venv_failed
if not exist ".venv\Scripts\python.exe" goto :venv_failed
set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" -m pip install --upgrade pip >nul 2>nul
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed
goto :python_ready

:venv_failed
echo.
echo [错误] 创建虚拟环境失败。请手动执行：
echo        %PYTHON% -m venv .venv
echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:install_failed
echo.
echo [错误] 依赖安装失败（多数是网络问题）。可换国内镜像重试：
echo        .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:python_ready
echo [1/3] Python 就绪: %PYTHON%
set "TOOLPATH_LAB_PYTHON=%PYTHON%"

rem -- 4. Node.js / Electron ---------------------------------------------------
where node >nul 2>nul
if errorlevel 1 goto :no_node
echo [2/3] Node.js 就绪: & node -v

if exist "node_modules\electron\dist\electron.exe" goto :run_electron
echo [3/3] 首次运行，正在安装 Electron（约 200 MB，请耐心等待）...
call npm install
if not errorlevel 1 goto :ensure_binary
echo [提示] 常规安装失败，改用 npmmirror 镜像重试 ...
set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"
call npm install --registry=https://registry.npmmirror.com
if errorlevel 1 goto :electron_failed
goto :ensure_binary

:ensure_binary
rem npm install 有时不会顺带下载 Electron 二进制，这里补一次。
if exist "node_modules\electron\dist\electron.exe" goto :run_electron
echo [提示] Electron 二进制未随 npm install 下载，正在单独获取 ...
set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"
node node_modules\electron\install.js
if errorlevel 1 goto :electron_failed
if not exist "node_modules\electron\dist\electron.exe" goto :electron_failed

:run_electron
echo [3/3] 启动独立窗口 ...
echo.
"node_modules\.bin\electron.cmd" .
echo.
echo 窗口已关闭。
pause
exit /b 0

:electron_failed
echo.
echo [错误] Electron 安装失败。可以先只用浏览器方式运行：
echo        %PYTHON% -m toolpath_lab
echo.
pause
exit /b 1

:no_node
echo [2/3] 未找到 Node.js，将以浏览器方式打开（体验一致，只是没有独立窗口）。
echo       想要独立窗口请安装 Node.js LTS: https://nodejs.org/
echo.
set PYTHONIOENCODING=utf-8
%PYTHON% -m toolpath_lab %*
echo.
echo 服务已退出。
pause
exit /b 0
