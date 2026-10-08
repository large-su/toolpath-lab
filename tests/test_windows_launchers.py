"""Exercise real cmd parsing, not just Python syntax or generated text."""
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from examples.course_launcher import locate_source
from toolpath_lab.cli import port_is_free
from toolpath_lab.export.windows import python_launcher_bat


class LauncherTests(unittest.TestCase):
    def test_listening_port_is_not_free(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            self.assertFalse(port_is_free("127.0.0.1", listener.getsockname()[1]))

    def test_batch_encoding_and_literal_paths(self):
        content = python_launcher_bat("launch_platform.py", needs_numpy=True)
        content.decode("ascii")
        self.assertEqual(content.count(b"\n"), content.count(b"\r\n"))
        self.assertNotIn(b"\x07", content)
        self.assertIn(br"\anaconda3\python.exe", content)

    def test_reject_batch_injection(self):
        for script, args in (("中文.py", ()), ("../entry.py", ()), ("entry.py", ("&exit",))):
            with self.assertRaises(ValueError):
                python_launcher_bat(script, args)

    def test_source_bootstrap(self):
        with tempfile.TemporaryDirectory(prefix="toolpath-launch-") as folder:
            root = Path(folder)
            with zipfile.ZipFile(root / "toolpath-lab源码.zip", "w") as archive:
                for name in ("toolpath_lab/__init__.py", "toolpath_lab/planning/spiral.py", "toolpath_lab/web/index.html"):
                    archive.writestr("toolpath-lab/" + name, "")
            self.assertEqual(locate_source(root), (root / "toolpath-lab").resolve())
            self.assertEqual(locate_source(root), (root / "toolpath-lab").resolve())

    def test_incomplete_delivery_error(self):
        with tempfile.TemporaryDirectory(prefix="toolpath-launch-") as folder:
            with self.assertRaisesRegex(RuntimeError, "完整交付包"):
                locate_source(Path(folder))

    def test_source_rejects_outside_paths_before_extracting(self):
        with tempfile.TemporaryDirectory(prefix="toolpath-launch-") as folder:
            root = Path(folder)
            with zipfile.ZipFile(root / "toolpath-lab源码.zip", "w") as archive:
                for name in ("toolpath_lab/__init__.py", "toolpath_lab/planning/spiral.py", "toolpath_lab/web/index.html"):
                    archive.writestr("toolpath-lab/" + name, "")
                archive.writestr("toolpath-lab/../outside.txt", "invalid")
            with self.assertRaisesRegex(RuntimeError, "目录结构"):
                locate_source(root)
            self.assertFalse((root / "toolpath-lab").exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_actual_cmd_unicode_directory_and_arguments(self):
        with tempfile.TemporaryDirectory(prefix="数控 启动 (&!) ") as folder:
            root = Path(folder)
            (root / "entry.bat").write_bytes(python_launcher_bat("probe.py", ("--render", "preview")))
            (root / "probe.py").write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "Path('args.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\n", encoding="utf-8")
            env = {**os.environ, "TOOLPATH_LAB_PYTHON": sys.executable, "TOOLPATHLAB_NO_PAUSE": "1"}
            for codepage in (936, 65001):
                # cmd.exe uses its own quote rules, not the C runtime escaping
                # performed by list2cmdline for a list of subprocess arguments.
                command = f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /c chcp {codepage}>nul & call entry.bat "argument with spaces"'
                result = subprocess.run(command,
                                        cwd=root, env=env, input="", capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads((root / "args.json").read_text()),
                                 ["--render", "preview", "argument with spaces"])

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_actual_cmd_preserves_failure_exit(self):
        with tempfile.TemporaryDirectory(prefix="toolpath-launch-") as folder:
            root = Path(folder)
            (root / "entry.bat").write_bytes(python_launcher_bat("failure.py"))
            (root / "failure.py").write_text("raise SystemExit(7)\n", encoding="ascii")
            env = {**os.environ, "TOOLPATH_LAB_PYTHON": sys.executable, "TOOLPATHLAB_NO_PAUSE": "1"}
            result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call entry.bat"],
                                    cwd=root, env=env, input="", capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("Operation failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
