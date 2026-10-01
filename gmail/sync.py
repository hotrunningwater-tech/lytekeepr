"""
Sync orchestration: scans Gmail for new/changed messages and surfaces
every one (minus Gmail's own Promotions/Social/Updates/Forums category
labels) for review. Only messages the user later approves via
save_candidates() get a permanent record under messages/; everything
else keeps only a minimal scan-state entry.
"""

from dataclasses import replace
from datetime import date, datetime, timezone

from . import client, storage
from .config import GmailSyncConfig
from .errors import GmailHistoryGoneError, GmailSyncError
from .parser import normalize_message
from google_oauth.store import list_token_account_ids, load_active_account_id, set_active_account_id as set_oauth_active_account_id
from profiles.store import attach_gmail_account, get_attached_gmail_account_ids

DECIDED_STATUSES = {"saved", "not_relevant", "dismissed"}

# Gmail's own inbox-tab categories. Excluding these up front (both from the
# search query and, as a safety net, from anything that slips through via
# incremental history diffs) skips the bulk of any mailbox — newsletters,
# social notifications, automated updates — without spending an OpenAI call
# on each one.
EXCLUDED_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}


def _display_fields(message: dict) -> dict:
    """Lightweight, cheap-to-keep fields captured for EVERY scanned message
    regardless of relevance — this is what lets the Inbox view show the
    full mailbox without permanently storing every message's full body."""
    from_obj = message.get("from") or {}
    return {
        "senderName": from_obj.get("name") or from_obj.get("email") or "",
        "senderEmail": from_obj.get("email", ""),
        "subject": message.get("subject") or "(no subject)",
        "receivedAt": message.get("receivedAt", ""),
        "snippet": message.get("snippet", ""),
        "hasAttachments": message.get("hasAttachments", False),
        "unread": message.get("isUnread", False),
        "labelIds": message.get("labelIds", []),
    }


def get_active_account_id() -> str | None:
    active_account_id = load_active_account_id()
    if active_account_id:
        return active_account_id

    attached_ids = set(get_attached_gmail_account_ids())
    local_ids = set(storage.list_account_ids_with_state())
    candidates = sorted((attached_ids | local_ids) or local_ids or attached_ids)
    if not candidates:
        return None

    set_oauth_active_account_id(candidates[0])
    return candidates[0]


def set_active_account_id(account_id: str) -> None:
    set_oauth_active_account_id(account_id)


def _list_full_window_ids(config: GmailSyncConfig) -> list[str]:
    category_exclusions = " ".join(
        f"-category:{cat.removeprefix('CATEGORY_').lower()}" for cat in EXCLUDED_CATEGORIES
    )
    query_parts = ["-in:spam", "-in:trash", category_exclusions]
    if config.sync_since_date:
        # Gmail search wants YYYY/MM/DD, not the ISO YYYY-MM-DD we store/validate.
        query_parts.insert(0, f"after:{config.sync_since_date.replace('-', '/')}")
    elif config.sync_window_days:
        query_parts.insert(0, f"newer_than:{config.sync_window_days}d")
    query = " ".join(part for part in query_parts if part).strip()
    message_ids: list[str] = []
    page_token = None
    while True:
        page = client.list_message_ids(query, page_token=page_token)
        message_ids.extend(m["id"] for m in page.get("messages", []) or [])
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return message_ids


def _list_history_ids(start_history_id: str):
    candidate_ids: set[str] = set()
    deleted_ids: set[str] = set()
    newest_history_id = start_history_id
    page_token = None
    while True:
        page = client.list_history(start_history_id, page_token=page_token)
        newest_history_id = page.get("historyId", newest_history_id)
        for record in page.get("history", []) or []:
            for m in record.get("messagesAdded", []) or []:
                candidate_ids.add(m["message"]["id"])
            for m in record.get("messagesDeleted", []) or []:
                deleted_ids.add(m["message"]["id"])
            for m in record.get("labelsAdded", []) or []:
                candidate_ids.add(m["message"]["id"])
            for m in record.get("labelsRemoved", []) or []:
                candidate_ids.add(m["message"]["id"])
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return candidate_ids, deleted_ids, newest_history_id


def scan_for_candidates(since_date: str | None = None) -> dict:
    """since_date: optional "YYYY-MM-DD" override for this one scan (e.g. from
    a UI date picker) — takes priority over the .env default when given."""
    config = GmailSyncConfig.from_env()
    if since_date:
        date.fromisoformat(since_date)  # fail loudly on a bad date, not deep in a Gmail query
        config = replace(config, sync_since_date=since_date)
    profile = client.get_profile()
    account_id = profile.get("emailAddress")
    if not account_id:
        raise GmailSyncError("Gmail profile did not return an account email address.")
    attach_gmail_account(None, account_id)

    sync_state = storage.load_sync_state(account_id)
    scan_state = storage.load_scan_state(account_id)
    has_local_mailbox_state = bool(
        scan_state
        or storage.load_index(account_id)
        or list(storage.pending_dir(account_id).glob("*.json"))
        or list(storage.messages_dir(account_id).glob("*.json"))
    )

    deleted_ids: set[str] = set()
    fell_back_to_full = False
    history_reason = None

    # Gmail's history-diff API has no date filter of its own — it only
    # reports what changed since a checkpoint, so it silently ignores
    # sync_since_date entirely. If the configured cutoff has changed since
    # the last scan, an incremental diff would keep enforcing the *old*
    # cutoff forever instead of the new one. Only take the cheap
    # incremental path when the since-date hasn't moved; otherwise force a
    # full, date-filtered re-list so the current setting actually governs.
    since_date_unchanged = sync_state.get("syncedSinceDate") == config.sync_since_date

    if sync_state.get("historyId") and has_local_mailbox_state and since_date_unchanged:
        try:
            candidate_ids, deleted_ids, newest_history_id = _list_history_ids(sync_state["historyId"])
        except GmailHistoryGoneError:
            candidate_ids = set(_list_full_window_ids(config))
            newest_history_id = profile.get("historyId", "")
            fell_back_to_full = True
            history_reason = "history_cursor_expired"
    else:
        candidate_ids = set(_list_full_window_ids(config))
        newest_history_id = profile.get("historyId", "")
        if sync_state.get("historyId") and not has_local_mailbox_state:
            fell_back_to_full = True
            history_reason = "local_mailbox_state_missing"
        elif sync_state.get("historyId") and not since_date_unchanged:
            fell_back_to_full = True
            history_reason = "since_date_changed"

    for message_id in deleted_ids:
        storage.delete_message(account_id, message_id)
        storage.remove_index_entry(account_id, message_id)
        storage.delete_pending(account_id, message_id)
        scan_state.pop(message_id, None)
    if deleted_ids:
        storage.save_scan_state(account_id, scan_state)

    # Skip anything already decided and unchanged since — this is what
    # makes repeated scans/refreshes cheap instead of re-classifying
    # everything every time.
    to_scan = [
        mid
        for mid in candidate_ids
        if mid not in deleted_ids and scan_state.get(mid, {}).get("status") not in DECIDED_STATUSES
    ]

    normalized_batch = []
    excluded_category_batch = []
    now = datetime.now(timezone.utc).isoformat()
    total_to_scan = len(to_scan)
    try:
        storage.save_scan_progress(
            account_id,
            {"phase": "fetching", "current": 0, "total": total_to_scan},
        )
        for i, message_id in enumerate(to_scan):
            try:
                raw = client.get_message(message_id)
            except GmailSyncError:
                continue
            normalized = normalize_message(raw, account_id)
            if EXCLUDED_CATEGORIES.intersection(normalized.get("labelIds", [])):
                # Query already excludes these for the full-window path; this
                # only fires for messages surfaced via incremental history diffs.
                excluded_category_batch.append(normalized)
            else:
                normalized_batch.append(normalized)
            storage.save_scan_progress(
                account_id,
                {
                    "phase": "fetching",
                    "current": i + 1,
                    "total": total_to_scan,
                    "receivedAt": normalized.get("receivedAt", ""),
                    "subject": normalized.get("subject", ""),
                },
            )

        for message in excluded_category_batch:
            storage.upsert_scan_entry(
                account_id,
                message["id"],
                {
                    "status": "not_relevant",
                    "isRelevant": False,
                    "reason": "Gmail category (Promotions/Social/Updates/Forums) — skipped without review.",
                    "lastScannedAt": now,
                    "historyId": message.get("historyId", ""),
                    **_display_fields(message),
                },
            )

        # Every new, non-category-excluded message goes straight to
        # pending_review — no relevance classification. This used to run
        # an LLM check for "is this about an active divorce/custody
        # case" (classify_fn, now unused), which is DLYTE-era logic that
        # doesn't fit LyteKeepr's general CRM purpose — it was silently
        # hiding real emails a CRM user would want to see.
        new_pending_ids = []
        for message in normalized_batch:
            mid = message["id"]
            storage.save_pending(account_id, message)
            new_pending_ids.append(mid)
            storage.upsert_scan_entry(
                account_id,
                mid,
                {
                    "status": "pending_review",
                    "isRelevant": True,
                    "reason": "",
                    "lastScannedAt": now,
                    "historyId": message.get("historyId", ""),
                    **_display_fields(message),
                },
            )

        new_sync_state = {
            "accountId": account_id,
            "accountEmail": account_id,
            "lastScanAt": now,
            "historyId": newest_history_id,
            "status": "success",
            "messageCount": len(storage.load_index(account_id)),
            "syncedSinceDate": config.sync_since_date,
        }
        storage.save_sync_state(account_id, new_sync_state)

        return {
            "account_id": account_id,
            "scanned": len(normalized_batch),
            "new_candidates": len(new_pending_ids),
            "total_pending": len(storage.get_pending_review_ids(account_id)),
            "deleted": len(deleted_ids),
            "fell_back_to_full_scan": fell_back_to_full,
            "reason": history_reason,
            "state": new_sync_state,
        }
    finally:
        # Always clear, even on error/exception, so the UI never gets stuck
        # showing a stale "Scanning… N/M" after the request has ended.
        storage.clear_scan_progress(account_id)


def get_pending_candidates(account_id: str) -> list[dict]:
    scan_state = storage.load_scan_state(account_id)
    candidates = []
    for message_id, entry in scan_state.items():
        if entry.get("status") != "pending_review":
            continue
        message = storage.load_pending(account_id, message_id)
        if not message:
            continue
        from_obj = message.get("from") or {}
        candidates.append(
            {
                "id": message_id,
                "senderName": from_obj.get("name") or from_obj.get("email", ""),
                "senderEmail": from_obj.get("email", ""),
                "subject": message.get("subject") or "(no subject)",
                "receivedAt": message.get("receivedAt", ""),
                "preview": message.get("snippet", ""),
                "reason": entry.get("reason", ""),
                "hasAttachments": bool(message.get("attachments")),
            }
        )
    candidates.sort(key=lambda c: c.get("receivedAt", ""), reverse=True)
    return candidates


def save_candidates(account_id: str, approved_ids: list[str], dismissed_ids: list[str] | None = None) -> dict:
    """Approves the given candidate ids into permanent records. Ids in
    dismissed_ids are explicitly rejected. Everything else pending is left
    untouched (still pending_review) — review can happen in multiple stages
    across separate calls; nothing is implicitly dismissed just for not
    being in this particular approved_ids list."""
    scan_state = storage.load_scan_state(account_id)
    approved_set = set(approved_ids)
    dismissed_set = set(dismissed_ids or [])
    now = datetime.now(timezone.utc).isoformat()

    saved = 0
    dismissed = 0
    for message_id in approved_set | dismissed_set:
        current_status = scan_state.get(message_id, {}).get("status")
        if message_id in approved_set:
            if current_status != "pending_review":
                continue
            message = storage.load_pending(account_id, message_id)
            if not message:
                continue
            storage.save_message(account_id, message)
            storage.upsert_index_entry(account_id, message)
            scan_state[message_id] = {**scan_state[message_id], "status": "saved", "lastScannedAt": now}
            saved += 1
        else:
            # Dismiss works from any current status, not just
            # pending_review — this also has to cover un-saving an
            # already-saved message from the main drawer, not just
            # rejecting a still-pending candidate before it's ever saved.
            if current_status == "saved":
                storage.delete_message(account_id, message_id)
                storage.remove_index_entry(account_id, message_id)
            scan_state[message_id] = {**scan_state.get(message_id, {}), "status": "dismissed", "lastScannedAt": now}
            dismissed += 1
        storage.delete_pending(account_id, message_id)

    storage.save_scan_state(account_id, scan_state)

    sync_state = storage.load_sync_state(account_id)
    sync_state["messageCount"] = len(storage.load_index(account_id))
    storage.save_sync_state(account_id, sync_state)

    return {"saved": saved, "dismissed": dismissed, "state": sync_state}
