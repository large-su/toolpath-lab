"""Windows integration: fresh source extraction, real BAT, HTTP and Blender build."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import zipfile


CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(port, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    query = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", data=data,
                                   headers={"Content-Type": "application/json"})
    with CLIENT.open(query, timeout=5) as response:
        return response.read()


def free_pair():
    for port in range(8788, 8850, 2):
        with socket.socket() as first, socket.socket() as second:
            try:
                first.bind(("127.0.0.1", port))
                second.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local ports for verification")


def command(bat, args="", codepage=936):
    # Pass cmd's own quote syntax directly; list2cmdline escapes it incorrectly.
    return f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /c chcp {codepage}>nul & call "{bat}" {args}'


def stop_owned_process(process):
    if process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=True)
        process.wait(timeout=10)


def wait_ready(process, port):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError(f"BAT terminated with code {process.returncode}")
        try:
            health = json.loads(request(port, "api/health"))
            if health.get("service") == "toolpath-lab":
                return health
        except (OSError, ValueError):
            pass
        time.sleep(0.15)
    raise RuntimeError("BAT did not start the platform within 20 seconds")


class OtherService(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true,"service":"other"}')

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("delivery")
    parser.add_argument("--output", default="exports/launcher-qa")
    parser.add_argument("--blender", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Retain the directory and logs as evidence; do not remove any user files.
    fresh = Path(tempfile.mkdtemp(prefix="数控 全新解压 (&!) ", dir=output))
    delivery = Path(args.delivery).resolve()
    for name in ("启动平台.bat", "launch_platform.py", "toolpath-lab源码.zip"):
        shutil.copy2(delivery / name, fresh / name)
    assert not (fresh / "toolpath-lab").exists()
    env = {**os.environ, "TOOLPATHLAB_NO_PAUSE": "1", "BLENDER_EXE": args.blender}
    # Exercise auto discovery, without the explicit interpreter override.
    env.pop("TOOLPATH_LAB_PYTHON", None)
    elsewhere = Path(os.environ["SystemRoot"]) / "System32"
    checks = []
    for page in (936, 65001):
        check = subprocess.run(command(fresh / "启动平台.bat", "--check", page),
                               cwd=elsewhere, env=env, input="", capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=30)
        (output / f"check-{page}.log").write_text(check.stdout + check.stderr, encoding="utf-8")
        if check.returncode:
            raise RuntimeError(f"Codepage {page}: BAT check failed")
        checks.append({"codepage": page, "exit_code": check.returncode})
    port = free_pair()
    payload = {"region": {"shape": "circle"}, "planner": {"id": "spiral"},
               "blender": {"playback_speed": 10}}
    with (output / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command(fresh / "启动平台.bat", f"--no-browser --port {port}"),
                                   cwd=elsewhere, env=env, stdout=log, stderr=log)
        try:
            health = wait_ready(process, port)
            catalog = json.loads(request(port, "api/catalog"))
            assert any(item["id"] == "spiral" for item in catalog["planners"]["list"])
            assert b"ToolpathLab" in request(port, "")
            plan = json.loads(request(port, "api/plan", payload))
            assert plan["ok"]
            comparison = json.loads(request(port, "api/compare", payload))
            assert len(comparison["rows"]) == 3
            assert b"G" in request(port, "api/export/gcode", payload)
            blob = request(port, "api/export/blender", payload)
            (output / "downloaded-blender.zip").write_bytes(blob)
            package = output / "blender-bat-build"
            package.mkdir(exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                for name in ("01_create_scene.bat", "02_render_preview.bat", "03_render_cycles.bat"):
                    content = archive.read(name)
                    content.decode("ascii")
                    assert content.count(b"\n") == content.count(b"\r\n")
                archive.extractall(package)
            repeat = subprocess.run(command(fresh / "启动平台.bat", f"--no-browser --port {port}"),
                                    cwd=elsewhere, env=env, input="", capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=20)
            assert repeat.returncode == 0 and "已运行" in repeat.stdout
            (output / "repeat.log").write_text(repeat.stdout + repeat.stderr, encoding="utf-8")
        finally:
            stop_owned_process(process)
    port = free_pair()
    other = ThreadingHTTPServer(("127.0.0.1", port), OtherService)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        with (output / "occupied-port.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command(fresh / "启动平台.bat", f"--no-browser --port {port}"),
                                       cwd=elsewhere, env=env, stdout=log, stderr=log)
            try:
                wait_ready(process, port + 1)
            finally:
                stop_owned_process(process)
    finally:
        other.shutdown()
        other.server_close()
    build = subprocess.run(command(package / "01_create_scene.bat", "--no-open"),
                           cwd=elsewhere, env=env, input="", capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    (output / "blender-bat-build.log").write_text(build.stdout + build.stderr, encoding="utf-8")
    if build.returncode or not (package / "machining.blend").is_file():
        raise RuntimeError("Downloaded Blender BAT build failed; see the build log")
    reopened = subprocess.run([args.blender, "--background", str(package / "machining.blend"),
                               "--python-exit-code", "1", "--python", str(package / "build_scene.py"),
                               "--", "--verify-only", "--output", str(package / "machining.blend")],
                              cwd=package, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    (output / "blender-reopen.log").write_text(reopened.stdout + reopened.stderr, encoding="utf-8")
    if reopened.returncode:
        raise RuntimeError("Blender reopen verification failed")
    summary = {"date": "2026-10-08", "fresh_directory": str(fresh), "cmd_checks": checks,
               "source_extracted": True, "health": health, "web_plan_compare_gcode_blender": "passed",
               "repeat_start": "reused", "occupied_port": "selected_next_free_port",
               "blender_bat_build_and_reopen": json.loads((package / "verify.json").read_text(encoding="utf-8"))}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
