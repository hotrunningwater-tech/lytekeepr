"""
Full-text search over locally OCR'd document content (ocr-storage/jobs/).
Pure local read — no OCR calls, no LLM calls. This is what the AI
assistant's document-search tool wraps (see assistant/tools.py).
"""

import json

from .config import get_ocr_storage_root


def _excerpt(text: str, query_lower: str, context: int = 150) -> str:
    idx = text.lower().find(query_lower)
    if idx == -1:
        return text[:context]
    start = max(0, idx - context // 2)
    end = min(len(text), idx + len(query_lower) + context // 2)
    return text[start:end].strip()


def search_documents(query: str, limit: int = 5) -> list[dict]:
    query_lower = (query or "").strip().lower()
    if not query_lower:
        return []

    jobs_dir = get_ocr_storage_root() / "jobs"
    if not jobs_dir.is_dir():
        return []

    results = []
    for job_dir in jobs_dir.iterdir():
        result_path = job_dir / "results" / "result.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        matching_pages = []
        for page in data.get("pages", []) or []:
            text = page.get("text", "") or ""
            if query_lower in text.lower():
                matching_pages.append(
                    {"pageNumber": page.get("page_number"), "excerpt": _excerpt(text, query_lower)}
                )

        if matching_pages:
            results.append(
                {
                    "jobId": data.get("job_id", job_dir.name),
                    "filename": data.get("original_filename", ""),
                    "matchingPages": matching_pages[:5],
                }
            )
        if len(results) >= limit:
            break

    return results
