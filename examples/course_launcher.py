"""Standalone course entry point, copied as launch_platform.py next to the BAT."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
from pathlib import Path, PurePosixPath
import sys
import traceback
import urllib.request
import webbrowser
import zipfile


class Tee:
    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log

    def write(self, value):
        self.terminal.write(value)
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def is_source(folder):
    return all((folder / name).is_file() for name in (
        "toolpath_lab/__init__.py", "toolpath_lab/planning/spiral.py", "toolpath_lab/web/index.html"))


def locate_source(here):
    for folder in (here / "toolpath-lab", here.parent / "toolpath-lab", here.parent):
        if is_source(folder):
            return folder.resolve()
    archive = here / "toolpath-lab源码.zip"
    if not archive.is_file():
        raise RuntimeError("找不到完整源码。请解压完整交付包，并保留 BAT、launch_platform.py 和 toolpath-lab源码.zip 在同一目录。")
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        required = {"toolpath-lab/toolpath_lab/__init__.py", "toolpath-lab/toolpath_lab/planning/spiral.py", "toolpath-lab/toolpath_lab/web/index.html"}
        if not required.issubset(names):
            raise RuntimeError("源码 ZIP 缺少必要文件，请重新解压新版完整交付包。")
        for name in names:
            normalized = name.replace("\\", "/")
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "toolpath-lab":
                raise RuntimeError("源码 ZIP 的目录结构无效，请使用原始完整交付包。")
        print("首次启动：正在解压随包源码……", flush=True)
        source.extractall(here)
    folder = here / "toolpath-lab"
    if not is_source(folder):
        raise RuntimeError("源码未成功解压，请检查文件夹是否可写。")
    return folder.resolve()


def existing_platform(url):
    # Local requests must not be sent through a configured network proxy.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with client.open(url + "api/health", timeout=1) as response:
            health = json.load(response)
        if health.get("service") != "toolpath-lab" or not health.get("ok"):
            return False
        with client.open(url + "api/catalog", timeout=1) as response:
            catalog = json.load(response)
        return "blender" in catalog and any(item.get("id") == "spiral" for item in catalog["planners"]["list"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def launch(argv, here):
    parser = argparse.ArgumentParser(description="ToolpathLab 课程平台启动器")
    parser.add_argument("--check", action="store_true", help="检查环境和规划，不启动窗口")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv)
    print("Python:", sys.executable, flush=True)
    if sys.version_info < (3, 10):
        raise RuntimeError("需要 Python 3.10 或更新版本。")
    try:
        import numpy
    except ImportError as error:
        raise RuntimeError(f'当前 Python 缺少 numpy。请运行："{sys.executable}" -m pip install numpy') from error
    print("numpy:", numpy.__version__, flush=True)
    repo = locate_source(here)
    print("源码目录:", repo, flush=True)
    sys.path.insert(0, str(repo))
    from toolpath_lab.cli import main as server_main
    if args.check:
        from toolpath_lab.server.schema import PlanRequest
        from toolpath_lab.server.service import execute_plan
        result = execute_plan(PlanRequest.from_payload({"region": {"shape": "circle"}, "planner": {"id": "spiral"}}))
        print("启动检查通过，螺旋工时:", result.toolpath.estimated_time_s, "s", flush=True)
        return 0
    url = f"http://127.0.0.1:{args.port}/"
    if existing_platform(url):
        print("检测到已运行的课程平台:", url, flush=True)
        if not args.no_browser and not webbrowser.open(url):
            print("浏览器未自动打开，请手动访问上述地址。", flush=True)
        return 0
    print("正在启动本地平台。运行期间请保留这个终端窗口；关闭窗口会停止服务。", flush=True)
    options = ["--host", "127.0.0.1", "--port", str(args.port)]
    if args.no_browser:
        options.append("--no-browser")
    return server_main(options)


def main(argv=None, here=None):
    here = Path(here) if here else Path(__file__).resolve().parent
    logfile = here / "platform-launch.log"
    try:
        with logfile.open("w", encoding="utf-8") as log:
            with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
                try:
                    return launch(argv, here)
                except Exception:
                    print("启动失败，详细记录保存在:", logfile, file=sys.stderr)
                    traceback.print_exc()
                    return 1
    except OSError as error:
        print("无法写入启动日志，请先把完整 ZIP 解压到可写文件夹。", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
