"""Calls recorded by Fathom: their transcripts land on the contacts who were on the call, in date order with their emails.

Each workspace's admin pastes their own Fathom API key (Settings > Calls). LyteKeepr then fetches every recorded call (hourly, and
on demand) with its transcript, keeps it in that workspace's CRM database, and links it to:
  - the contacts on the call (by the invitees' email addresses), and
  - the meeting on the calendar (by time), so a meeting with no recording can be flagged "No-show?".
Nothing is summarized automatically: the user presses Summarize. Fathom's free API is enough (keys, meetings, transcripts).
"""

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import store

log = logging.getLogger(__name__)
FATHOM_API = "https://api.fathom.ai/external/v1"
MAX_TRANSCRIPT_CHARS_FOR_AI = 60000
NO_SHOW_AFTER_HOURS = 2            # a call meeting this long past its end with no recording is asked about

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    duration_min INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT '',
    share_url TEXT NOT NULL DEFAULT '',
    invitees TEXT NOT NULL DEFAULT '[]',
    transcript TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    event_id INTEGER,
    fetched_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS call_people (
    call_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    PRIMARY KEY (call_id, person_id)
);
"""
STATUSES = ("ready", "done", "no_show")


class CallsError(Exception):
    pass


@contextmanager
def _crm():
    """This workspace's CRM database, with the calls tables in place."""
    from . import contacts
    with contacts._crm() as conn:
        conn.executescript(SCHEMA)
        if "owner_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}:
            conn.execute("ALTER TABLE calls ADD COLUMN owner_id INTEGER")      # whose Fathom recorded it: NULL = the admin's
        yield conn


# ------------------------------------------------------------------ whose Fathom: the admin's, or a team member's own
def _me():
    """The team member working now (their own Fathom key and calls), or None for the admin. Same rule as their own Google Calendar."""
    from .google_sync import member_id
    return member_id()


def _state(name: str) -> str:
    from .google_sync import _key
    return _key(name)


def _member_ids() -> set:
    with store._conn() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM users WHERE role != 'admin'")}


def _mine_sql(alias: str = "") -> tuple[str, list]:
    """SQL condition for calls recorded by the current person's own Fathom."""
    col = f"{alias}owner_id"
    me = _me()
    return (f"{col} = ?", [me]) if me else (f"{col} IS NULL", [])


def _visible_sql(alias: str = "") -> tuple[str, list]:
    """SQL condition for the calls this person may see: everyone's for the admin and "Everything", else only their own."""
    from . import access
    return ("1 = 1", []) if access.is_full() else _mine_sql(alias)


# ------------------------------------------------------------------ the Fathom key (the admin's per workspace, plus each team member's own)
def _key_path():
    from profiles.store import profile_component_dir
    me = _me()
    if me:
        folder = profile_component_dir("fathom") / "members"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{int(me)}.json"
    return profile_component_dir("fathom") / "key.json"


def get_key() -> str:
    try:
        return json.loads(_key_path().read_text()).get("api_key", "")
    except (OSError, ValueError):
        return ""


def save_key(api_key: str) -> None:
    api_key = (api_key or "").strip()
    if not api_key:
        raise CallsError("paste your Fathom API key")
    _fathom_get("/meetings", {}, api_key)          # refuse a key Fathom does not accept
    path = _key_path()
    path.write_text(json.dumps({"api_key": api_key, "saved_at": store.now_local().isoformat()}))
    os.chmod(path, 0o600)


def remove_key() -> None:
    try:
        _key_path().unlink()
    except FileNotFoundError:
        pass


def settings() -> dict:
    key = get_key()
    return {"connected": bool(key), "key_end": key[-4:] if key else "", "last_sync": store.get_state(_state("fathom_last_sync")) or "",
            "last_error": store.get_state(_state("fathom_last_error")) or "", "member": bool(_me())}


def _fathom_get(path: str, params: dict, api_key: str | None = None) -> dict:
    key = api_key or get_key()
    if not key:
        raise CallsError("Fathom is not connected")
    query = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(f"{FATHOM_API}{path}" + (f"?{query}" if query else ""), headers={"X-Api-Key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CallsError("Fathom did not accept this API key") from exc
        if exc.code == 429:
            raise CallsError("Fathom is busy (too many requests); it will try again later") from exc
        raise CallsError(f"Fathom answered with an error ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CallsError(f"could not reach Fathom: {exc}") from exc


# ------------------------------------------------------------------ fetching calls
def _iso(text: str) -> str:
    """Fathom's UTC time as this workspace's local time (YYYY-MM-DDTHH:MM)."""
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(store.TZ).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


def _platform(url: str) -> str:
    u = (url or "").lower()
    return "Zoom" if "zoom." in u else "Google Meet" if "meet.google" in u else "Teams" if "teams." in u else ""


def sync() -> dict:
    """Fetch calls recorded since the last fetch (first time: the last 30 days) and store them. Safe to run any time."""
    if not get_key():
        return {"status": "not_connected"}
    last = store.get_state(_state("fathom_last_sync_utc"))
    try:                                              # look back a few hours each time: a call Fathom listed before its transcript was ready gets it now
        since = (datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ") if last else ""
    except ValueError:
        since = ""
    since = since or (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = updated = 0
    cursor, pages = None, 0
    try:
        while True:
            params = {"include_transcript": "true", "created_after": since}
            if cursor:
                params["cursor"] = cursor
            page = _fathom_get("/meetings", params)
            for item in page.get("items") or []:
                new = _store_call(item)
                added += 1 if new else 0
                updated += 0 if new else 1
            cursor, pages = page.get("next_cursor"), pages + 1
            if not cursor or pages >= 20:
                break
    except CallsError as exc:
        store.set_state(_state("fathom_last_error"), str(exc))
        raise
    store.set_state(_state("fathom_last_sync_utc"), started)
    store.set_state(_state("fathom_last_sync"), store.now_local().isoformat())
    store.set_state(_state("fathom_last_error"), "")
    return {"status": "ok", "added": added, "updated": updated}


def _store_call(item: dict) -> bool:
    rid = str(item.get("recording_id") or "")
    if not rid:
        return False
    start = _iso(item.get("recording_start_time") or item.get("scheduled_start_time") or "")
    end = _iso(item.get("recording_end_time") or item.get("scheduled_end_time") or "")
    minutes = 0
    if start and end:
        minutes = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60))
    invitees = [{"name": i.get("name") or "", "email": (i.get("email") or "").lower(), "external": bool(i.get("is_external"))}
                for i in (item.get("calendar_invitees") or [])]
    transcript = [{"speaker": (t.get("speaker") or {}).get("display_name") or "", "email": ((t.get("speaker") or {}).get("matched_calendar_invitee_email") or "").lower(),
                   "at": t.get("timestamp") or "", "text": t.get("text") or ""} for t in (item.get("transcript") or [])]
    fields = {"title": (item.get("title") or item.get("meeting_title") or "Call")[:200], "started_at": start, "ended_at": end, "duration_min": minutes,
              "platform": _platform(item.get("meeting_url") or ""), "share_url": item.get("share_url") or item.get("url") or "",
              "invitees": json.dumps(invitees), "transcript": json.dumps(transcript), "fetched_at": store.now_local().isoformat()}
    with _crm() as conn:
        row = conn.execute("SELECT call_id FROM calls WHERE recording_id = ?", (rid,)).fetchone()
        if row:
            conn.execute(f"UPDATE calls SET {', '.join(k + ' = :' + k for k in fields)} WHERE call_id = :id", {**fields, "id": row["call_id"]})
            call_id, new = row["call_id"], False
        else:
            call_id = conn.execute(f"INSERT INTO calls (recording_id, owner_id, {', '.join(fields)}) VALUES (:rid, :owner, {', '.join(':' + k for k in fields)})",
                                   {**fields, "rid": rid, "owner": _me()}).lastrowid
            new = True
        _link_people(conn, call_id, invitees)
    _link_event(call_id, start, end)
    if new:                                         # a recording arrived, so the meeting happened: mark it completed (unless already set)
        with _crm() as conn:
            event_id = conn.execute("SELECT event_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()["event_id"]
        event = store.get_event(event_id) if event_id else None
        if event and not event.get("meeting_status"):
            store.set_meeting(event_id, meeting_status="completed")
    return new


def _link_people(conn, call_id: int, invitees: list) -> None:
    """The contacts on the call: anyone invited whose email belongs to a contact (any capitalisation)."""
    for inv in invitees:
        if not inv["email"]:
            continue
        for r in conn.execute("SELECT DISTINCT person_id FROM channel_accounts WHERE channel = 'email' AND lower(channel_identifier) = ?", (inv["email"],)):
            conn.execute("INSERT OR IGNORE INTO call_people (call_id, person_id) VALUES (?, ?)", (call_id, r["person_id"]))


def _link_event(call_id: int, start: str, end: str) -> None:
    """The calendar meeting this recording belongs to: a call meeting starting within 45 minutes of the recording."""
    if not start:
        return
    s = datetime.fromisoformat(start)
    me, members = _me(), _member_ids()
    with store._conn() as cal:
        best = None
        for e in cal.execute("SELECT id, start_date, start_time, owner_id FROM events WHERE (has_call = 1 OR kind = 'meeting') AND start_date BETWEEN ? AND ?",
                             ((s - timedelta(days=1)).date().isoformat(), (s + timedelta(days=1)).date().isoformat())):
            if (e["owner_id"] != me) if me else (e["owner_id"] in members):    # a recording belongs to a meeting in the same person's calendar
                continue
            gap = abs((datetime.fromisoformat(f"{e['start_date']}T{e['start_time']}") - s).total_seconds())
            if gap <= 45 * 60 and (best is None or gap < best[0]):
                best = (gap, e["id"])
    if best:
        with _crm() as conn:
            conn.execute("UPDATE calls SET event_id = ? WHERE call_id = ?", (best[1], call_id))


# ------------------------------------------------------------------ reading
def _public(row, with_transcript: bool = False) -> dict:
    out = {k: row[k] for k in ("call_id", "recording_id", "title", "started_at", "ended_at", "duration_min", "platform", "share_url", "summary", "status", "event_id")}
    out["invitees"] = json.loads(row["invitees"] or "[]")
    out["has_transcript"] = row["transcript"] not in ("", "[]")
    if with_transcript:
        out["transcript"] = json.loads(row["transcript"] or "[]")
    return out


def _catch_up(conn, person_id: int) -> None:
    """Calls that arrived before this contact existed (or before it had this email): link them now, by invitee email."""
    emails = {r["e"] for r in conn.execute("""SELECT lower(channel_identifier) AS e FROM channel_accounts
                                              WHERE person_id = ? AND channel = 'email' AND channel_identifier != ''""", (person_id,))}
    if not emails:
        return
    for r in conn.execute("""SELECT call_id, invitees FROM calls WHERE call_id NOT IN (SELECT call_id FROM call_people WHERE person_id = ?)""",
                          (person_id,)).fetchall():      # (linking is not showing: who may see each call is decided when reading)
        if emails & {(i.get("email") or "").lower() for i in json.loads(r["invitees"] or "[]")}:
            conn.execute("INSERT OR IGNORE INTO call_people (call_id, person_id) VALUES (?, ?)", (r["call_id"], person_id))


def calls_for_person(person_id: int) -> list[dict]:
    with _crm() as conn:
        _catch_up(conn, person_id)
        cond, args = _visible_sql("c.")
        rows = conn.execute(f"""SELECT c.* FROM calls c JOIN call_people p ON p.call_id = c.call_id WHERE p.person_id = ? AND {cond}
                                ORDER BY c.started_at DESC""", (person_id, *args)).fetchall()
        people = _people_on(conn, [r["call_id"] for r in rows])
    return [{**_public(r, with_transcript=True), "contacts": people.get(r["call_id"], [])} for r in rows]


def no_shows_for_person(person_id: int) -> list[dict]:
    """Meetings this contact was invited to that were marked no-show and have no recording, shaped like call rows for the timeline."""
    with _crm() as conn:
        emails = {r["e"] for r in conn.execute("""SELECT lower(channel_identifier) AS e FROM channel_accounts
                                                  WHERE person_id = ? AND channel = 'email' AND channel_identifier != ''""", (person_id,))}
        linked = {r["event_id"] for r in conn.execute("SELECT event_id FROM calls WHERE event_id IS NOT NULL")}
    if not emails:
        return []
    out = []
    with store._conn() as cal:
        from . import access
        for e in cal.execute("SELECT * FROM events WHERE meeting_status = 'no_show' AND attendees != '[]' ORDER BY start_date DESC, start_time DESC"):
            invited = json.loads(e["attendees"] or "[]")
            if e["id"] in linked or not emails & {(a.get("email") or "").lower() for a in invited} or not access.can_event(dict(e)):
                continue
            out.append({"call_id": None, "event_id": e["id"], "title": e["title"], "started_at": f"{e['start_date']}T{e['start_time']}",
                        "duration_min": e["duration_min"], "platform": "", "share_url": "", "summary": "", "status": "no_show",
                        "invitees": invited, "has_transcript": False, "transcript": [], "contacts": [], "meeting_only": True})
    return out


def _people_on(conn, call_ids: list) -> dict:
    if not call_ids:
        return {}
    marks = ",".join("?" for _ in call_ids)
    out: dict = {}
    for r in conn.execute(f"""SELECT cp.call_id, p.person_id, p.name FROM call_people cp JOIN people p ON p.person_id = cp.person_id
                              WHERE cp.call_id IN ({marks})""", call_ids):
        out.setdefault(r["call_id"], []).append({"person_id": r["person_id"], "name": r["name"]})
    return out


def get_call(call_id: int) -> dict | None:
    """One call, or None when it does not exist or this person may not see it."""
    with _crm() as conn:
        cond, args = _visible_sql()
        row = conn.execute(f"SELECT * FROM calls WHERE call_id = ? AND {cond}", (call_id, *args)).fetchone()
        if not row:
            return None
        people = _people_on(conn, [call_id])
    return {**_public(row, with_transcript=True), "contacts": people.get(call_id, [])}


def call_for_event(event_id: int) -> dict | None:
    with _crm() as conn:
        cond, args = _visible_sql()
        row = conn.execute(f"SELECT call_id FROM calls WHERE event_id = ? AND {cond} ORDER BY call_id DESC LIMIT 1", (event_id, *args)).fetchone()
    return get_call(row["call_id"]) if row else None


def transcript_text(call: dict, limit: int = MAX_TRANSCRIPT_CHARS_FOR_AI) -> str:
    lines = [f"[{t['at']}] {t['speaker'] or 'Someone'}: {t['text']}" for t in call.get("transcript") or []]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n[... transcript cut here ...]"


def event_attendees(event_id: int) -> list[dict]:
    """Who was invited to a calendar meeting, each matched to a contact when their email belongs to one."""
    with store._conn() as cal:
        row = cal.execute("SELECT attendees FROM events WHERE id = ?", (event_id,)).fetchone()
    people = json.loads(row["attendees"] or "[]") if row else []
    out = []
    with _crm() as conn:
        for a in people:
            match = conn.execute("""SELECT p.person_id, p.name, f.name AS funnel, s.name AS stage FROM channel_accounts ca JOIN people p ON p.person_id = ca.person_id
                                    LEFT JOIN funnels f ON f.funnel_id = p.funnel_id LEFT JOIN funnel_stages s ON s.stage_id = p.funnel_stage_id
                                    WHERE ca.channel = 'email' AND lower(ca.channel_identifier) = ? LIMIT 1""", (a["email"],)).fetchone()
            out.append({**a, "person_id": match["person_id"] if match else None, "contact_name": match["name"] if match else "",
                        "funnel": (match["funnel"] or "") if match else "", "stage": (match["stage"] or "") if match else ""})
    return out


def parse_invitees(text: str) -> tuple[list, list]:
    """'Peter, sara@x.com' -> ([emails], [names not found]). A name is looked up among contacts that have an email."""
    emails, unknown = [], []
    for part in [p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()]:
        found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", part)
        if found:
            emails += [f.lower() for f in found]
            continue
        from assistant.contact_lookup import find_people
        from . import access
        people = find_people(part, email_only=True)
        allowed = access.person_ids()                  # a team member resolves only names they may see
        if allowed is not None:
            people = [p for p in people if p["person_id"] in allowed]
        if len(people) == 1:
            emails.append(people[0]["email"].lower())
        else:
            unknown.append(part)
    return list(dict.fromkeys(emails)), unknown


# ------------------------------------------------------------------ actions
def set_status(call_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise CallsError("status must be ready, done or no_show")
    call = get_call(call_id)
    if not call:
        raise CallsError("no such call")
    with _crm() as conn:
        conn.execute("UPDATE calls SET status = ? WHERE call_id = ?", (status, call_id))
    if call["event_id"] and status in ("done", "no_show"):
        store.set_meeting(call["event_id"], meeting_status="completed" if status == "done" else "no_show")   # the calendar follows
    return get_call(call_id)


def summarize(call_id: int) -> dict:
    """Summarize the transcript (on request), save the summary and mark the call done."""
    call = get_call(call_id)
    if not call:
        raise CallsError("no such call")
    if not call["has_transcript"]:
        raise CallsError("this call has no transcript yet")
    from assistant.config import AssistantConfig
    from services.openai.client import generate_reply
    config = AssistantConfig.from_env()
    system = ("Summarize this sales or client call for the person who held it. Plain language, short. Format: one sentence on what the call "
              "was about; then 'Key points:' with 3 to 6 numbered points; then 'Next steps:' with who does what and by when, only if said. "
              "Use only what is in the transcript; never invent anything.")
    text = generate_reply([{"role": "system", "content": system},
                           {"role": "user", "content": f"Call: {call['title']} ({call['started_at']})\n\n{transcript_text(call)}"}],
                          api_key=config.api_key, model=config.model)
    with _crm() as conn:
        conn.execute("UPDATE calls SET summary = ? WHERE call_id = ?", (text.strip(), call_id))
    return set_status(call_id, "done")


def draft_followup(call_id: int, person_id: int | None = None) -> dict:
    """Write the follow-up email from the call and put it on screen (the same draft card as any email: nothing is sent)."""
    call = get_call(call_id)
    if not call:
        raise CallsError("no such call")
    from . import users
    if not users.is_admin():                        # the draft is sent from the workspace's Gmail, which is the admin's
        raise CallsError("Follow-up emails go out from the admin's Gmail, so for now only the admin can draft them. Copy the summary instead.")
    from . import contacts
    target = None
    for c in call["contacts"]:
        if person_id in (None, c["person_id"]):
            info = contacts.get_contact(c["person_id"]) or {}
            if info.get("email"):
                target = info
                break
    if not target:
        raise CallsError("no contact with an email address was on this call")
    from assistant import email_send_flow
    from assistant.config import AssistantConfig
    from services.openai.client import generate_reply
    config = AssistantConfig.from_env()
    basis = call["summary"] or transcript_text(call, 30000)
    system = ("Write a short follow-up email after a call. Thank them briefly, restate what was agreed and the next steps in plain words, "
              "nothing invented. No subject line in the body. End with 'Best,' and the sender's name placeholder [Your Name]. "
              "Answer as JSON: {\"subject\": \"...\", \"body\": \"...\"}.")
    raw = generate_reply([{"role": "system", "content": system},
                          {"role": "user", "content": f"Call '{call['title']}' on {call['started_at']} with {target.get('name') or target['email']}.\n\n{basis}"}],
                         api_key=config.api_key, model=config.model)
    try:
        draft = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except (AttributeError, ValueError):
        draft = {"subject": f"Following up on our call", "body": raw}
    return email_send_flow.create_pending_send(person_name=target["email"], subject=draft.get("subject") or "Following up on our call",
                                               body=draft.get("body") or raw)


# ------------------------------------------------------------------ what needs attention
def attention(days: int = 14) -> dict:
    """For Home: calls with a transcript nobody has summarized yet, and call meetings that passed with no recording (no-show?)."""
    since = (store.now_local() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    cond, args = _mine_sql()                        # Home asks each person only about their own calls
    with _crm() as conn:
        ready = [dict(r) for r in conn.execute(f"""SELECT call_id, title, started_at, duration_min, invitees, summary FROM calls
                                                    WHERE status = 'ready' AND transcript != '[]' AND started_at >= ? AND {cond}
                                                    ORDER BY started_at DESC""", (since, *args))]
        for r in ready:
            r["invitees"] = json.loads(r["invitees"] or "[]")
        people = _people_on(conn, [r["call_id"] for r in ready])
        for r in ready:                             # a contact added since the call arrived: link it now, by email
            if not people.get(r["call_id"]):
                _link_people(conn, r["call_id"], r["invitees"])
        people = _people_on(conn, [r["call_id"] for r in ready])
        linked = {r["event_id"] for r in conn.execute("SELECT event_id FROM calls WHERE event_id IS NOT NULL")}
    for r in ready:
        r["contacts"] = people.get(r["call_id"], [])
    missing = []
    if get_key():                                   # without Fathom there is nothing to compare with
        cutoff = store.now_local() - timedelta(hours=NO_SHOW_AFTER_HOURS)
        try:                                        # only meetings after Fathom was connected: older ones were never going to be recorded
            connected = json.loads(_key_path().read_text()).get("saved_at", "")[:16]
        except (OSError, ValueError):
            connected = ""
        me, members = _me(), _member_ids()
        with store._conn() as cal:
            for e in cal.execute("""SELECT * FROM events WHERE (has_call = 1 OR expect_recording = 1) AND meeting_status = ''
                                    AND start_date BETWEEN ? AND ?""", (since[:10], store.now_local().date().isoformat())):
                if (e["owner_id"] != me) if me else (e["owner_id"] in members):      # only this person's own meetings
                    continue
                end = datetime.fromisoformat(f"{e['start_date']}T{e['start_time']}") + timedelta(minutes=e["duration_min"])
                starts = f"{e['start_date']}T{e['start_time']}"
                if end <= cutoff and e["id"] not in linked and starts >= connected:
                    missing.append({"event_id": e["id"], "title": e["title"], "date": e["start_date"], "time": e["start_time"]})
    from . import autopilot                         # follow-up drafts the scheduled Claude run left: on their call, or on their own
    waiting = autopilot.drafts()
    by_call = {d["ref"]: d for d in waiting if d["ref"].startswith("call:")}
    for r in ready:
        d = by_call.get(f"call:{r['call_id']}")
        r["draft"] = {"draft_id": d["draft_id"], "to": d["to_email"], "subject": d["subject"]} if d else None
    others = [{"draft_id": d["draft_id"], "to": d["to_email"], "subject": d["subject"], "title": d["title"], "kind": d["kind"]}
              for d in waiting if not d["ref"].startswith("call:")]
    return {"ready": ready, "missing": missing, "drafts": others}


# ------------------------------------------------------------------ fetching: right after a call ends, and hourly, in every workspace with Fathom
_started = False
QUICK_MINUTES = 2              # a calendar call just ended and its recording hasn't arrived: check this often
QUICK_FOR_MINUTES = 60         # ... for up to an hour after it ended
HOURLY_MINUTES = 60            # otherwise (e.g. calls that weren't in the calendar)


def waiting_for_recording() -> bool:
    """Did a call in the calendar end within the last hour without its recording here yet?"""
    now = store.now_local()
    with _crm() as conn:
        linked = {r[0] for r in conn.execute("SELECT event_id FROM calls WHERE event_id IS NOT NULL")}
    with store._conn() as cal:
        rows = cal.execute("""SELECT id, start_date, start_time, duration_min FROM events WHERE (has_call = 1 OR expect_recording = 1)
                              AND meeting_status IN ('', 'completed') AND start_date BETWEEN ? AND ?""",
                           ((now - timedelta(days=1)).date().isoformat(), now.date().isoformat())).fetchall()
    for e in rows:
        if e["id"] in linked:
            continue
        end = datetime.fromisoformat(f"{e['start_date']}T{e['start_time']}") + timedelta(minutes=e["duration_min"] or 30)
        if end <= now <= end + timedelta(minutes=QUICK_FOR_MINUTES):
            return True
    return False


def _tick() -> str:
    """Fetch now if a call just ended and is waiting for its recording (every few minutes), or if an hour has passed."""
    now = store.now_local()
    last = store.get_state("fathom_last_pull") or ""
    try:
        since = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else 10 ** 6
    except ValueError:
        since = 10 ** 6
    due = since >= HOURLY_MINUTES or (since >= QUICK_MINUTES and waiting_for_recording())
    if not due:
        return "not due"
    store.set_state("fathom_last_pull", now.isoformat())
    return sync_everyone()


def sync_everyone() -> str:
    """This workspace's admin Fathom, then each active team member's own."""
    from .google_sync import as_member
    out = sync()["status"] if get_key() else "not connected"
    for user_id in sorted(_member_ids()):
        with as_member(user_id):
            if get_key():
                try:
                    sync()
                except CallsError as exc:
                    log.warning("Fathom fetch for team member %s failed: %s", user_id, exc)
    return out


def _loop() -> None:
    from . import directory
    time.sleep(90)
    while True:
        try:
            directory.run_in_each_workspace(_tick, "Fathom calls")
        except Exception:
            log.exception("Fathom fetch failed; it will try again shortly")
        time.sleep(60)


def start() -> None:
    global _started
    if _started or os.getenv("LK_FATHOM_AUTOSYNC", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, name="fathom-calls", daemon=True).start()


# ------------------------------------------------------------------ routes
def register(app):
    from functools import wraps
    from flask import jsonify, request

    def guarded(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except CallsError as exc:
                return jsonify({"error": str(exc)}), 400
            except Exception as exc:                       # e.g. the AI service: say so plainly instead of a bare 500
                log.exception("calls request failed")
                return jsonify({"error": f"Something went wrong: {exc}"}), 502
        return inner

    @app.get("/api/calls/settings")
    @guarded
    def calls_settings():
        return jsonify(settings())

    @app.put("/api/calls/settings")
    @guarded
    def calls_settings_save():
        save_key(str((request.get_json(silent=True) or {}).get("api_key", "")))
        result = sync()
        return jsonify({**settings(), "sync": result})

    @app.delete("/api/calls/settings")
    @guarded
    def calls_settings_remove():
        remove_key()
        return jsonify(settings())

    @app.post("/api/calls/sync")
    @guarded
    def calls_sync():
        return jsonify(sync())

    @app.get("/api/calls/attention")
    @guarded
    def calls_attention():
        return jsonify(attention())

    @app.get("/api/calls/person/<int:person_id>")
    @guarded
    def calls_person(person_id):
        from . import access
        if not access.can_person(person_id):
            return jsonify({"error": "You don't have access to that contact."}), 403
        return jsonify({"calls": calls_for_person(person_id) + no_shows_for_person(person_id)})

    @app.get("/api/calls/event/<int:event_id>")
    @guarded
    def calls_event(event_id):
        from . import access
        event = store.get_event(event_id)
        if not event or not access.can_event(event):
            return jsonify({"error": "no such meeting"}), 404
        return jsonify({"call": call_for_event(event_id), "attendees": event_attendees(event_id)})

    @app.get("/api/calendar/meetings/people")
    @guarded
    def meetings_people():
        """Contacts with an email that match what is typed in the Invite box (for the suggestions list)."""
        from assistant.contact_lookup import find_people
        q = request.args.get("q", "").strip()
        people = find_people(q, email_only=True) if len(q) >= 2 else []
        from . import access
        allowed = access.person_ids()                  # None = the whole database
        if allowed is not None:
            people = [p for p in people if p["person_id"] in allowed]
        return jsonify({"people": [{"name": p["name"], "email": p["email"].lower(), "company": p.get("company") or ""} for p in people[:6]]})

    @app.post("/api/calendar/meetings")
    @guarded
    def meetings_create():
        """Create a meeting in Google Calendar (optionally with a Meet link) and send the invites."""
        from . import google_sync
        body = request.get_json(silent=True) or {}
        try:
            store.check_not_past(str(body.get("start_date", "")), str(body.get("start_time", "")))
        except store.ValidationError as exc:
            raise CallsError(str(exc))
        emails, unknown = parse_invitees(str(body.get("invite", "")))
        if unknown:
            raise CallsError("Not found as a contact with an email: " + ", ".join(unknown) + ". Type their email address instead.")
        try:
            meeting = google_sync.create_meeting(str(body.get("title", "")), str(body.get("start_date", "")), str(body.get("start_time", "")),
                                                 int(body.get("duration_min") or 30), emails, bool(body.get("meet", True)), str(body.get("notes", "")),
                                                 bool(body.get("expect_recording", True)))
        except google_sync.GoogleSyncError as exc:
            raise CallsError(str(exc))
        return jsonify(meeting), 201

    @app.delete("/api/calendar/meetings/<int:event_id>")
    @guarded
    def meetings_delete(event_id):
        """Delete a Google meeting (from Google Calendar too; invited people get a cancellation)."""
        from . import google_sync
        try:
            ok = google_sync.delete_meeting(event_id)
        except google_sync.GoogleSyncError as exc:
            raise CallsError(str(exc))
        return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": "not found"}), 404)

    @app.post("/api/calendar/meetings/<int:event_id>/description")
    @guarded
    def meetings_description(event_id):
        """Write the guest-visible description (e.g. an agenda) of a meeting you organized, in Google Calendar."""
        from . import google_sync, access
        ev = store.get_event(event_id)
        if not ev or not access.can_event(ev):
            return jsonify({"error": "not found"}), 404
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(google_sync.set_description(event_id, str(body.get("text") or ""), body.get("mode") or "append", bool(body.get("notify"))))
        except google_sync.GoogleSyncError as exc:
            raise CallsError(str(exc))

    @app.get("/api/calls/<int:call_id>")
    @guarded
    def calls_get(call_id):
        call = get_call(call_id)
        return (jsonify({"call": call}), 200) if call else (jsonify({"error": "no such call"}), 404)

    @app.post("/api/calls/<int:call_id>/summarize")
    @guarded
    def calls_summarize(call_id):
        return jsonify({"call": summarize(call_id)})

    @app.post("/api/calls/<int:call_id>/draft-followup")
    @guarded
    def calls_followup(call_id):
        body = request.get_json(silent=True) or {}
        result = draft_followup(call_id, body.get("person_id"))
        if result.get("status") != "pending_confirmation":
            raise CallsError(result.get("message") or "could not write the follow-up")
        return jsonify(result)

    @app.post("/api/calls/<int:call_id>/status")
    @guarded
    def calls_status(call_id):
        return jsonify({"call": set_status(call_id, str((request.get_json(silent=True) or {}).get("status", "")))})

    @app.post("/api/calls/missing/<int:event_id>")
    @guarded
    def calls_missing(event_id):
        """A meeting with no recording: the user says no-show, or that it happened anyway."""
        status = str((request.get_json(silent=True) or {}).get("status", ""))
        if status not in ("no_show", "completed"):
            raise CallsError("status must be no_show or completed")
        from . import access
        event = store.get_event(event_id)
        if not event or not access.can_event(event):
            return jsonify({"error": "no such meeting"}), 404
        event = store.set_meeting(event_id, meeting_status=status)
        return (jsonify({"ok": True}), 200) if event else (jsonify({"error": "no such meeting"}), 404)
