"""Build and reopen independent Blender bundles; persist actual verification results."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toolpath_lab.export.blender import blender_bundle
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    parser.add_argument("--output", default="exports/verification")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    standard = {"tool": {"diameter_mm": 6, "length_mm": 30},
                "region": {"shape": "circle", "parameters": {"diameter_mm": 80}},
                "planner": {"id": "spiral", "parameters": {"stepover_mm": 3, "feed_mm_per_min": 800}}}
    cases = [("spiral_inward", standard, {"playback_speed": 10}),
             ("spiral_outward", {**standard, "planner": {"id": "spiral", "parameters": {"radial_direction": "outward"}}}, {"fps": 60}),
             ("small_circle", {**standard, "region": {"shape": "circle", "parameters": {"diameter_mm": 12}}}, {}),
             ("near_fit", {**standard, "tool": {"diameter_mm": 79.8, "length_mm": 30}}, {}),
             ("pitch_6", {**standard, "planner": {"id": "spiral", "parameters": {"stepover_mm": 6}}}, {"playback_speed": 10}),
             ("square_raster", {}, {"playback_speed": 10}),
             ("circle_raster", {**standard, "planner": {"id": "raster"}}, {"playback_speed": 10})]
    summary = []
    for name, payload, options in cases:
        result = execute_plan(PlanRequest.from_payload(payload))
        folder = root / name
        folder.mkdir(exist_ok=True)
        blob = blender_bundle(result.request.tool, result.request.region, result.toolpath, result.timeline.to_payload(), options)
        archive = folder / "bundle.zip"
        archive.write_bytes(blob)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(folder)
        common = [args.blender, "--background", "--python-exit-code", "1"]
        script = str(folder / "build_scene.py")
        scene = str(folder / "machining.blend")
        command = [*common, "--factory-startup", "--python", script, "--", "--data", str(folder / "scene.json"), "--output", scene]
        created = subprocess.run(command, cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace")
        (folder / "build.log").write_text(created.stdout + created.stderr, encoding="utf-8")
        if created.returncode:
            raise RuntimeError(f"{name}: build failed; see {folder / 'build.log'}")
        built = json.loads((folder / "verify.json").read_text(encoding="utf-8"))
        opened = subprocess.run([*common, scene, "--python", script, "--", "--verify-only", "--output", scene],
                                cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace")
        (folder / "reopen.log").write_text(opened.stdout + opened.stderr, encoding="utf-8")
        if opened.returncode:
            raise RuntimeError(f"{name}: reopen failed; see {folder / 'reopen.log'}")
        reopened = json.loads((folder / "verify.json").read_text(encoding="utf-8"))
        row = {"case": name, "build": built, "reopen": reopened}
        summary.append(row)
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: {reopened['max_position_error_mm']:.9f} mm; reopen passed", flush=True)


if __name__ == "__main__":
    main()
