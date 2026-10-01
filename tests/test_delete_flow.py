import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant import delete_flow


class DeleteScannedDocumentTest(unittest.TestCase):
    def test_delete_scanned_document_removes_job_folder(self):
        with tempfile.TemporaryDirectory() as tempdir:
            storage_root = Path(tempdir) / "ocr-storage"
            job_dir = storage_root / "jobs" / "job-123"
            results_dir = job_dir / "results"
            original_dir = job_dir / "original"
            results_dir.mkdir(parents=True)
            original_dir.mkdir(parents=True)
            (original_dir / "sample.pdf").write_bytes(b"pdf")
            (results_dir / "result.json").write_text(json.dumps({"original_filename": "sample.pdf"}))

            with (
                mock.patch.object(delete_flow, "get_ocr_storage_root", return_value=storage_root),
                mock.patch.object(delete_flow, "run_check_now", return_value={}),
                mock.patch.object(delete_flow, "_remove_processed_document_source"),
            ):
                result = delete_flow.delete_scanned_document("job-123")

            self.assertEqual("deleted", result["status"])
            self.assertFalse(job_dir.exists())


if __name__ == "__main__":
    unittest.main()
