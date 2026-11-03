"""Saving LinkedIn conversations into the CRM (crm/db.py): one Person and one Channel Account per LinkedIn contact, one Messages row per
message, deduped by (channel, external_message_id). The reading happens on the USER's own computer, in the LyteKeepr LinkedIn add-on
(lk_calendar/linkedin_helper/agent.py), which hands each conversation to lk_calendar/linkedin_link.py; nothing here talks to LinkedIn.

A brand-new contact is trusted only if the user wrote to them (an outgoing message in the thread); inbound-only senders wait for review
(the LinkedIn tab's Inbox, or Claude's linkedin_inbox). A contact the user removed is never brought back (excluded_contacts).

LinkedIn gives no real timestamps, only display text ("10:39 PM", "Yesterday", "Monday", "Feb 12"): _parse_activity_text turns that into
a date when the add-on has not already done so."""

from datetime import datetime

from crm import db as crm_db

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

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


def save_conversation(summary: dict, messages: list[dict], run_id: str) -> int:
    """Steps 5-6 for one conversation already read from LinkedIn (here, or on
    the user's own computer by the LinkedIn helper). A message may carry
    sent_at, its time already worked out where it was read."""
    ref = summary.get("conversation_ref")
    participant_name = summary.get("participant_name", "Unknown")

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
        if channel_identifier.startswith("name:"):
            # No profile link came with this thread: someone imported with a link is the same person if the name is unique.
            same_name = conn.execute(
                "SELECT channel_identifier FROM channel_accounts WHERE channel = 'linkedin' AND lower(display_name) = lower(?)",
                (participant_name,),
            ).fetchall()
            if len(same_name) == 1:
                channel_identifier = same_name[0]["channel_identifier"]
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
            timestamp = msg.get("sent_at") or (resolved.isoformat() if resolved else msg.get("sent_at_text"))
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
