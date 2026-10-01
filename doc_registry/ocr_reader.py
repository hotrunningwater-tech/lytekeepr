"""Read-only reader over the existing OCR job storage (ocr-storage/jobs/).
Does not touch or modify ocr_service or services/ocr/ — just reads the
result.json files those already write."""

import json
from pathlib import Path

from .config import get_ocr_storage_root


def list_ocr_documents() -> list[dict]:
    jobs_dir = get_ocr_storage_root() / "jobs"
    if not jobs_dir.is_dir():
        return []

    docs = []
    for job_dir in jobs_dir.iterdir():
        result_path = job_dir / "results" / "result.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        size = None
        original_path = data.get("original_pdf_path")
        if original_path:
            p = Path(original_path)
            if p.is_file():
                size = p.stat().st_size

        docs.append(
            {
                "jobId": data.get("job_id", job_dir.name),
                "filename": data.get("original_filename", ""),
                # Frozen at scan time, never touched by a later rename —
                # this is what matcher.py compares against, so renaming a
                # scanned doc never makes its source email look "unscanned"
                # again. Falls back to filename for jobs scanned before
                # this field existed.
                "matchFilename": data.get("matchFilename") or data.get("original_filename", ""),
                "status": data.get("status", "unknown"),
                "pageCount": data.get("page_count", 0),
                "size": size,
                "originalPath": original_path,
            }
        )
    return docs
