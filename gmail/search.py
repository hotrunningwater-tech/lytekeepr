"""
Local search over synced email records — reads only what's already stored
under email-storage/. Never calls the Gmail API. This is the function the
OpenAI assistant's tool wraps (see assistant/tools.py); nothing here knows
about OpenAI.
"""

import re

from . import storage
from .sync import get_active_account_id


def _matches_date_range(received_at: str, date_from: str | None, date_to: str | None) -> bool:
    if not date_from and not date_to:
        return True
    if not received_at:
        return False
    received_date = received_at[:10]
    if date_from and received_date < date_from:
        return False
    if date_to and received_date > date_to:
        return False
    return True


def _extract_terms(query: str) -> list[str]:
    """Callers (an LLM in particular) may pass search-engine-style queries
    like '"Form 3" OR sworn statement' rather than one plain phrase — treat
    it as a set of OR'd terms instead of one literal substring, so a
    reasonable query still matches something."""
    cleaned = query.replace('"', " ").replace("'", " ")
    cleaned = re.sub(r"\b(OR|AND)\b", " ", cleaned, flags=re.IGNORECASE)
    terms = [t.strip().lower() for t in re.split(r"[,\s]+", cleaned) if len(t.strip()) >= 2]
    return terms or [query.strip().lower()]


def _text_contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _index_text_match(entry: dict, terms: list[str]) -> bool:
    haystack = " ".join(
        [
            entry.get("subject", ""),
            entry.get("snippet", ""),
            entry.get("from", {}).get("name", ""),
            entry.get("from", {}).get("email", ""),
            " ".join(t.get("email", "") for t in entry.get("to", [])),
        ]
    ).lower()
    return _text_contains_any(haystack, terms)


def search_synced_emails(
    query: str | None = None,
    sender: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    labels: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    account_id = get_active_account_id()
    if not account_id:
        return []

    index = storage.load_index(account_id)
    entries = list(index.values())

    sender_lower = (sender or "").strip().lower()
    labels_upper = {l.upper() for l in (labels or [])}

    candidates = []
    for entry in entries:
        if sender_lower:
            from_obj = entry.get("from", {}) or {}
            from_text = f"{from_obj.get('name', '')} {from_obj.get('email', '')}".lower()
            if sender_lower not in from_text:
                continue
        if labels_upper and not labels_upper.intersection(entry.get("labelIds", [])):
            continue
        if not _matches_date_range(entry.get("receivedAt", ""), date_from, date_to):
            continue
        candidates.append(entry)

    query_stripped = (query or "").strip()
    if not query_stripped:
        matched = candidates
    else:
        terms = _extract_terms(query_stripped)
        matched = [e for e in candidates if _index_text_match(e, terms)]
        if len(matched) < limit:
            # Index-only fields (subject/snippet/sender) missed the query —
            # fall back to scanning full message bodies for remaining candidates.
            already = {e["id"] for e in matched}
            scanned = 0
            for entry in candidates:
                if entry["id"] in already:
                    continue
                message = storage.load_message(account_id, entry["id"])
                body_text = ((message or {}).get("textBody", "") + " " + (message or {}).get("htmlBody", "")).lower()
                if _text_contains_any(body_text, terms):
                    matched.append(entry)
                scanned += 1
                if scanned >= limit * 5:  # cap body-scan work for this MVP
                    break

    matched.sort(key=lambda e: e.get("receivedAt", ""), reverse=True)
    matched = matched[:limit]

    results = []
    for entry in matched:
        message = storage.load_message(account_id, entry["id"]) or {}
        body = message.get("textBody") or entry.get("snippet", "")
        results.append(
            {
                "id": entry["id"],
                "threadId": entry.get("threadId", ""),
                "from": entry.get("from", {}),
                "subject": entry.get("subject", ""),
                "receivedAt": entry.get("receivedAt", ""),
                "snippet": entry.get("snippet", ""),
                "bodyExcerpt": body[:800],
                "labelIds": entry.get("labelIds", []),
            }
        )
    return results
