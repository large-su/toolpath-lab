"""Command line entry point: ``python -m toolpath_lab``."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from typing import Sequence

from toolpath_lab import __version__
from toolpath_lab.server.app import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolpath-lab",
        description="ToolpathLab - 刀路规划实验台 (tool + region + raster/contour toolpaths)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8770, help="监听端口 (默认 8770)")
    parser.add_argument(
        "--no-browser", action="store_true", help="启动后不自动打开浏览器"
    )
    parser.add_argument(
        "--version", action="version", version=f"toolpath-lab {__version__}"
    )
    return parser


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(host: str, preferred: int, attempts: int = 20) -> int:
    """Return the first free port at or after the preferred one."""

    for offset in range(attempts):
        candidate = preferred + offset
        if port_is_free(host, candidate):
            return candidate
    raise SystemExit(
        f"端口 {preferred}-{preferred + attempts - 1} 都被占用，请用 --port 指定其它端口"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    port = pick_port(args.host, args.port)
    if port != args.port:
        print(f"提示: 端口 {args.port} 已被占用，改用 {port}")
    url = f"http://{args.host}:{port}/"
    server = create_server(args.host, port)
    print("=" * 62)
    print(f" ToolpathLab {__version__}  刀路规划实验台")
    print(f" 打开: {url}")
    print(" 停止: Ctrl+C")
    print("=" * 62)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
