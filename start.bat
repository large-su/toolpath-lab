@echo off
rem ===========================================================================
rem  ToolpathLab launcher (Windows)
rem
rem  Double click this file:
rem    1. finds a Python 3.10+ that already has numpy (the choice is cached);
rem    2. makes sure Electron is available (npm install on first run only);
rem    3. opens the desktop window, then this console closes itself.
rem
rem  This file is ASCII only on purpose: cmd.exe mis-parses a batch file that
rem  switches code page (chcp) while it contains multi-byte characters, which
rem  used to print a burst of "not recognized as an internal or external
rem  command" lines. Chinese documentation lives in README.md.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title ToolpathLab

set "CACHE_DIR=%LOCALAPPDATA%\ToolpathLab"
set "CACHE_FILE=%CACHE_DIR%\python.txt"
set "LAUNCH_LOG=%TEMP%\toolpathlab-launch.log"
set "PYTHON="

rem -- 1. interpreter used last time (skips every probe below) ---------------
if not exist "%CACHE_FILE%" goto :probe_python
set "CACHED="
set /p CACHED=<"%CACHE_FILE%"
if not defined CACHED goto :probe_python
call :probe_numpy "%CACHED%"
if defined PYTHON goto :python_ready

rem -- 2. the virtual environment of this project ---------------------------
:probe_python
if not exist ".venv\Scripts\python.exe" goto :probe_system
call :probe_numpy ".venv\Scripts\python.exe"
if defined PYTHON goto :python_ready

rem -- 3. system interpreters, preferring one that already has numpy --------
:probe_system
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

rem -- 4. nothing has numpy: create a local virtual environment ------------
for %%C in (python python3) do call :probe_any %%C
if defined PYTHON goto :make_venv
call :probe_any "py -3"
if defined PYTHON goto :make_venv
goto :no_python

:make_venv
echo [1/2] numpy not found, creating .venv and installing requirements ...
%PYTHON% -m venv .venv
if errorlevel 1 goto :venv_failed
if not exist ".venv\Scripts\python.exe" goto :venv_failed
set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" -m pip install --upgrade pip >nul 2>nul
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed
goto :python_ready

:python_ready
if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%" >nul 2>nul
>"%CACHE_FILE%" echo %PYTHON%
set "TOOLPATH_LAB_PYTHON=%PYTHON%"
set "TOOLPATH_LAB_TIMING_LOG=%LAUNCH_LOG%"
del "%LAUNCH_LOG%" >nul 2>nul
echo [1/2] Python: %PYTHON%

rem -- 5. Electron ----------------------------------------------------------
if exist "node_modules\electron\dist\electron.exe" goto :launch

where node >nul 2>nul
if errorlevel 1 goto :no_node
echo [2/2] First run: installing Electron (about 200 MB, once) ...
call npm install
if exist "node_modules\electron\dist\electron.exe" goto :launch
echo [info] retrying with the npmmirror registry ...
set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"
call npm install --registry=https://registry.npmmirror.com
if exist "node_modules\electron\dist\electron.exe" goto :launch
node node_modules\electron\install.js
if not exist "node_modules\electron\dist\electron.exe" goto :electron_failed

:launch
echo [2/2] Opening the window ...
start "" "node_modules\electron\dist\electron.exe" .
rem Wait until the app reports "UI-READY" in its launch log (30 s at most).
set /a WAITED=0
:wait_window
findstr /c:"UI-READY" "%LAUNCH_LOG%" >nul 2>nul
if not errorlevel 1 exit /b 0
set /a WAITED+=1
if %WAITED% GEQ 30 goto :launch_failed
ping -n 2 127.0.0.1 >nul 2>nul
goto :wait_window

rem -- failures -------------------------------------------------------------
:launch_failed
echo.
echo [error] the window did not become ready within 30 s.
echo launch log: %LAUNCH_LOG%
echo ---------------------------------------------------------------------
type "%LAUNCH_LOG%" 2>nul
echo ---------------------------------------------------------------------
echo Try the backend directly to see the error:
echo     %PYTHON% -m toolpath_lab
echo.
pause
exit /b 1

:electron_failed
echo.
echo [error] Electron could not be installed. You can still run it in a browser:
echo     %PYTHON% -m toolpath_lab
echo.
pause
exit /b 1

:venv_failed
echo.
echo [error] creating the virtual environment failed. Please run manually:
echo     %PYTHON% -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:install_failed
echo.
echo [error] installing requirements failed (usually network). Retry with a mirror:
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:no_python
echo.
echo [error] no Python 3.10+ found. Install it first:
echo     https://www.python.org/downloads/   (tick "Add python.exe to PATH")
echo     or install Anaconda / Miniconda.
echo.
pause
exit /b 1

:no_node
echo [2/2] Node.js not found: opening in the default browser instead.
echo       For a desktop window install Node.js LTS: https://nodejs.org/
echo.
set PYTHONIOENCODING=utf-8
%PYTHON% -m toolpath_lab %*
echo.
echo server stopped.
pause
exit /b 0

rem -- subroutines -----------------------------------------------------------
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
