"""
Backfill + incremental sync for LinkedIn conversations. Writes into the CRM
database (crm/db.py) — one Person + one Channel Account per LinkedIn
contact, one Messages row per message, deduped by (channel,
external_message_id).

THE SEQUENCE (run_backfill / run_incremental_sync) — this is the actual
step-by-step flow, kept here as the single source of truth so diagnosing a
failure means checking activity_log against these named steps, not
re-reading code from scratch each time. Every step below logs one
crm_db.log_activity("linkedin_sync", step, ...) row:

  1. check_backfilled   — incremental sync requires a prior backfill; read
                           sync-state.json.
  2. determine_window   — backfill: fixed N days. incremental: time since
                           last_synced_at, capped at N days.
  3. search_page         — call client.search_messages() per page, estimate
                           each conversation's age from its last_activity_text
                           display string (LinkedIn gives no real timestamp —
                           see _parse_activity_text), keep only ones inside
                           the window. Repeats until pages run out or every
                           parseable age on a page is past the window.
  4. fetch_conversation  — for each kept conversation, client.get_conversation()
                           pulls its full message history.
  5. resolve_contact     — match by profile_slug or name; check
                           excluded_contacts (skip entirely if the user
                           removed them before); decide trusted vs
                           pending_review for a brand-new contact.
  6. write_messages      — insert each message into the CRM, deduped by
                           external_message_id.
  7. save_sync_state     — update last_synced_at (and backfilled_at for a
                           first backfill).
  8. profile_enrichment  — separate pass: look up company for any contact
                           still missing one (see _run_profile_enrichment).

Important limitation: linkedin.messaging.search has no date-range
parameter — it only returns conversations sorted by recency, paginated by
cursor. To backfill "the last N days" we page backward and estimate each
conversation's age from its last_activity_text display string (e.g.
"10:39 PM", "Yesterday", "Monday", "Feb 12"), since the tool gives us no
structured timestamp. This is a best-effort heuristic: unparseable strings
are treated as "keep going" rather than silently dropped, so backfill
errs toward over-fetching, never under-fetching. See _parse_activity_text.
"""

import logging
import re
import threading
import time
from datetime import datetime

from crm import db as crm_db
from . import client, storage

logger = logging.getLogger(__name__)

# Auto-sync only runs during these hours — LinkedIn activity is
# essentially zero overnight, so skipping it cuts total automated
# requests roughly in half for no real loss of freshness.
_AUTO_SYNC_START_HOUR = 8
_AUTO_SYNC_END_HOUR = 21
_AUTO_SYNC_INTERVAL_SECONDS = 60 * 60
_SCHEDULER_CHECK_INTERVAL_SECONDS = 5 * 60

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Safety caps so a parsing miss can never spin forever or hammer the server.
_MAX_PAGES = 20
_PAGE_SIZE = 50
_MAX_MESSAGES_PER_CONVERSATION = 100
_MAX_CONVERSATIONS_PER_BACKFILL = 20
_MAX_PROFILE_LOOKUPS_PER_RUN = 20

# What the UI polls for a live "what's it doing right now" label during a
# backfill/sync call. Single-process Flask dev server (threaded=True), so a
# plain module global is enough — no cross-process state needed.
_current_activity: str | None = None


def _set_activity(text: str | None) -> None:
    global _current_activity
    _current_activity = text


def get_current_activity() -> str | None:
    return _current_activity


def _parse_activity_text(text: str | None, now: datetime) -> datetime | None:
    """Best-effort: resolve a LinkedIn display string ("10:39 PM",
    "Yesterday", "Monday", "Feb 12") to an actual datetime. Recent strings
    resolve to a real time; older ones only give day-level precision (no
    time of day) since that's all LinkedIn's UI shows. Returns None if the
    format isn't recognized — caller treats that as "assume recent"."""
    if not text:
        return None
    t = text.strip().lower()

    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", t)
    if m:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t == "today":
        return now
    if t == "yesterday":
        return (now - _days(1)).replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.match(r"^(\d+)\s*d$", t)
    if m:
        return (now - _days(int(m.group(1)))).replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.match(r"^(\d+)\s*w$", t)
    if m:
        return (now - _days(int(m.group(1)) * 7)).replace(hour=0, minute=0, second=0, microsecond=0)

    if t in _WEEKDAYS:
        target = _WEEKDAYS.index(t)
        delta = (now.weekday() - target) % 7
        delta = delta if delta != 0 else 7  # same weekday name means last week, not today
        return (now - _days(delta)).replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.match(r"^(\d+)\s*mo(nths?)?$", t)
    if m:
        return (now - _days(int(m.group(1)) * 30)).replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.match(r"^(\d+)\s*y(r|ears?)?$", t)
    if m:
        return (now - _days(int(m.group(1)) * 365)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Month + day + explicit year, e.g. "Nov 3, 2024" — checked before the
    # no-year pattern below so the year isn't dropped on the floor.
    m = re.match(r"^([a-z]{3})[a-z]*\s+(\d{1,2}),?\s+(\d{4})$", t)
    if m and m.group(1) in _MONTHS:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        except ValueError:
            return None

    m = re.match(r"^([a-z]{3})[a-z]*\s+(\d{1,2})$", t)
    if m and m.group(1) in _MONTHS:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        try:
            candidate = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        if candidate > now:
            candidate = candidate.replace(year=candidate.year - 1)
        return candidate

    return None  # unrecognized format — caller keeps going rather than guessing wrong


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _collect_recent_conversations(max_age_days: float, run_id: str) -> tuple[list[dict], str | None]:
    """Step 3 (search_page): page through search results until conversations
    are clearly past max_age_days, or a safety cap is hit. Only conversations
    actually within the window are returned — pagination continuing past a
    page doesn't mean everything on every page fetched belongs in the
    result. Returns (conversations, error)."""
    now = datetime.now()
    collected: list[dict] = []
    cursor = None

    for page_num in range(_MAX_PAGES):
        res = client.search_messages(page_size=_PAGE_SIZE, cursor=cursor)
        if not res.get("ok"):
            crm_db.log_activity(
                run_id, 3, "linkedin_sync", "search_page",
                detail=f"page {page_num + 1} failed: {res.get('error', 'search failed')}",
            )
            return collected, res.get("error", "search failed")

        result = res.get("result") or {}
        page = result.get("conversations") or []
        crm_db.log_activity(
            run_id, 3, "linkedin_sync", "search_page",
            detail=f"page {page_num + 1}: {len(page)} conversations returned",
            data={"page_num": page_num + 1, "conversation_names": [c.get("participant_name") for c in page]},
        )

        ages = [_parse_activity_text(c.get("last_activity_text"), now) for c in page]
        # Keep a conversation if it's within the window OR its age couldn't
        # be parsed at all (conservative: never silently drop something we
        # can't confidently place in time). Anything clearly older than
        # max_age_days is excluded here, not just used to decide paging.
        for conv, resolved in zip(page, ages):
            if resolved is None or (now - resolved).total_seconds() / 86400 <= max_age_days:
                collected.append(conv)

        known_ages = [(now - r).total_seconds() / 86400 for r in ages if r is not None]
        # Stop paginating once every conversation we could actually parse on
        # this page is already past the window — unparseable ones don't
        # count against stopping, so we never cut off early on a format we
        # don't recognize.
        if known_ages and all(a > max_age_days for a in known_ages):
            break

        pagination = result.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        cursor = pagination.get("next_cursor")
        if not cursor:
            break
        time.sleep(1)  # space out pagination calls rather than bursting

    crm_db.log_activity(
        run_id, 3, "linkedin_sync", "search_page",
        detail=f"done: {len(collected)} conversation(s) kept inside the {max_age_days}-day window",
        data={"kept_names": [c.get("participant_name") for c in collected]},
    )
    return collected, None


def _ingest_conversation(summary: dict, run_id: str) -> int:
    """Steps 4-6 for one conversation: fetch its messages (step 4), resolve
    who the contact is (step 5), write new messages into the CRM (step 6).
    Returns how many messages were newly added (0 if the conversation
    couldn't be fetched or nothing was new)."""
    ref = summary.get("conversation_ref")
    participant_name = summary.get("participant_name", "Unknown")
    if not ref:
        crm_db.log_activity(run_id, 4, "linkedin_sync", "fetch_conversation", detail=f"{participant_name}: no conversation_ref, skipped")
        return 0

    conv_res = client.get_conversation(ref, max_messages=_MAX_MESSAGES_PER_CONVERSATION)
    time.sleep(1)  # space out the per-conversation calls too
    if not conv_res.get("ok"):
        crm_db.log_activity(run_id, 4, "linkedin_sync", "fetch_conversation", detail=f"{participant_name}: fetch failed: {conv_res.get('error')}")
        return 0
    conv_data = ((conv_res.get("result") or {}).get("conversation")) or {}
    messages = conv_data.get("messages") or []
    crm_db.log_activity(run_id, 4, "linkedin_sync", "fetch_conversation", detail=f"{participant_name}: {len(messages)} message(s) in thread")
    if not messages:
        return 0

    # participant_profile_slug is frequently null in what LinkedIn actually
    # returns. Falling back to conversation_ref would split one real person
    # into a new "person" per thread (every thread has its own ref), so
    # fall back to their display name instead — stable across threads,
    # even though it can't disambiguate two different people sharing a name.
    channel_identifier = summary.get("participant_profile_slug") or f"name:{participant_name}"

    # A contact only earns automatic trust if the user has actually messaged
    # them (an outgoing message present in this pull) — inbound-only senders
    # go to the Inbox tab for review first, since that's exactly the shape
    # of daily spam/ads. Only matters for a brand-new channel_account; an
    # existing one already carries whatever status it has and is never
    # downgraded here (see get_or_create_person_for_channel).
    initial_status = "trusted" if any(m.get("direction") == "outgoing" for m in messages) else "pending_review"

    conn = crm_db.get_connection()
    try:
        if crm_db.is_contact_excluded(conn, "linkedin", channel_identifier):
            crm_db.log_activity(run_id, 5, "linkedin_sync", "resolve_contact", detail=f"{participant_name} ({channel_identifier}): excluded by user — skipped entirely", conn=conn)
            conn.commit()
            return 0  # explicitly removed by the user — never silently re-created
        person_id, account_id = crm_db.get_or_create_person_for_channel(
            conn, "linkedin", channel_identifier, participant_name, contact_status=initial_status
        )
        crm_db.set_unread(conn, account_id, bool(summary.get("unread")))
        crm_db.log_activity(
            run_id, 5, "linkedin_sync", "resolve_contact",
            detail=f"{participant_name} -> person_id={person_id} status={initial_status}",
            conn=conn,
        )
        now = datetime.now()
        added = 0
        for msg in messages:
            resolved = _parse_activity_text(msg.get("sent_at_text"), now)
            timestamp = resolved.isoformat() if resolved else msg.get("sent_at_text")
            sender = msg.get("sender_name") or ("You" if msg.get("direction") == "outgoing" else participant_name)
            was_new = crm_db.insert_message(
                conn,
                person_id=person_id,
                account_id=account_id,
                thread_id=ref,
                channel="linkedin",
                sender=sender,
                direction=msg.get("direction", "incoming"),
                message_text=msg.get("text"),
                timestamp=timestamp,
                external_message_id=msg.get("message_ref"),
            )
            if was_new:
                added += 1
        crm_db.log_activity(run_id, 6, "linkedin_sync", "write_messages", detail=f"{participant_name}: {added} new message(s) written (of {len(messages)} seen)", conn=conn)
        conn.commit()
        return added
    finally:
        conn.close()


def reset() -> dict:
    """Wipes all LinkedIn data from the CRM and resets sync state back to
    "not backfilled" — safe to call as many times as needed for testing;
    it never touches other channels' data."""
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        crm_db.wipe_channel(conn, "linkedin")
        conn.commit()
    finally:
        conn.close()
    storage.save({"backfilled": False, "backfilled_at": None, "last_synced_at": None})
    return {"ok": True}


def _run_profile_enrichment() -> int:
    """For every LinkedIn contact who hasn't been checked for a company yet,
    resolve their profile by name and pull just the company name (nothing
    else from the profile, deliberately kept minimal for now). Marks
    company_checked regardless of outcome so a person with no resolvable
    company/profile is never re-searched on every future run. Returns how
    many people got a company assigned."""
    conn = crm_db.get_connection()
    try:
        people = crm_db.list_people_missing_company(conn, "linkedin")
    finally:
        conn.close()

    people = people[:_MAX_PROFILE_LOOKUPS_PER_RUN]
    assigned = 0
    for i, person in enumerate(people, start=1):
        name = person["name"]
        _set_activity(f"Verifying/extracting profile — {name} ({i}/{len(people)})")
        conn = crm_db.get_connection()
        try:
            search_res = client.search_people(name)
            time.sleep(1)
            candidates = ((search_res.get("result") or {}).get("people")) or [] if search_res.get("ok") else []
            if not candidates:
                crm_db.mark_company_checked(conn, person["person_id"])
                conn.commit()
                continue

            profile_slug = candidates[0].get("profile_slug")
            if not profile_slug:
                crm_db.mark_company_checked(conn, person["person_id"])
                conn.commit()
                continue

            # Save the real profile slug now — independent of whether the
            # company lookup below succeeds, since name-search already
            # resolved it either way. Never overwrites an already-real
            # identifier, only the "name:X" fallback.
            crm_db.upgrade_channel_identifier(conn, person["person_id"], "linkedin", profile_slug)
            conn.commit()

            profile_res = client.get_person(profile_slug)
            time.sleep(1)
            if not profile_res.get("ok"):
                crm_db.mark_company_checked(conn, person["person_id"])
                conn.commit()
                continue

            company_text = ((profile_res.get("result") or {}).get("person") or {}).get("current_company_text")
            if company_text:
                company_id = crm_db.get_or_create_company(conn, company_text)
                crm_db.set_person_company(conn, person["person_id"], company_id)
                assigned += 1
            else:
                crm_db.mark_company_checked(conn, person["person_id"])
            conn.commit()
        finally:
            conn.close()
    return assigned


def run_backfill(max_age_days: float = 7) -> dict:
    run_id = crm_db.new_run_id()
    crm_db.init_db()
    sync_state = storage.load()
    crm_db.log_activity(run_id, 1, "linkedin_sync", "check_backfilled", detail="backfill: no prior-state check needed, this IS the backfill")
    crm_db.log_activity(run_id, 2, "linkedin_sync", "determine_window", detail=f"fixed {max_age_days}-day window (backfill)")
    _set_activity("Pulling messages…")
    try:
        conversations, error = _collect_recent_conversations(max_age_days, run_id)
        if error and not conversations:
            return {"ok": False, "error": error, "run_id": run_id}

        conversations_found = len(conversations)
        capped = conversations_found > _MAX_CONVERSATIONS_PER_BACKFILL
        conversations = conversations[:_MAX_CONVERSATIONS_PER_BACKFILL]
        total_new_messages = sum(_ingest_conversation(summary, run_id) for summary in conversations)

        sync_state["backfilled"] = True
        sync_state["backfilled_at"] = datetime.now().isoformat()
        sync_state["last_synced_at"] = sync_state["backfilled_at"]
        storage.save(sync_state)
        crm_db.log_activity(run_id, 7, "linkedin_sync", "save_sync_state", detail=f"backfilled_at={sync_state['backfilled_at']}")

        companies_assigned = _run_profile_enrichment()
        crm_db.log_activity(run_id, 8, "linkedin_sync", "profile_enrichment", detail=f"{companies_assigned} compan(y/ies) assigned")
        return {
            "ok": True,
            "run_id": run_id,
            "conversations_seen": conversations_found,
            "conversations_ingested": len(conversations),
            "capped": capped,
            "new_messages": total_new_messages,
            "companies_assigned": companies_assigned,
            "warning": error,
        }
    finally:
        _set_activity(None)


def run_incremental_sync(max_age_days: float = 7) -> dict:
    """Page back through search results and store anything newer than the
    last successful sync, instead of only ever checking a fixed-size recent
    page. Meant to be called on a schedule (e.g. hourly) after run_backfill()
    has already run once."""
    run_id = crm_db.new_run_id()
    crm_db.init_db()
    sync_state = storage.load()
    if not sync_state.get("backfilled"):
        crm_db.log_activity(run_id, 1, "linkedin_sync", "check_backfilled", detail="not backfilled yet — aborting")
        return {"ok": False, "error": "run_backfill() has not completed yet", "run_id": run_id}
    crm_db.log_activity(run_id, 1, "linkedin_sync", "check_backfilled", detail="already backfilled, proceeding")

    last_synced_at = sync_state.get("last_synced_at")
    if last_synced_at:
        elapsed_days = (datetime.now() - datetime.fromisoformat(last_synced_at)).total_seconds() / 86400
        window_days = min(max(elapsed_days, 0.0), max_age_days)
    else:
        window_days = max_age_days
    crm_db.log_activity(run_id, 2, "linkedin_sync", "determine_window", detail=f"window={window_days:.3f} days (last_synced_at={last_synced_at})")

    _set_activity("Pulling messages…")
    try:
        conversations, error = _collect_recent_conversations(window_days, run_id)
        if error and not conversations:
            return {"ok": False, "error": error, "run_id": run_id}

        total_new_messages = sum(_ingest_conversation(summary, run_id) for summary in conversations)

        sync_state["last_synced_at"] = datetime.now().isoformat()
        storage.save(sync_state)
        crm_db.log_activity(run_id, 7, "linkedin_sync", "save_sync_state", detail=f"last_synced_at={sync_state['last_synced_at']}")

        companies_assigned = _run_profile_enrichment()
        crm_db.log_activity(run_id, 8, "linkedin_sync", "profile_enrichment", detail=f"{companies_assigned} compan(y/ies) assigned")
        return {
            "ok": True,
            "run_id": run_id,
            "conversations_checked": len(conversations),
            "new_messages": total_new_messages,
            "companies_assigned": companies_assigned,
            "warning": error,
        }
    finally:
        _set_activity(None)


def _scheduler_loop() -> None:
    while True:
        time.sleep(_SCHEDULER_CHECK_INTERVAL_SECONDS)
        try:
            now = datetime.now()
            if not (_AUTO_SYNC_START_HOUR <= now.hour < _AUTO_SYNC_END_HOUR):
                continue

            sync_state = storage.load()
            if not sync_state.get("backfilled"):
                continue  # never auto-backfill; that's an explicit, manual action

            last_synced_at = sync_state.get("last_synced_at")
            if last_synced_at:
                elapsed = (now - datetime.fromisoformat(last_synced_at)).total_seconds()
                if elapsed < _AUTO_SYNC_INTERVAL_SECONDS:
                    continue

            run_incremental_sync()
        except Exception:  # noqa: BLE001 - a scheduler tick failing should never kill the loop
            continue


def start_scheduler() -> None:
    """Starts the hourly (8am-9pm) auto-sync background thread. Safe to
    call once at app startup; does nothing until a backfill has already
    completed at least once."""
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
