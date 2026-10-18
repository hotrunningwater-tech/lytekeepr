"""Runs scripts/check_opportunities.py in its own process (it sets up its temporary data folder before the app is loaded)."""

import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


class OpportunitiesTest(unittest.TestCase):
    def test_opportunity_rules(self):
        run = subprocess.run([sys.executable, str(APP_DIR / "scripts" / "check_opportunities.py")], cwd=APP_DIR, capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-2000:])
        self.assertIn("OPPORTUNITY CHECK PASSED", run.stdout)


if __name__ == "__main__":
    unittest.main()
