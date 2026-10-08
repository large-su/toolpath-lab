"""Refresh all delivered BAT files and independent Blender ZIPs reproducibly."""
import argparse
import io
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toolpath_lab.export.windows import python_launcher_bat


BLENDER_BATCHES = {
    "01_create_scene.bat": (), "02_render_preview.bat": ("--render", "preview"),
    "03_render_cycles.bat": ("--render", "cycles"),
}


def update(root):
    root = Path(root)
    (root / "启动平台.bat").write_bytes(python_launcher_bat("launch_platform.py", needs_numpy=True))
    (root / "launch_platform.py").write_bytes(Path(__file__).with_name("course_launcher.py").read_bytes())
    # Preserve the old Python filename for users who ran it directly.
    (root / "启动平台.py").write_text("from launch_platform import main\nimport sys\nif __name__ == '__main__':\n    sys.exit(main())\n", encoding="utf-8")
    batches = {name: python_launcher_bat("launch_blender.py", args) for name, args in BLENDER_BATCHES.items()}
    templates = Path(__file__).resolve().parents[1] / "toolpath_lab/export/blender_templates"
    replacements = {**batches, **{name: (templates / name).read_bytes() for name in ("launch_blender.py", "README.txt")}}
    loose = 0
    for name, content in batches.items():
        for target in root.rglob(name):
            target.write_bytes(content)
            loose += 1
    for name in ("launch_blender.py", "README.txt"):
        for target in root.rglob(name):
            if (target.parent / "scene.json").is_file():
                target.write_bytes(replacements[name])
    archives = 0
    for archive in root.rglob("*.zip"):
        with zipfile.ZipFile(archive) as original:
            if "scene.json" not in original.namelist() or "build_scene.py" not in original.namelist():
                continue
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as fixed:
                for member in original.infolist():
                    fixed.writestr(member, replacements.get(member.filename, original.read(member)))
            content = stream.getvalue()
        archive.write_bytes(content)
        archives += 1
    print(f"Updated platform entry point, {loose} Blender BAT files and {archives} independent ZIPs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", help="Extracted course delivery directory")
    update(parser.parse_args().root)
