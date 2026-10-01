from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from demo_state import recordings


class DemoRecordingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.app_dir = Path(self.temp_dir.name)
        self.recordings_dir = self.app_dir / "demo_state" / "recordings"

        patchers = [
            mock.patch.object(recordings, "APP_DIR", self.app_dir),
            mock.patch.object(recordings, "RECORDINGS_DIR", self.recordings_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sanitize_recording_filename_defaults_extension(self) -> None:
        self.assertEqual("my-demo-name.webm", recordings.sanitize_recording_filename("My Demo Name"))

    def test_sanitize_recording_filename_keeps_supported_mp4_extension(self) -> None:
        self.assertEqual("my-demo-name.mp4", recordings.sanitize_recording_filename("My Demo Name.mp4"))

    def test_save_demo_recording_writes_into_demo_state_recordings(self) -> None:
        result = recordings.save_demo_recording("clip one", b"video-bytes")
        saved_path = self.app_dir / result["relative_path"]
        self.assertTrue(saved_path.is_file())
        self.assertEqual(b"video-bytes", saved_path.read_bytes())
        self.assertEqual("clip-one.webm", result["filename"])

    def test_list_demo_recordings_returns_saved_items_newest_first(self) -> None:
        recordings.save_demo_recording("first", b"111")
        recordings.save_demo_recording("second", b"222")
        recordings.save_demo_recording("third.mp4", b"333")
        items = recordings.list_demo_recordings()
        self.assertEqual("third.mp4", items[0]["filename"])
        self.assertEqual("second.webm", items[1]["filename"])
        self.assertEqual("first.webm", items[2]["filename"])


if __name__ == "__main__":
    unittest.main()
