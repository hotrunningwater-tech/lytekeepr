"""
Duplicate and conflict detection for candidate chronology events. Pure
functions, no I/O, no LLM calls — operates only on already-extracted event
dicts. No vector database; a straightforward token-overlap + date-proximity
heuristic, same spirit as the local email search.
"""

import re
from datetime import date

TEXT_SIMILARITY_THRESHOLD = 0.35
DATE_PROXIMITY_DAYS = 2


def _normalize(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _token_set(text: str | None) -> set[str]:
    return {t for t in _normalize(text).split() if len(t) > 2}


def _text_similarity(a: dict, b: dict) -> float:
    tokens_a = _token_set(a.get("title", "") + " " + a.get("description", ""))
    tokens_b = _token_set(b.get("title", "") + " " + b.get("description", ""))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _dates_close(d1: str | None, d2: str | None, max_days: int = DATE_PROXIMITY_DAYS) -> bool:
    if not d1 or not d2:
        return False
    try:
        a = date.fromisoformat(d1[:10])
        b = date.fromisoformat(d2[:10])
    except ValueError:
        return False
    return abs((a - b).days) <= max_days


def _has_conflict(candidate: dict, existing: dict) -> bool:
    d1, d2 = candidate.get("eventDate"), existing.get("eventDate")
    if d1 and d2 and d1[:10] != d2[:10]:
        return True

    n1, n2 = candidate.get("natureOfClaim"), existing.get("natureOfClaim")
    if n1 and n2 and n1 != n2 and {n1, n2} & {"confirmed_fact", "disputed"} == {"confirmed_fact", "disputed"}:
        return True

    return False


# Categories where a single exact-date match is, on its own, strong enough
# evidence to treat two events as "the same event" even with low text
# overlap. A case realistically has one hearing/filing/order on a given
# date, so wording differences (terse vs. detailed) shouldn't create
# duplicates. Deliberately excludes categories like "communication" or
# "confirmed_action" where several genuinely distinct things can happen on
# the same day (e.g. five different emails on one date are five different
# events, not one).
EXACT_DATE_MERGE_CATEGORIES = {"court_procedural", "deadline_request"}


def _same_exact_date(d1: str | None, d2: str | None) -> bool:
    return bool(d1) and bool(d2) and d1[:10] == d2[:10]


def find_best_match(candidate: dict, existing_events: list[dict]):
    """Returns (matched_event_or_None, is_conflict: bool). Only compares
    within the same category — a court event and a communication about it
    are related but not "the same event"."""
    best = None
    best_score = 0.0

    category = candidate.get("category")
    exact_date_ok = category in EXACT_DATE_MERGE_CATEGORIES

    for event in existing_events:
        if event.get("category") != category:
            continue

        sim = _text_similarity(candidate, event)
        same_date = _same_exact_date(candidate.get("eventDate"), event.get("eventDate"))

        if exact_date_ok and same_date:
            score = max(sim, 0.4) + 0.3
        elif sim >= TEXT_SIMILARITY_THRESHOLD:
            score = sim + (0.3 if _dates_close(candidate.get("eventDate"), event.get("eventDate")) else 0)
        else:
            continue

        if score > best_score:
            best_score = score
            best = event

    if best is None:
        return None, False

    return best, _has_conflict(candidate, best)
