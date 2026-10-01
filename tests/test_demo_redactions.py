from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from demo_state import redactions


class DemoRedactionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.app_dir = Path(self.temp_dir.name)
        self.recordings_dir = self.app_dir / "demo_state" / "recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        (self.recordings_dir / "source.webm").write_bytes(b"demo")

        patchers = [
            mock.patch.object(redactions, "APP_DIR", self.app_dir),
            mock.patch.object(redactions, "RECORDINGS_DIR", self.recordings_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_validate_redaction_request_normalizes_filename_and_entries(self) -> None:
        payload = redactions.validate_redaction_request(
            {
                "source_filename": "source.webm",
                "output_filename": "My Edit",
                "redactions": [
                    {
                        "x": 0.10,
                        "y": 0.20,
                        "width": 0.30,
                        "height": 0.15,
                        "start_time": 1.25,
                        "end_time": 3.75,
                    }
                ],
            }
        )
        self.assertEqual("source.webm", payload["source_filename"])
        self.assertEqual("my-edit.mp4", payload["output_filename"])
        self.assertEqual(1.25, payload["redactions"][0]["start_time"])

    def test_validate_redaction_request_rejects_out_of_bounds_boxes(self) -> None:
        with self.assertRaisesRegex(ValueError, "inside the video bounds"):
            redactions.validate_redaction_request(
                {
                    "source_filename": "source.webm",
                    "redactions": [
                        {
                            "x": 0.80,
                            "y": 0.10,
                            "width": 0.30,
                            "height": 0.15,
                            "start_time": 1,
                            "end_time": 2,
                        }
                    ],
                }
            )

    def test_build_blur_filter_complex_contains_time_windows(self) -> None:
        filter_complex, final_label = redactions.build_blur_filter_complex(
            [
                {
                    "x": 0.10,
                    "y": 0.20,
                    "width": 0.30,
                    "height": 0.15,
                    "start_time": 1.25,
                    "end_time": 3.75,
                },
                {
                    "x": 0.50,
                    "y": 0.40,
                    "width": 0.20,
                    "height": 0.10,
                    "start_time": 4.0,
                    "end_time": 5.0,
                },
            ]
        )
        self.assertIn("boxblur=", filter_complex)
        self.assertIn("between(t,1.25,3.75)", filter_complex)
        self.assertEqual(final_label, "stage1")
        self.assertIn("between(t,4,5)", filter_complex)
        self.assertIn("x='iw*0.1'", filter_complex)

    def test_render_redacted_recording_invokes_ffmpeg_and_returns_saved_file(self) -> None:
        target_path = self.recordings_dir / "edited.mp4"

        def fake_run(cmd, check, capture_output):
            target_path.write_bytes(b"rendered")
            return None

        with mock.patch("demo_state.redactions.subprocess.run", side_effect=fake_run) as mocked_run:
            result = redactions.render_redacted_recording(
                {
                    "source_filename": "source.webm",
                    "output_filename": "edited.mp4",
                    "redactions": [
                        {
                            "x": 0.10,
                            "y": 0.20,
                            "width": 0.30,
                            "height": 0.15,
                            "start_time": 1.25,
                            "end_time": 3.75,
                        }
                    ],
                }
            )

        self.assertTrue(target_path.is_file())
        self.assertEqual("edited.mp4", result["filename"])
        self.assertEqual("demo_state/recordings/edited.mp4", result["relative_path"])
        mocked_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
