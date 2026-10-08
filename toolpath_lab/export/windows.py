"""ASCII/CRLF Windows entry points shared by platform and Blender bundles."""
import re


def python_launcher_bat(script: str, arguments: tuple[str, ...] = (), *, needs_numpy: bool = False) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.py", script):
        raise ValueError("Batch entry point must have an ASCII filename")
    if any(not re.fullmatch(r"[-A-Za-z0-9_.]+", item) for item in arguments):
        raise ValueError("Batch arguments must be literal ASCII tokens")
    probe = "import sys; sys.exit(1) if sys.version_info < (3,10) else None"
    if needs_numpy:
        probe += "; import numpy"
    lines = [
        "@echo off", "setlocal DisableDelayedExpansion", "chcp 65001 >nul",
        'cd /d "%~dp0"', "if errorlevel 1 goto :bad_directory",
        'set "LAB_PYTHON="', 'set "LAB_PYTHON_ARGS="',
        'if not defined TOOLPATH_LAB_PYTHON goto :local_python',
        'call :probe "%TOOLPATH_LAB_PYTHON%"', "if defined LAB_PYTHON goto :run",
        ":local_python", r'call :probe "%~dp0.venv\Scripts\python.exe"',
        "if defined LAB_PYTHON goto :run",
        r'call :probe "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"',
        "if defined LAB_PYTHON goto :run",
        'call :probe "py" "-3"', "if defined LAB_PYTHON goto :run",
        'call :probe "python"', "if defined LAB_PYTHON goto :run",
        'call :probe "python3"', "if defined LAB_PYTHON goto :run",
        r'call :probe "%USERPROFILE%\miniconda3\python.exe"', "if defined LAB_PYTHON goto :run",
        r'call :probe "%USERPROFILE%\anaconda3\python.exe"', "if defined LAB_PYTHON goto :run",
        'echo [ERROR] Python 3.10+ ' + ("with numpy " if needs_numpy else "") + "was not found.",
        "echo Install Python and run: python -m pip install numpy" if needs_numpy else "echo Install Python or set TOOLPATH_LAB_PYTHON to python.exe.",
        "goto :failed", ":run", 'echo [INFO] Python: "%LAB_PYTHON%" %LAB_PYTHON_ARGS%',
        f'"%LAB_PYTHON%" %LAB_PYTHON_ARGS% -X utf8 "%~dp0{script}" ' + " ".join(arguments) + " %*",
        'set "LAB_EXIT_CODE=%ERRORLEVEL%"', "if %LAB_EXIT_CODE% EQU 0 exit /b 0",
        "echo.", "echo [ERROR] Operation failed. See the message above and platform-launch.log if present.",
        "if not defined TOOLPATHLAB_NO_PAUSE pause", "exit /b %LAB_EXIT_CODE%",
        ":bad_directory", "echo [ERROR] Extract the complete ZIP to a writable folder before running this BAT.",
        ":failed", "if not defined TOOLPATHLAB_NO_PAUSE pause", "exit /b 1",
        ":probe", f'"%~1" %~2 -X utf8 -c "{probe}" >nul 2>nul',
        "if errorlevel 1 exit /b 0", 'set "LAB_PYTHON=%~1"', 'set "LAB_PYTHON_ARGS=%~2"', "exit /b 0",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")
