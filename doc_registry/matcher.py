"""Cross-reference matching logic — pure functions, no I/O. Matches one
email attachment against a pool of locally OCR'd documents, in explicit
priority order rather than a fuzzy score, so results stay easy to reason
about and test.

We never have a hash to compare (the attachment itself is never downloaded
in this phase — only its metadata is known), so matching relies on
filename + size, the strongest signals actually available.
"""


def _normalize_filename(name: str | None) -> str:
    return (name or "").strip().lower()


def match_attachment(attachment: dict, ocr_docs: list[dict], claimed_job_ids: set[str]):
    """Returns (matched_doc_or_None, confidence), confidence one of
    'confirmed' | 'probable' | 'missing'."""
    att_name = _normalize_filename(attachment.get("filename"))
    att_size = attachment.get("size")
    candidates = [d for d in ocr_docs if d["jobId"] not in claimed_job_ids]

    if att_name:
        # Matches against matchFilename — frozen at scan time, never
        # touched by a later rename (see services/ocr/adapter.py) — so
        # renaming a scanned doc for clarity never makes its source email
        # attachment look "unscanned" again.

        # 1) exact filename + exact size — strongest available signal pair.
        for doc in candidates:
            if _normalize_filename(doc.get("matchFilename")) == att_name and att_size is not None and doc.get("size") == att_size:
                return doc, "confirmed"

        # 2) exact filename match, size unknown or mismatched.
        for doc in candidates:
            if _normalize_filename(doc.get("matchFilename")) == att_name:
                return doc, "probable"

        # 3) partial/substring filename match (weak signal, still filename-based).
        for doc in candidates:
            doc_name = _normalize_filename(doc.get("matchFilename"))
            if doc_name and len(att_name) > 4 and (att_name in doc_name or doc_name in att_name):
                return doc, "probable"

    return None, "missing"
