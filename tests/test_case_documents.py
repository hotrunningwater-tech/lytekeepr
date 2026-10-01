import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CaseDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.registry_path = self.root / "document-registry" / "registry.json"
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.ocr_root = self.root / "ocr-storage"

        self._write_job(
            "job-p13",
            "P13_French_Relocation_Declaration_and_Temporary_School_Transfer_Request.pdf",
            [
                "Declaration in French about a temporary move to Montreal.",
                "The child should attend school in Montreal for one semester.",
            ],
        )
        self._write_job(
            "job-p14",
            "P14_English_Message_Exhibit_on_Proposed_Montreal_Transfer.pdf",
            [
                "Page 1 opening summary of the proposed Montreal transfer.",
                "Page 27 message discussing the move to Montreal and school transfer logistics.",
            ],
        )
        self.registry_path.write_text(
            json.dumps(
                {
                    "email:msg-1:att-1": {
                        "id": "email:msg-1:att-1",
                        "source": "email",
                        "emailId": "msg-1",
                        "emailSubject": "French declaration draft and 52-page message exhibit for Montreal transfer request",
                        "emailSender": {"name": "Petr Artemov", "email": "hotrunningwater@gmail.com"},
                        "emailDate": "2026-07-31T20:10:43+00:00",
                        "filename": "P13_French_Relocation_Declaration_and_Temporary_School_Transfer_Request.pdf",
                        "ocrJobId": "job-p13",
                        "ocrStatus": "success",
                        "status": "matched_scanned",
                    },
                    "email:msg-1:att-2": {
                        "id": "email:msg-1:att-2",
                        "source": "email",
                        "emailId": "msg-1",
                        "emailSubject": "French declaration draft and 52-page message exhibit for Montreal transfer request",
                        "emailSender": {"name": "Petr Artemov", "email": "hotrunningwater@gmail.com"},
                        "emailDate": "2026-07-31T20:10:43+00:00",
                        "filename": "P14_English_Message_Exhibit_on_Proposed_Montreal_Transfer.pdf",
                        "ocrJobId": "job-p14",
                        "ocrStatus": "success",
                        "status": "matched_scanned",
                    },
                },
                indent=2,
            )
        )

    def _write_job(self, job_id: str, filename: str, page_texts: list[str]) -> None:
        job_dir = self.ocr_root / "jobs" / job_id / "results"
        job_dir.mkdir(parents=True, exist_ok=True)
        original_dir = self.ocr_root / "jobs" / job_id / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        original_path = original_dir / filename
        original_path.write_bytes(b"%PDF-1.4")
        payload = {
            "job_id": job_id,
            "status": "success",
            "original_filename": filename,
            "original_pdf_path": str(original_path),
            "page_count": len(page_texts),
            "full_text": "\n".join(page_texts),
            "pages": [
                {"page_number": index + 1, "text": text}
                for index, text in enumerate(page_texts)
            ],
        }
        (job_dir / "result.json").write_text(json.dumps(payload))

    def test_find_case_document_matches_filename_fragment(self):
        from doc_registry import case_documents

        with self._patched_case_documents(case_documents):
            results = case_documents.find_case_documents(filename="p13")

        self.assertEqual(1, len(results))
        self.assertEqual("job-p13", results[0]["jobId"])

    def test_find_case_document_matches_sender_date_and_theme(self):
        from doc_registry import case_documents

        with self._patched_case_documents(case_documents):
            results = case_documents.find_case_documents(
                query="Montreal transfer request",
                sender="Petr",
                date_from="2026-07-31",
                date_to="2026-07-31",
            )

        self.assertGreaterEqual(len(results), 2)
        self.assertEqual("job-p14", results[0]["jobId"])

    def test_read_case_document_returns_full_ocr_text(self):
        from doc_registry import case_documents

        with self._patched_case_documents(case_documents):
            result = case_documents.read_case_document(job_id="job-p14")

        self.assertEqual("job-p14", result["jobId"])
        self.assertEqual(2, result["pageCount"])
        self.assertIn("Page 27 message", result["fullText"])
        self.assertEqual(2, len(result["pages"]))

    def test_search_case_document_content_returns_page_hits(self):
        from doc_registry import case_documents

        with self._patched_case_documents(case_documents):
            results = case_documents.search_case_document_content("page 27 Montreal", limit=5)

        self.assertEqual(1, len(results))
        self.assertEqual("job-p14", results[0]["jobId"])
        self.assertEqual(2, results[0]["matchingPages"][0]["pageNumber"])

    def _patched_case_documents(self, case_documents):
        return mock.patch.multiple(
            case_documents,
            get_ocr_storage_root=mock.Mock(return_value=self.ocr_root),
            load_registry=mock.Mock(return_value=json.loads(self.registry_path.read_text())),
        )


class AssistantToolDispatchTest(unittest.TestCase):
    def test_run_tool_dispatches_find_case_document(self):
        from assistant import tools

        with mock.patch.object(
            tools,
            "find_case_documents",
            return_value=[{"jobId": "job-p13", "filename": "P13_test.pdf"}],
        ):
            payload = json.loads(
                tools.run_tool(
                    "find_case_document",
                    {"filename": "p13"},
                )
            )

        self.assertEqual(1, payload["count"])
        self.assertEqual("job-p13", payload["results"][0]["jobId"])


class ActiveChatDocumentTest(unittest.TestCase):
    def test_active_preview_document_normalizes_to_chat_payload(self):
        import server

        with mock.patch.object(
            server,
            "read_case_document",
            return_value={
                "jobId": "job-p14",
                "filename": "P14_test.pdf",
                "pageCount": 2,
                "fullText": "full saved text",
                "pages": [
                    {"pageNumber": 1, "text": "page one"},
                    {"pageNumber": 2, "text": "page two"},
                ],
            },
        ):
            payload = server._resolve_chat_document(
                upload_document={"filename": "upload.pdf", "page_count": 1, "full_text": "upload", "pages": []},
                active_preview_job_id="job-p14",
            )

        self.assertEqual("P14_test.pdf", payload["filename"])
        self.assertEqual(2, payload["page_count"])
        self.assertEqual("full saved text", payload["full_text"])
        self.assertEqual(2, len(payload["pages"]))

    def test_active_preview_document_takes_precedence_over_upload_document(self):
        import server

        with mock.patch.object(
            server,
            "_load_saved_preview_chat_document",
            return_value={"filename": "saved.pdf", "page_count": 3, "full_text": "saved", "pages": []},
        ):
            payload = server._resolve_chat_document(
                upload_document={"filename": "upload.pdf", "page_count": 1, "full_text": "upload", "pages": []},
                active_preview_job_id="job-saved",
            )

        self.assertEqual("saved.pdf", payload["filename"])

    def test_upload_document_is_used_when_no_active_preview_document_exists(self):
        import server

        payload = server._resolve_chat_document(
            upload_document={"filename": "upload.pdf", "page_count": 1, "full_text": "upload", "pages": []},
            active_preview_job_id=None,
        )

        self.assertEqual("upload.pdf", payload["filename"])


if __name__ == "__main__":
    unittest.main()
