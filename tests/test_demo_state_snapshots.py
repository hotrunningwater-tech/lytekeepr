from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from demo_state import snapshots


class DemoStateSnapshotsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.app_dir = Path(self.temp_dir.name)
        self.snapshot_root = self.app_dir / "demo-snapshots"
        self.demo_state_dir = self.app_dir / "demo_state"

        for relative in ("chronology-data", "document-registry", "email-storage", "ocr-storage", "demo_state"):
            path = self.app_dir / relative
            path.mkdir(parents=True, exist_ok=True)
            (path / ".keep").write_text(relative)

        patchers = [
            mock.patch.object(snapshots, "APP_DIR", self.app_dir),
            mock.patch.object(snapshots, "SNAPSHOT_ROOT", self.snapshot_root),
            mock.patch.object(
                snapshots,
                "SNAPSHOT_TARGETS",
                ("chronology-data", "document-registry", "email-storage", "ocr-storage", "demo_state"),
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_active_state(self, *, active: str | None = None, baseline: str | None = None) -> None:
        payload = {}
        if active is not None:
            payload["activeSnapshot"] = active
        if baseline is not None:
            payload["baselineSnapshot"] = baseline
        (self.demo_state_dir / "demo-state-manager.json").write_text(json.dumps(payload, indent=2))

    def test_capture_sets_active_snapshot_and_includes_metadata(self) -> None:
        self._write_active_state(baseline="zero")

        snapshot = snapshots.capture_snapshot("clip_10", description="Before recording ten")

        self.assertEqual("clip_10", snapshot["name"])
        self.assertEqual("Before recording ten", snapshot["description"])
        state = json.loads((self.demo_state_dir / "demo-state-manager.json").read_text())
        self.assertEqual("clip_10", state["activeSnapshot"])
        self.assertEqual("zero", state["baselineSnapshot"])
        manifest = json.loads((self.snapshot_root / "clip_10" / "manifest.json").read_text())
        self.assertEqual("clip_10", manifest["name"])
        self.assertEqual("zero", manifest["baselineSnapshot"])
        self.assertIsNone(manifest["parentSnapshot"])

    def test_restore_rolls_visible_snapshot_list_back_to_restored_head(self) -> None:
        snapshots.capture_snapshot("zero", is_baseline=True)
        snapshots.capture_snapshot("mid")
        snapshots.capture_snapshot("late")

        visible_before = [item["name"] for item in snapshots.list_snapshots()]
        self.assertEqual(["late", "mid", "zero"], visible_before)

        snapshots.restore_snapshot("mid")

        visible_after = [item["name"] for item in snapshots.list_snapshots()]
        self.assertEqual(["mid", "zero"], visible_after)
        state = json.loads((self.demo_state_dir / "demo-state-manager.json").read_text())
        self.assertEqual("mid", state["activeSnapshot"])
        self.assertEqual("zero", state["baselineSnapshot"])

    def test_reset_to_baseline_restores_baseline_snapshot(self) -> None:
        (self.app_dir / "chronology-data" / "timeline.txt").write_text("baseline")
        snapshots.capture_snapshot("zero", is_baseline=True)

        (self.app_dir / "chronology-data" / "timeline.txt").write_text("changed")
        snapshots.capture_snapshot("mid")
        (self.app_dir / "chronology-data" / "timeline.txt").write_text("broken")

        result = snapshots.reset_to_baseline()

        self.assertEqual("zero", result["name"])
        self.assertEqual("baseline", (self.app_dir / "chronology-data" / "timeline.txt").read_text())
        state = json.loads((self.demo_state_dir / "demo-state-manager.json").read_text())
        self.assertEqual("zero", state["activeSnapshot"])


if __name__ == "__main__":
    unittest.main()
