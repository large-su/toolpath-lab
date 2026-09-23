#!/usr/bin/env bash
# ToolpathLab 启动器（Linux / macOS）。Windows 用户请双击 start.bat。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "[错误] 未找到 Python，请先安装 Python 3.10+" >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c "import numpy" >/dev/null 2>&1; then
    echo "[提示] 未检测到 numpy，正在创建 .venv ..."
    "$PYTHON_BIN" -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pip install --upgrade pip >/dev/null
    python -m pip install -r requirements.txt
    PYTHON_BIN=python
fi

# 有 Electron 就用独立窗口，否则退回浏览器方式（体验一致，只是没有独立窗口）。
export TOOLPATH_LAB_PYTHON="$PYTHON_BIN"
if [ -x "node_modules/.bin/electron" ]; then
    exec node_modules/.bin/electron .
fi
if command -v npm >/dev/null 2>&1; then
    echo "[提示] 首次运行，正在安装 Electron（约 200 MB）..."
    npm install && exec node_modules/.bin/electron .
fi
echo "[提示] 未找到 Node.js / Electron，改用浏览器方式打开。"
exec "$PYTHON_BIN" -m toolpath_lab "$@"
