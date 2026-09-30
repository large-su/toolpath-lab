"""示例脚本：不装包、不设 PYTHONPATH，也要能按 README 的命令直接跑通。"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = Path("examples") / "headless_plan.py"
NC_FILE = REPO_ROOT / "examples" / "toolpath_demo.nc"


class HeadlessExampleTestCase(unittest.TestCase):
    def test_runs_from_repo_root_without_pythonpath(self) -> None:
        NC_FILE.unlink(missing_ok=True)

        # 清掉 PYTHONPATH：否则父进程的导入路径会替脚本兜底，掩盖回归。
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        result = subprocess.run(
            [sys.executable, str(EXAMPLE)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("预计工时", result.stdout)
        self.assertTrue(NC_FILE.is_file(), msg="示例没有写出 NC 文件")
        self.assertIn("G21", NC_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
