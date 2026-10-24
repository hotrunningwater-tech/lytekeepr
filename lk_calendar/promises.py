"""Follow-up dates from what was actually said.

After new emails (or LinkedIn messages) arrive for an outreach lead, the newest message decides:
- A reply from them, while we were still waiting (new / sent / other "cold" statuses), switches the lead to "replied".
- If that newest message, whoever wrote it, promises a time for the next contact ("I'll get back to you next week",
  "let's talk in November", "Monday works"), the follow-up moves to that date. The AI only finds the words; the date is
  calculated here, in code, from the day the message was sent. No promise: the usual rule for the status stays.
Since 2026-10-24 the promised time is read by the scheduled Claude run instead (inbox_watch: save_message_review follow_ups), on the
user's own Claude plan; the sync hook here only does the plain-code part (no_reader). read_promise stays for tests and manual use.
Each message is looked at once (lead_message_seen), so the AI is asked only about new messages.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta

from . import leads, store

log = logging.getLogger(__name__)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_message_seen (
    lead_id INTEGER NOT NULL, message_key TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (lead_id, message_key)
);
"""


# ------------------------------------------------------------------ the date, from the words (code, never the AI)
def _workday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7) if d.weekday() >= 5 else d       # Saturday/Sunday -> Monday


def promised_date(p: dict, sent: date) -> date | None:
    """{"when": ..., "n": ..., "weekday": ..., "month": ..., "date": ...} said on `sent` -> the follow-up day, or None."""
    when = (p or {}).get("when") or "none"
    n = int(p.get("n") or 0) if str(p.get("n") or "0").lstrip("-").isdigit() else 0
    if when == "tomorrow":
        return _workday(sent + timedelta(days=1))
    if when == "days" and 0 < n <= 90:
        return _workday(sent + timedelta(days=n))
    if when == "weeks" and 0 < n <= 26:
        return _workday(sent + timedelta(weeks=n))
    if when == "next_week":                                   # Monday of the following week
        return sent + timedelta(days=7 - sent.weekday())
    if when == "end_of_week":                                 # Friday of this week (next week's if already past Friday)
        return sent + timedelta(days=(4 - sent.weekday()) % 7 or (7 if sent.weekday() > 4 else 0))
    if when == "weekday" and (p.get("weekday") or "").lower() in WEEKDAYS:
        target = WEEKDAYS.index(p["weekday"].lower())
        return sent + timedelta(days=(target - sent.weekday()) % 7 or 7)              # the next one after that day
    if when == "next_month":
        first = (sent.replace(day=1) + timedelta(days=32)).replace(day=1)
        return _workday(first)
    if when == "month" and (p.get("month") or "").lower() in MONTHS:
        m = MONTHS.index(p["month"].lower()) + 1
        year = sent.year + (1 if m <= sent.month else 0)
        return _workday(date(year, m, 1))
    if when == "date":
        try:
            d = date.fromisoformat(str(p.get("date") or ""))
        except ValueError:
            return None
        return d if sent < d <= sent + timedelta(days=366) else None
    return None


# ------------------------------------------------------------------ the words, from the message (the AI, asked one thing)
PROMPT = """You read one message between a salesperson and a prospect. Find ONLY a promise or request about WHEN they will next
be in touch or meet (for example "I'll get back to you next week", "let's talk in November", "call me Monday", "in two weeks",
"by Friday", "tomorrow"). Ignore every other date (dates of past events, deadlines unrelated to the next contact, signatures).

Answer with JSON only:
{"when": "none" | "tomorrow" | "days" | "weeks" | "next_week" | "end_of_week" | "weekday" | "next_month" | "month" | "date",
 "n": number of days or weeks (only for days/weeks), "weekday": "monday".."sunday" (only for weekday),
 "month": "january".."december" (only for month), "date": "YYYY-MM-DD" (only for an exact calendar date),
 "quote": "the exact words that say it"}
Use "none" when no such timing is said. The message was sent on {sent} ({weekday})."""


def read_promise(text: str, sent: date) -> dict:
    from assistant.config import AssistantConfig
    from services.openai.client import generate_reply
    config = AssistantConfig.from_env()
    if not config.api_key or not (text or "").strip():
        return {"when": "none"}
    raw = generate_reply([{"role": "system", "content": PROMPT.replace("{sent}", sent.isoformat()).replace("{weekday}", WEEKDAYS[sent.weekday()].title())},
                          {"role": "user", "content": text[:6000]}], api_key=config.api_key, model=config.model)
    try:
        return json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except (AttributeError, ValueError):
        return {"when": "none"}


# ------------------------------------------------------------------ looking at each lead's newest message
def _messages(person_id: int, channel: str) -> list:
    from crm import db as crm_db
    from . import contacts
    with contacts._crm() as conn:
        detail = crm_db.get_person_detail(conn, person_id) or {}
    msgs = contacts.messages_with_saved_emails(person_id, detail.get("messages") or [])
    return [m for m in msgs if (m.get("channel") or "").lower() == channel and m.get("timestamp")]


def _when(ts: str) -> datetime | None:
    from . import contacts
    d = contacts._when(ts)
    if d is None:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(store.TZ).replace(tzinfo=None)
    return d


def review_leads(channels=("email", "linkedin"), reader=read_promise) -> dict:
    """Look at every open lead's newest message once. Returns what changed (for the log and tests)."""
    out = {"replied": [], "dated": [], "looked_at": 0}
    today = store.now_local().date()
    with store._conn() as conn:
        conn.executescript(SCHEMA)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM leads WHERE person_id IS NOT NULL AND channel IN ({','.join('?' for _ in channels)})", channels)]
        buckets = store.bucket_map(conn)
    for lead in rows:
        if buckets.get((lead["channel"], lead["status"]), "none") == "closed":
            continue                                           # finished: not interested, done, bounced
        try:
            msgs = _messages(lead["person_id"], lead["channel"])
        except Exception:
            log.exception("could not read messages for lead %s", lead["id"])
            continue
        if not msgs:
            continue
        newest = msgs[-1]
        key = str(newest.get("message_id") or newest.get("id") or newest.get("timestamp"))
        with store._conn() as conn:
            conn.executescript(SCHEMA)
            if conn.execute("SELECT 1 FROM lead_message_seen WHERE lead_id = ? AND message_key = ?", (lead["id"], key)).fetchone():
                continue
            conn.execute("INSERT OR IGNORE INTO lead_message_seen VALUES (?, ?, ?)", (lead["id"], key, store.now_local().isoformat()))
        out["looked_at"] += 1
        sent_at = _when(newest["timestamp"])
        if not sent_at:
            continue
        incoming = newest.get("direction") == "incoming"
        bucket = buckets.get((lead["channel"], lead["status"]), "none")
        if incoming and (lead["status"] == "new" or bucket == "cold") and sent_at.date() >= date.fromisoformat(lead["status_date"] or "2000-01-01"):
            leads.set_status(lead["id"], "replied")
            out["replied"].append(lead["contact"])
        try:
            promise = reader(newest.get("message_text") or "", sent_at.date())
        except Exception:
            log.exception("could not read the timing in a message for lead %s", lead["id"])
            continue
        day = promised_date(promise, sent_at.date())
        if day and day >= today:
            leads.set_follow_up(lead["id"], day.isoformat())
            who = "They" if incoming else "You"
            quote = str(promise.get("quote") or "").strip()[:200]
            with store._conn() as conn:                          # say why, on the follow-up itself
                conn.execute("UPDATE events SET notes = ? WHERE lead_ref = ? AND source = 'auto'",
                             (f"{who} said: “{quote}” ({sent_at.date().strftime('%b %-d')})" if quote else f"{who} gave a time ({sent_at.date().strftime('%b %-d')})", f"lead:{lead['id']}"))
            out["dated"].append({"contact": lead["contact"], "date": day.isoformat(), "quote": quote})
    if out["replied"] or out["dated"]:
        log.info("follow-ups from messages: %s", out)
    return out


def no_reader(text: str, sent: date) -> dict:
    """Promised times are now read by the scheduled Claude run (inbox_watch), on the user's own plan, not by our paid AI here."""
    return {"when": "none"}


def review_quietly() -> None:
    """For hooks after new mail or messages arrive: never let a problem here break the sync that called it.
    Only the plain-code part runs here (a reply switches the lead to "replied"); the promised times come from the Claude run."""
    try:
        review_leads(reader=no_reader)
    except Exception:
        log.exception("reviewing follow-up promises failed; it will run again after the next sync")
