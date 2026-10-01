"""
Runs chronology extraction over approved emails only,
de-duplicates/conflict-checks new candidates against what's already known,
and splits results into auto-confirmed events vs. the review queue.

extract_email_batch_fn / extract_document_fn are injected callables (see
assistant.chronology_extractor) — this module has no OpenAI knowledge.
Only processes sources not already recorded in extraction-state.json, so
re-running never reprocesses or duplicates a source.
"""

import uuid
from datetime import datetime, timezone

from gmail.storage import load_all_messages
from gmail.sync import get_active_account_id

from . import storage
from .dedup import find_best_match
from .verbatim import fix_excerpt


def _is_strong(event: dict) -> bool:
    return event.get("confidence") == "high" and event.get("natureOfClaim") == "confirmed_fact"


def _source_ref(source_type: str, source_id: str, source_label: str, page_number, excerpt: str) -> dict:
    return {
        "sourceType": source_type,
        "sourceId": source_id,
        "sourceLabel": source_label,
        "pageNumber": page_number,
        "excerpt": excerpt,
    }


def _normalize_candidate(raw: dict) -> dict:
    return {
        "title": raw.get("title") or "Untitled event",
        "description": raw.get("description") or "",
        "category": raw.get("category") or "case_fact",
        "eventDate": raw.get("eventDate") or None,
        "dateRangeFrom": raw.get("dateRangeFrom") or None,
        "dateRangeTo": raw.get("dateRangeTo") or None,
        "recordedAt": raw.get("recordedAt") or None,
        "involvedParties": raw.get("involvedParties") or [],
        "natureOfClaim": raw.get("natureOfClaim") or "alleged",
        "importance": raw.get("importance") or "medium",
        "confidence": raw.get("confidence") or "medium",
    }


def _mark_processed_source(processed_sources: dict, source_type: str, source_id: str, source_label: str, processed_at: str) -> None:
    key = f"{source_type}:{source_id}"
    processed_sources[key] = {
        "sourceType": source_type,
        "sourceId": source_id,
        "sourceLabel": source_label,
        "processedAt": processed_at,
    }


def run_extraction(extract_email_batch_fn, extract_document_fn, email_batch_size: int = 8) -> dict:
    account_id = get_active_account_id()
    emails = load_all_messages(account_id) if account_id else []

    extraction_state = storage.load_extraction_state()
    processed_sources = storage.load_processed_sources()
    processed_source_ids = set(processed_sources.keys())
    now = datetime.now(timezone.utc).isoformat()

    new_emails = [e for e in emails if f"email:{e['id']}" not in processed_source_ids]

    raw_candidates: list[tuple[dict, dict]] = []  # (normalized_event, source_ref)

    for i in range(0, len(new_emails), email_batch_size):
        batch = new_emails[i : i + email_batch_size]
        results = extract_email_batch_fn(batch)
        for email in batch:
            source_text = email.get("textBody") or email.get("snippet") or ""
            for raw_event in results.get(email["id"], []):
                candidate = _normalize_candidate(raw_event)
                verified_excerpt = fix_excerpt(raw_event.get("excerpt", ""), source_text)
                ref = _source_ref(
                    "email", email["id"], email.get("subject") or "(no subject)", None, verified_excerpt
                )
                raw_candidates.append((candidate, ref))
            _mark_processed_source(
                processed_sources,
                "email",
                email["id"],
                email.get("subject") or "(no subject)",
                now,
            )

    events = storage.load_events()
    review_queue = storage.load_review_queue()
    existing_all = list(events.values()) + list(review_queue.values())

    created_confirmed = 0
    created_review = 0
    merged_into_existing = 0

    for candidate, ref in raw_candidates:
        matched, conflict = find_best_match(candidate, existing_all)

        if matched and not conflict:
            already_has_source = any(
                s["sourceType"] == ref["sourceType"] and s["sourceId"] == ref["sourceId"] for s in matched["sources"]
            )
            if not already_has_source:
                matched["sources"].append(ref)
            matched["updatedAt"] = now
            merged_into_existing += 1
            continue

        event_id = str(uuid.uuid4())
        record = {
            "id": event_id,
            **candidate,
            "sources": [ref],
            "conflictsWith": [],
            "status": "pending_review",
            "reviewReason": "",
            "createdAt": now,
            "updatedAt": now,
        }

        if conflict and matched:
            record["reviewReason"] = "Conflicts with an existing event (differing date or completion status)."
            record["conflictsWith"] = [matched["id"]]
            if matched.get("status") != "confirmed":
                matched.setdefault("conflictsWith", []).append(event_id)
                matched["reviewReason"] = "Conflicts with a newly found event (differing date or completion status)."
                matched["updatedAt"] = now
                review_queue[matched["id"]] = matched
            review_queue[event_id] = record
            existing_all.append(record)
            created_review += 1
        elif _is_strong(record):
            record["status"] = "confirmed"
            events[event_id] = record
            existing_all.append(record)
            created_confirmed += 1
        else:
            record["reviewReason"] = "Below auto-confirm threshold (confidence or claim nature not both strong)."
            review_queue[event_id] = record
            existing_all.append(record)
            created_review += 1

    storage.save_events(events)
    storage.save_review_queue(review_queue)
    storage.save_extraction_state(
        {
            "processedSourceIds": sorted(processed_sources.keys()),
            "processedSources": processed_sources,
            "lastRunAt": now,
        }
    )

    return {
        "sourcesProcessed": len(new_emails),
        "candidatesFound": len(raw_candidates),
        "mergedIntoExisting": merged_into_existing,
        "eventsCreatedConfirmed": created_confirmed,
        "eventsCreatedReview": created_review,
        "totalConfirmed": len(events),
        "totalReview": len(review_queue),
        "newEmailsProcessed": len(new_emails),
        "newDocumentsProcessed": 0,
    }
