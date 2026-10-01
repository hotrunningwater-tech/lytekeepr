"""
Demonstrates that a single PDF can be submitted to ocr_document() and a
structured, page-ordered OCRResult comes back, with everything saved to
a job folder.

Two ways to run it:

1. Automated / offline (no API key or network required):

       python -m unittest ocr_service.tests.test_ocr_service

   Builds a real 2-page PDF in memory (via Pillow), runs it through the
   real PDF-splitting code, and only mocks the network call to the OCR
   provider — so this proves the actual page-splitting/ordering/storage
   pipeline works, into a temporary storage root (the real project
   ocr-storage/ folder is left untouched by this test).

2. Live / manual CLI (requires EDEN_AI_API_KEY in the project .env):

       ./.venv/bin/python -m ocr_service.tests.test_ocr_service path/to/file.pdf

   Calls the real Eden AI endpoint on the given PDF and pretty prints the
   resulting structured OCRResult as JSON. Uses the real default storage
   root (ocr-storage/) so you can inspect the job folder afterward.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from ocr_service import OCRResult, OCRServiceConfig, ocr_document
from ocr_service import provider as provider_module


def _make_test_pdf_bytes(page_count: int = 2) -> bytes:
    pages = [Image.new("RGB", (200, 100), color="white") for _ in range(page_count)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


class OCRServiceOfflineTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.storage_root = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_multi_page_pdf_returns_ordered_structured_result_and_stores_job(self):
        fake_pages_in_order = ["First page text", "Second page text"]
        call_count = {"n": 0}

        def fake_call(*args, **kwargs):
            text = fake_pages_in_order[call_count["n"]]
            call_count["n"] += 1
            return {"google": {"text": text}}

        with mock.patch.object(provider_module, "call_eden_ocr", side_effect=fake_call):
            config = OCRServiceConfig(api_key="fake-key-for-offline-test", storage_root=self.storage_root)
            result = ocr_document(_make_test_pdf_bytes(2), filename="sample.pdf", config=config)

        self.assertIsInstance(result, OCRResult)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.original_filename, "sample.pdf")
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.errors, [])

        # Page order preserved
        self.assertEqual(result.pages[0].text, "First page text")
        self.assertEqual(result.pages[1].text, "Second page text")
        self.assertLess(
            result.full_text.index("First page text"),
            result.full_text.index("Second page text"),
        )

        # Everything actually landed on disk under the job folder
        job_dir = self.storage_root / "jobs" / result.job_id
        self.assertTrue((job_dir / "original" / "sample.pdf").exists())
        self.assertTrue((job_dir / "pages" / "page-001.png").exists())
        self.assertTrue((job_dir / "pages" / "page-002.png").exists())
        self.assertTrue(Path(result.extracted_text_path).exists())
        self.assertTrue(Path(result.json_result_path).exists())

        saved_json = json.loads(Path(result.json_result_path).read_text())
        self.assertEqual(saved_json["job_id"], result.job_id)
        self.assertEqual(saved_json["page_count"], 2)

    def test_non_pdf_file_is_clearly_rejected(self):
        config = OCRServiceConfig(api_key="fake-key-for-offline-test", storage_root=self.storage_root)
        result = ocr_document(b"just some bytes", filename="notes.txt", config=config)

        self.assertEqual(result.status, "failed")
        self.assertTrue(any("Only PDF files are currently supported" in e for e in result.errors))

    def test_missing_api_key_returns_failed_status_not_an_exception(self):
        config = OCRServiceConfig(api_key=None, storage_root=self.storage_root)
        result = ocr_document(_make_test_pdf_bytes(1), filename="sample.pdf", config=config)

        self.assertEqual(result.status, "failed")
        self.assertTrue(any("EDEN_AI_API_KEY" in e for e in result.errors))


def _run_live_cli(path: str) -> None:
    config = OCRServiceConfig.from_env()
    result = ocr_document(path, config=config)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _run_live_cli(sys.argv[1])
    else:
        unittest.main()
