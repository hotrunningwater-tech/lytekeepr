"""
Runs the actual attachment-download + OCR pipeline for queued documents —
pure orchestration over two existing, untouched pieces: gmail.client (for
the download) and services.ocr.adapter (for OCR). Nothing here duplicates
either's logic; this is the only place that combines them.

This is the one place in the whole feature that actually downloads
attachment bytes — deliberately deferred everywhere else until the user
explicitly confirms via the queue.
"""

from pathlib import Path

from gmail.client import download_attachment_bytes
from services.ocr.adapter import scan_pdf, set_original_filename_label
from services.ocr.doc_convert import DocConversionError, convert_to_pdf_bytes, is_word_document

from . import queue_storage
from .registry import compute_counts
from .scheduler import run_check_now


def run_queued_scans() -> dict:
    queue = queue_storage.load_queue()
    succeeded = []
    failed = []

    for item in queue:
        try:
            file_bytes = download_attachment_bytes(item["emailId"], item["attachmentId"])
        except Exception as exc:  # noqa: BLE001 - surfaced per-item, one failure shouldn't stop the rest
            failed.append({**item, "error": f"Could not download attachment: {exc}"})
            continue

        original_filename = item["filename"]
        scan_filename = original_filename
        is_converted_word_doc = is_word_document(original_filename)

        if is_converted_word_doc:
            try:
                file_bytes = convert_to_pdf_bytes(file_bytes, original_filename)
            except DocConversionError as exc:
                failed.append({**item, "error": str(exc)})
                continue
            # scan_pdf validates by filename extension, not file content —
            # needs a .pdf-named input now that the bytes actually are one.
            scan_filename = f"{Path(original_filename).stem}.pdf"

        result = scan_pdf(file_bytes, scan_filename)
        if result["status"] == "failed":
            failed.append({**item, "error": "; ".join(result.get("errors") or ["OCR failed."])})
        else:
            if is_converted_word_doc:
                # Registry matching keys off the original email attachment's
                # filename (see doc_registry.matcher) — restore the .docx
                # name as the match label without touching the stored PDF
                # file itself, which must keep its real .pdf extension for
                # the document viewer to render it correctly.
                set_original_filename_label(result["job_id"], original_filename)
            succeeded.append({**item, "jobId": result["job_id"], "ocrStatus": result["status"]})

    succeeded_ids = {item["id"] for item in succeeded}
    remaining_queue = queue_storage.remove_from_queue(succeeded_ids)

    entries = run_check_now()  # re-cross-reference now that new OCR jobs exist

    return {
        "succeeded": succeeded,
        "failed": failed,
        "remaining_in_queue": len(remaining_queue),
        "counts": compute_counts(entries),
    }
