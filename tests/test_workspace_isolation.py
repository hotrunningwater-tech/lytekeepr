"""Runs scripts/check_isolation.py in its own process (it must set up its temporary data folder before the app is loaded)."""

import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


class WorkspaceIsolationTest(unittest.TestCase):
    def test_no_workspace_sees_or_changes_another(self):
        run = subprocess.run([sys.executable, str(APP_DIR / "scripts" / "check_isolation.py")], cwd=APP_DIR, capture_output=True, text=True, timeout=900)
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-2000:])
        self.assertIn("ISOLATION CHECK PASSED", run.stdout)


if __name__ == "__main__":
    unittest.main()
