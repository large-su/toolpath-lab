"""Create demo bundles and measured strategy data without running the HTTP server."""
import argparse
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toolpath_lab.export.blender import blender_bundle
from toolpath_lab.export import toolpath_to_gcode
from toolpath_lab.server.comparison import compare_strategies, comparison_csv
from toolpath_lab.server.schema import PlanRequest
from toolpath_lab.server.service import execute_plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="exports/demo")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    payload = {"tool": {"diameter_mm": 6, "length_mm": 30},
               "region": {"shape": "circle", "parameters": {"diameter_mm": 80}},
               "planner": {"id": "spiral", "parameters": {"stepover_mm": 3,
                           "feed_mm_per_min": 800, "sample_step_mm": 0.5}}}
    request = PlanRequest.from_payload(payload)
    result = execute_plan(request)
    comparison = compare_strategies(request)
    (root / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "comparison.csv").write_text(comparison_csv(comparison), encoding="utf-8")
    (root / "spiral.nc").write_text(toolpath_to_gcode(result.toolpath), encoding="utf-8")
    (root / "plan.json").write_text(json.dumps(result.to_payload(), ensure_ascii=False), encoding="utf-8")
    options = {"playback_speed": 10, "samples": 64}
    for name, case in (("spiral", payload), ("square_raster", {}),
                       ("circle_raster", {**payload, "planner": {"id": "raster", "parameters": {"stepover_mm": 3, "feed_mm_per_min": 800}}})):
        out = execute_plan(PlanRequest.from_payload(case))
        blob = blender_bundle(out.request.tool, out.request.region, out.toolpath, out.timeline.to_payload(), options)
        archive = root / (name + "_blender.zip")
        archive.write_bytes(blob)
        folder = root / name
        folder.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(folder)
    print(json.dumps(comparison["rows"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
