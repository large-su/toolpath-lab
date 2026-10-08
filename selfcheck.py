"""Run the whole pre-commit checklist from CONTRIBUTING.md with one command.

    python selfcheck.py                      # skips the front-end syntax check when node is missing
    python selfcheck.py --node /path/to/node # point it at a node executable explicitly

The four checks mirror CONTRIBUTING's list, plus one HTTP smoke test which automates the manual
"open the window and click through it" step:

1. unit tests (`python -m unittest discover -s tests` must be green);
2. the headless example (the library path still works);
3. front-end and desktop shell syntax (`node --check`, covering every module under web/js);
4. HTTP smoke test: start a throwaway server on an ephemeral port, walk the API and shut it down.

Exit code 0 means everything passed; any failure exits 1 and prints that check's output.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent
if str(REPOSITORY_ROOT) not in sys.path:  # make `python selfcheck.py` work from anywhere
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _run(command: list[str]) -> tuple[bool, str]:
    """Run a command in the repository root and return (ok, its cleaned output)."""

    completed = subprocess.run(
        command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if completed.returncode != 0:
        tail = "\n".join(lines[-12:])
        return False, f"退出码 {completed.returncode}\n{tail}"
    return True, "\n".join(lines)


def check_unit_tests() -> tuple[bool, str]:
    ok, output = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    if not ok:
        return False, output
    lines = output.splitlines()
    ran = next((line for line in lines if line.startswith("Ran ")), "")
    return True, f"{ran}，{lines[-1]}" if ran else lines[-1]


def check_headless_example() -> tuple[bool, str]:
    output = REPOSITORY_ROOT / "examples" / "toolpath_demo.nc"
    output.unlink(missing_ok=True)
    ok, detail = _run([sys.executable, "examples/headless_plan.py"])
    if not ok:
        return False, detail
    if not output.exists():
        return False, "示例没有写出 examples/toolpath_demo.nc"
    return True, f"已写出 {output.name}"


def check_frontend_syntax(node: str | None) -> tuple[bool, str]:
    if node is None:
        return True, "跳过（PATH 上找不到 node，可用 --node 指定）"
    files = [REPOSITORY_ROOT / "electron" / "main.mjs"]
    files += sorted((REPOSITORY_ROOT / "toolpath_lab" / "web" / "js").glob("*.js"))
    for path in files:
        ok, detail = _run([node, "--check", str(path)])
        if not ok:
            return False, f"{path.relative_to(REPOSITORY_ROOT)}\n{detail}"
    return True, f"{len(files)} 个文件语法通过"


def _request(
    base: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def check_http_api() -> tuple[bool, str]:
    """Walk the API against a throwaway server: every shape, every strategy, exports, error codes."""

    from toolpath_lab import __version__
    from toolpath_lab.server.app import ToolpathLabHandler, create_server

    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    # Silence the per-request access log while the smoke test runs: this script prints its own
    # summary, and 20 log lines would bury it.
    original_log = ToolpathLabHandler.log_message
    ToolpathLabHandler.log_message = lambda *args, **kwargs: None
    thread.start()
    problems: list[str] = []
    steps = 0

    def expect(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    try:
        status, body = _request(base, "/api/health")
        steps += 1
        expect(status == 200 and json.loads(body)["version"] == __version__,
               f"/api/health 返回 {status}")

        status, body = _request(base, "/api/catalog")
        steps += 1
        catalog = json.loads(body)
        planners = [item["id"] for item in catalog["planners"]["list"]]
        shapes = [item["id"] for item in catalog["regions"]["shapes"]]
        expect(status == 200 and len(planners) >= 2 and len(shapes) >= 5,
               f"/api/catalog 只给出 {len(planners)} 个策略、{len(shapes)} 个形状")

        for shape in shapes:
            status, body = _request(base, "/api/plan", {"region": {"shape": shape}})
            steps += 1
            expect(status == 200, f"形状 {shape} 规划失败：{status} {body[:60]}")

        for planner in planners:
            payload = {"region": {"shape": "dumbbell"}, "planner": {"id": planner}}
            status, body = _request(base, "/api/plan", payload)
            steps += 1
            if status != 200:
                problems.append(f"策略 {planner} 规划失败：{status} {body[:60]}")
                continue
            plan = json.loads(body)
            statistics = plan["toolpath"]["statistics"]
            expect(statistics["pass_count"] > 0 and statistics["estimated_time_s"] > 0,
                   f"策略 {planner} 的统计不合理：{statistics}")
            expect(0.0 < plan["coverage"]["ratio"] <= 1.0,
                   f"策略 {planner} 的覆盖率不合理：{plan['coverage']['ratio']}")
            expect(bool(plan["toolpath"]["notes"]), f"策略 {planner} 没有给出 notes")
            expect(plan.get("removal") is not None, f"策略 {planner} 没有给出材料切除结果")

            status, csv_body = _request(base, "/api/export/csv", payload)
            steps += 1
            rows = len(
                [
                    line
                    for line in csv_body.splitlines()
                    if line and not line.startswith("#")  # the self-describing comment block
                ]
            )
            expect(status == 200 and rows == statistics["point_count"] + 1,
                   f"策略 {planner} 导出的 CSV 行数 {rows} 与刀点数 {statistics['point_count']} 不符")

        status, gcode = _request(base, "/api/export/gcode", {"region": {"shape": "square"}})
        steps += 1
        expect(status == 200 and "M30" in gcode, f"导出 NC 失败：{status}")
        expect(
            "(generated by ToolpathLab)" in gcode
            and "estimated time" in gcode
            and "coverage" in gcode,
            "导出的 NC 头部应带上自述摘要与覆盖率",
        )

        status, _ = _request(base, "/api/plan", {"region": {"shape": "nope"}})
        steps += 1
        expect(status == 400, f"未知形状应返回 400，实际 {status}")

        status, _ = _request(base, "/api/plan", {
            "tool": {"diameter_mm": 100},
            "region": {"shape": "square", "parameters": {"side_mm": 40}},
        })
        steps += 1
        expect(status == 422, f"几何不可行应返回 422，实际 {status}")

        status, _ = _request(base, "/api/nothing")
        steps += 1
        expect(status == 404, f"未知接口应返回 404，实际 {status}")

        # Corner feed reduction: off by default, and turning it on must slow the corner segments
        # without touching the geometry.
        payload = {"region": {"shape": "square"}, "planner": {"id": "contour"}}
        status, body = _request(base, "/api/plan", payload)
        steps += 1
        plain = json.loads(body) if status == 200 else {}
        payload["planner"]["parameters"] = {"corner_angle_deg": 30.0}
        status, body = _request(base, "/api/plan", payload)
        steps += 1
        if status != 200 or not plain:
            problems.append(f"拐角减速的规划失败：{status} {body[:60]}")
        else:
            slowed = json.loads(body)
            feeds = {move["feed_mm_per_min"] for move in slowed["toolpath"]["moves"]
                     if move["kind"] == "cut"}
            expect(len(feeds) > 1 and min(feeds) < max(feeds),
                   f"打开拐角减速后进给应当有快有慢，实际 {feeds}")
            expect(
                abs(slowed["toolpath"]["statistics"]["cut_length_mm"]
                    - plain["toolpath"]["statistics"]["cut_length_mm"]) < 1e-6,
                "拐角减速不应改变切削长度",
            )

        # Step-down: the planar path repeats per layer, the clearance plane stays absolute.
        status, body = _request(
            base, "/api/plan", {"region": {"shape": "square"}, "planner": {"id": "raster"}}
        )
        steps += 1
        single = json.loads(body) if status == 200 else {}
        status, body = _request(
            base,
            "/api/plan",
            {
                "region": {"shape": "square"},
                "planner": {"id": "raster", "parameters": {"depth_mm": 5.0, "stepdown_mm": 2.0}},
            },
        )
        steps += 1
        if status != 200 or not single:
            problems.append(f"分层的规划失败：{status} {body[:60]}")
        else:
            layered = json.loads(body)
            statistics = layered["toolpath"]["statistics"]
            zs = {point[2] for move in layered["toolpath"]["moves"] for point in move["points"]}
            expect(zs == {0.0, -2.0, -4.0, -5.0, 5.0}, f"分层的 Z 取值不对：{sorted(zs)}")
            expect(
                statistics["pass_count"] == single["toolpath"]["statistics"]["pass_count"] * 4,
                "分层后刀轨数应为单层的四倍"
                f"（{single['toolpath']['statistics']['pass_count']} -> {statistics['pass_count']}）",
            )
            expect(any("分层" in note for note in layered["toolpath"]["notes"]),
                   "分层应在 notes 里说明")
            # The depth map the 3D view colours: present once there is depth, bounded in size, and
            # anchored on the floor the removal measured.
            height_map = layered["removal"]["height_map"]
            if height_map is None:
                problems.append("分层的响应缺少高度图")
            else:
                values = [value for row in height_map["cells"] for value in row
                          if value is not None]
                expect(height_map["rows"] * height_map["cols"] <= 4096,
                       f"高度图没有守住格子上限：{height_map['rows']} x {height_map['cols']}")
                expect(height_map["floor_mm"] == -5.0 and min(values) == -5.0,
                       f"高度图的地面不对：floor {height_map['floor_mm']} min {min(values)}")
            expect(single["removal"]["height_map"] is None,
                   "没有切深的规划不应带高度图")

        status, page = _request(base, "/index.html")
        steps += 1
        expect(status == 200 and "<html" in page.lower(), f"静态首页返回 {status}")

        # DXF import: the drawing comes in, its outlines go back, nothing is stored.
        dxf = "\n".join([
            "0", "SECTION", "2", "ENTITIES", "0", "LWPOLYLINE", "70", "1",
            "10", "0", "20", "0", "10", "40", "20", "0", "10", "40", "20", "40", "10", "0", "20", "40",
            "0", "ENDSEC", "0", "EOF", "",
        ])
        status, body = _request(base, "/api/import/dxf", {"text": dxf})
        steps += 1
        if status != 200:
            problems.append(f"DXF 导入失败：{status} {body[:60]}")
        else:
            outlines = json.loads(body)["outlines"]
            expect(len(outlines) == 1 and outlines[0]["point_count"] == 4,
                   f"DXF 导入的轮廓不对：{outlines}")

            # The closed loop an imported drawing makes possible: its outlines go straight into a
            # plan, and the exported NC summarises the outline instead of printing its points.
            imported = {"region": {"shape": "imported", "points": outlines[0]["points"]}}
            status, body = _request(base, "/api/plan", imported)
            steps += 1
            if status != 200:
                problems.append(f"导入轮廓的规划失败：{status} {body[:60]}")
            else:
                plan = json.loads(body)
                expect(plan["region"]["id"] == "imported"
                       and plan["region"]["label"] == "导入轮廓"
                       and plan["region"]["parameters"]["point_count"] == 4,
                       f"导入轮廓的区域描述不对：{plan['region']['parameters']}")
                expect(len(plan["region"]["boundary"]) == 4, "导入轮廓的边界点数不对")
                expect(plan["coverage"] is not None and plan["removal"] is not None,
                       "导入轮廓没有给出覆盖率与材料切除")
                expect(plan["toolpath"]["statistics"]["pass_count"] > 0, "导入轮廓没有生成刀轨")
                expect(plan["request"]["region"]["parameters"]["points"] == outlines[0]["points"],
                       "导入轮廓的点串没有原样回送给调用方")

            status, nc = _request(base, "/api/export/gcode", imported)
            steps += 1
            expect(status == 200 and "region: imported - 4 points" in nc and "points=" not in nc,
                   f"导出的 NC 头部没有概括导入轮廓：{status}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        ToolpathLabHandler.log_message = original_log

    if problems:
        return False, f"{len(problems)} 处不符：\n" + "\n".join(problems)
    return True, f"{steps} 个请求全部符合预期"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提交前自检（CONTRIBUTING.md）")
    parser.add_argument("--node", help="node 可执行文件路径；默认从 PATH 查找")
    arguments = parser.parse_args(argv)
    node = arguments.node or shutil.which("node")

    checks = (
        ("单元测试", check_unit_tests),
        ("headless 示例", check_headless_example),
        ("前端与桌面壳语法", lambda: check_frontend_syntax(node)),
        ("HTTP 冒烟", check_http_api),
    )
    failures: list[str] = []
    print("提交前自检：")
    for name, check in checks:
        ok, detail = check()
        print(f"  {'通过' if ok else '失败'}  {name} · {detail}")
        if not ok:
            failures.append(name)
    if failures:
        print(f"\n{len(failures)} 项失败：{'、'.join(failures)}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
