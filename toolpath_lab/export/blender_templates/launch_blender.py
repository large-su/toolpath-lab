"""Locate Blender and launch a reproducible scene build without shell interpolation."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def find_blender():
    explicit = os.environ.get("BLENDER_EXE")
    if explicit:
        if not Path(explicit).is_file():
            raise RuntimeError("BLENDER_EXE 指定的 blender.exe 不存在")
        return explicit
    located = shutil.which("blender")
    if located:
        return located
    base = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Blender Foundation"
    choices = list(base.glob("Blender */blender.exe"))
    choices.sort(key=lambda p: tuple(int(x) for x in re.findall(r"\d+", p.parent.name)), reverse=True)
    if choices:
        return str(choices[0])
    raise RuntimeError("找不到 Blender。请设置 BLENDER_EXE 为完整 blender.exe 路径后重试")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", choices=("preview", "cycles"))
    parser.add_argument("--no-open", action="store_true", help="Build without opening the Blender window")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    blender = find_blender()
    command = [blender, "--background", "--factory-startup", "--python-exit-code", "1",
               "--python", str(root / "build_scene.py"), "--", "--data", str(root / "scene.json"),
               "--output", str(root / "machining.blend")]
    if args.render:
        command += ["--render", "video", "--engine",
                    "CYCLES" if args.render == "cycles" else "BLENDER_EEVEE"]
    subprocess.run(command, check=True)
    print("场景与渲染输出位于：", root)
    if not args.render and not args.no_open:
        subprocess.Popen([blender, str(root / "machining.blend")])


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print("操作失败：", error, file=sys.stderr)
        sys.exit(1)
