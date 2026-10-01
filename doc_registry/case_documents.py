"""Conversation-oriented access to saved OCR documents already stored in DLYTE.

This layer sits on top of the document registry snapshot plus OCR result files.
It does not call OCR, Gmail, or OpenAI services.
"""

import json

from .config import get_ocr_storage_root
from .storage import load_registry


def _normalize(text: str | None) -> str:
    return (text or "").strip().lower()


def _terms(text: str | None) -> list[str]:
    normalized = _normalize(text)
    return [term for term in normalized.replace("_", " ").replace("-", " ").split() if len(term) >= 2]


def _matches_date(entry_date: str | None, date_from: str | None, date_to: str | None) -> bool:
    if not date_from and not date_to:
        return True
    if not entry_date:
        return False
    day = entry_date[:10]
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def _read_result(job_id: str) -> dict | None:
    result_path = get_ocr_storage_root() / "jobs" / job_id / "results" / "result.json"
    if not result_path.is_file():
        return None
    try:
        return json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _base_record(entry: dict) -> dict:
    sender = entry.get("emailSender") or {}
    return {
        "registryId": entry.get("id"),
        "jobId": entry.get("ocrJobId"),
        "filename": entry.get("filename", ""),
        "emailId": entry.get("emailId"),
        "emailSubject": entry.get("emailSubject"),
        "emailDate": entry.get("emailDate"),
        "emailSender": sender,
        "source": entry.get("source"),
        "status": entry.get("status"),
    }


def _iter_saved_entries() -> list[dict]:
    entries = []
    seen_job_ids: set[str] = set()
    for entry in load_registry().values():
        job_id = entry.get("ocrJobId")
        if not job_id or job_id in seen_job_ids:
            continue
        if entry.get("status") not in {"matched_scanned", "unresolved_local"}:
            continue
        seen_job_ids.add(job_id)
        entries.append(entry)
    return entries


def find_case_documents(
    *,
    query: str | None = None,
    sender: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    filename: str | None = None,
    attachment_type: str | None = None,
    limit: int = 5,
) -> list[dict]:
    query_terms = _terms(query)
    sender_terms = _terms(sender)
    filename_terms = _terms(filename)
    attachment_terms = _terms(attachment_type)
    results = []

    for entry in _iter_saved_entries():
        if not _matches_date(entry.get("emailDate"), date_from, date_to):
            continue

        sender_obj = entry.get("emailSender") or {}
        sender_text = _normalize(f"{sender_obj.get('name', '')} {sender_obj.get('email', '')}")
        filename_text = _normalize(entry.get("filename"))
        subject_text = _normalize(entry.get("emailSubject"))
        combined_text = " ".join(part for part in [filename_text, subject_text, sender_text] if part)

        if sender_terms and not all(term in sender_text for term in sender_terms):
            continue
        if filename_terms and not all(term in filename_text for term in filename_terms):
            continue
        if attachment_terms and not any(term in combined_text for term in attachment_terms):
            continue

        score = 0
        if filename_terms:
            score += 10
        if sender_terms:
            score += 7
        if attachment_terms:
            score += 5
        if date_from or date_to:
            score += 4
        score += sum(3 for term in query_terms if term in filename_text)
        score += sum(2 for term in query_terms if term in subject_text)
        score += sum(1 for term in query_terms if term in sender_text)

        if query_terms and score == 0:
            continue

        result = _base_record(entry)
        result["matchScore"] = score
        results.append(result)

    results.sort(
        key=lambda item: (
            item.get("matchScore", 0),
            item.get("emailDate") or "",
            item.get("filename") or "",
        ),
        reverse=True,
    )
    return results[:limit]


def read_case_document(*, job_id: str | None = None, registry_id: str | None = None) -> dict:
    target = None
    for entry in _iter_saved_entries():
        if job_id and entry.get("ocrJobId") == job_id:
            target = entry
            break
        if registry_id and entry.get("id") == registry_id:
            target = entry
            break

    if not target:
        raise FileNotFoundError("Saved case document not found.")

    result = _read_result(target["ocrJobId"])
    if not result:
        raise FileNotFoundError(f"OCR result not found for saved document {target['ocrJobId']}.")

    payload = _base_record(target)
    payload.update(
        {
            "pageCount": result.get("page_count", 0),
            "fullText": result.get("full_text", ""),
            "pages": [
                {
                    "pageNumber": page.get("page_number"),
                    "text": page.get("text", "") or "",
                }
                for page in (result.get("pages") or [])
            ],
        }
    )
    return payload


def search_case_document_content(query: str, limit: int = 5) -> list[dict]:
    query_terms = _terms(query)
    if not query_terms:
        return []

    results = []
    for entry in _iter_saved_entries():
        result = _read_result(entry["ocrJobId"])
        if not result:
            continue

        matches = []
        for page in result.get("pages") or []:
            text = page.get("text", "") or ""
            lowered = text.lower()
            if not all(term in lowered for term in query_terms):
                continue
            matches.append(
                {
                    "pageNumber": page.get("page_number"),
                    "excerpt": text[:300].strip(),
                }
            )

        if not matches:
            continue

        payload = _base_record(entry)
        payload["matchingPages"] = matches[:5]
        payload["matchScore"] = len(matches)
        results.append(payload)

    results.sort(
        key=lambda item: (
            item.get("matchScore", 0),
            item.get("emailDate") or "",
            item.get("filename") or "",
        ),
        reverse=True,
    )
    return results[:limit]
