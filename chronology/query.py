"""
Local search over confirmed chronology events — no LLM calls, pure
filtering over already-extracted data. This is what the AI assistant's
tool wraps (see assistant/tools.py); nothing here knows about OpenAI.
Deliberately searches only status="confirmed" events, not the review
queue — unconfirmed/conflicting events shouldn't be presented to the
user as settled facts via chat.
"""

from . import storage


def search_events(
    query: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    importance: str | None = None,
    limit: int = 15,
) -> list[dict]:
    events = list(storage.load_events().values())
    query_lower = (query or "").strip().lower()

    def matches(event: dict) -> bool:
        if category and event.get("category") != category:
            return False
        if importance and event.get("importance") != importance:
            return False
        event_date = event.get("eventDate")
        if date_from and (not event_date or event_date[:10] < date_from):
            return False
        if date_to and (not event_date or event_date[:10] > date_to):
            return False
        if query_lower:
            haystack = (event.get("title", "") + " " + event.get("description", "")).lower()
            if query_lower not in haystack:
                return False
        return True

    filtered = [e for e in events if matches(e)]
    filtered.sort(key=lambda e: e.get("eventDate") or "9999")
    return filtered[:limit]
