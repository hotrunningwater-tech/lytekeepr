"""Private notes on a meeting: prep before a call, and anything added during or after. One note per meeting per person, seen only by them.

- Kept in LyteKeepr only (meeting_private_notes), never sent to Google, never in invites or emails, never in any guest-visible field.
  (The event's "notes" field is NOT used: for meetings booked from LyteKeepr it becomes the Google description guests see.)
- A meeting is identified by Google's iCalUID when it has one, so the note follows the meeting even when several people's calendars hold a copy.
- The older shared "my notes" on a meeting are moved into its owner's private note once.
- Claude's meeting prep (asked for, or written by the scheduled run) lives in the same private note, as one marked section at the top
  ("── Prep by Claude (Oct 25) ── … ── end of prep ──"): it can be found, replaced (only after the user agrees) or told about,
  and the user's own words around it are never touched. Prep is only for a meeting that is in the calendar.
"""

import re

from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_private_notes (
    meeting_key TEXT NOT NULL,
    who TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (meeting_key, who)
);
"""
MAX = 50_000
PREP_RE = re.compile(r"── Prep by Claude \(([^)]*)\) ──\n(.*?)\n── end of prep ──\n*", re.S)


def split_prep(text: str) -> tuple:
    """A private note -> (Claude's prep, the day it was written, the user's own text)."""
    m = PREP_RE.search(text or "")
    if not m:
        return "", "", text or ""
    return m.group(2).strip(), m.group(1), ((text[:m.start()] + text[m.end():]).strip())


def _with_prep(old: str, prep: str, day: str) -> str:
    section = f"── Prep by Claude ({day}) ──\n{prep.strip()}\n── end of prep ──"
    if PREP_RE.search(old or ""):
        return PREP_RE.sub(lambda _m: section + "\n\n", old, count=1).strip()
    return section + ("\n\n" + old.strip() if (old or "").strip() else "")


def _day(iso: str) -> str:
    from datetime import date
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%b %-d")
    except ValueError:
        return str(iso)[:10]


def is_meeting(ev: dict) -> bool:
    return ev.get("kind") == "meeting" or ev.get("source") == "google" or bool(ev.get("has_call")) or bool(ev.get("expect_recording"))


def _me() -> str:
    from . import users
    return "admin" if users.is_admin() else f"user:{users.current_id()}"


def _key(ev: dict) -> str:
    return f"ical:{ev['ical_uid']}" if ev.get("ical_uid") else f"event:{ev['id']}"


def _ready(conn) -> None:
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT id, ical_uid, owner_id, my_notes FROM events WHERE my_notes != ''").fetchall()
    admins = {r[0] for r in conn.execute("SELECT id FROM users WHERE role = 'admin'")} if rows else set()
    for r in rows:                                    # the old shared notes become their owner's private note (once)
        who = f"user:{r['owner_id']}" if r["owner_id"] and r["owner_id"] not in admins else "admin"
        key = _key(dict(r))
        have = conn.execute("SELECT text FROM meeting_private_notes WHERE meeting_key = ? AND who = ?", (key, who)).fetchone()
        text = ((have["text"] + "\n\n") if have and have["text"] else "") + r["my_notes"]
        conn.execute("INSERT OR REPLACE INTO meeting_private_notes (meeting_key, who, text, updated_at) VALUES (?, ?, ?, ?)",
                     (key, who, text, store.now_local().isoformat(timespec="minutes")))
        conn.execute("UPDATE events SET my_notes = '' WHERE id = ?", (r["id"],))
    for r in conn.execute("SELECT * FROM meeting_private_notes WHERE who LIKE 'brief:%'").fetchall():   # briefs kept apart before: into the notes, once
        who = r["who"][len("brief:"):]
        have = conn.execute("SELECT text FROM meeting_private_notes WHERE meeting_key = ? AND who = ?", (r["meeting_key"], who)).fetchone()
        old = have["text"] if have else ""
        if r["text"].strip() and not split_prep(old)[0]:
            conn.execute("INSERT OR REPLACE INTO meeting_private_notes (meeting_key, who, text, updated_at) VALUES (?, ?, ?, ?)",
                         (r["meeting_key"], who, _with_prep(old, r["text"], _day(r["updated_at"])), r["updated_at"]))
        conn.execute("DELETE FROM meeting_private_notes WHERE meeting_key = ? AND who = ?", (r["meeting_key"], r["who"]))


def _event(event_id: int) -> dict:
    from . import access
    ev = store.get_event(int(event_id))
    if not ev or not access.can_event(ev):
        raise ValueError("No such meeting.")
    return ev


def get(event_id: int) -> dict:
    ev = _event(event_id)
    with store._conn() as conn:
        _ready(conn)
        row = conn.execute("SELECT text, updated_at FROM meeting_private_notes WHERE meeting_key = ? AND who = ?", (_key(ev), _me())).fetchone()
    prep, prepared_on, _ = split_prep(row["text"] if row else "")
    return {"item_id": ev["id"], "title": ev["title"], "date": ev["start_date"], "time": ev["start_time"],
            "private_notes": row["text"] if row else "", "updated_at": row["updated_at"] if row else "",
            "prep": prep, "prepared_on": prepared_on}


def set_prep(event_id: int, text: str, replace: bool = False) -> dict:
    """Claude's prep for a scheduled meeting, into the user's private note on it. An earlier prep is replaced only with replace=True
    (the user agreed); otherwise the answer says it is already prepped, so Claude tells the user instead of redoing it."""
    ev = _event(event_id)
    if not is_meeting(ev):
        raise ValueError(f"\"{ev['title']}\" is not a meeting. Prep is only for a meeting in the calendar.")
    if not (text or "").strip():
        raise ValueError("The prep is empty.")
    with store._conn() as conn:
        _ready(conn)
        row = conn.execute("SELECT text FROM meeting_private_notes WHERE meeting_key = ? AND who = ?", (_key(ev), _me())).fetchone()
        old = row["text"] if row else ""
        prep, prepared_on, _ = split_prep(old)
        if prep and not replace:
            return {"saved": False, "already_prepped": True, "prepared_on": prepared_on, "prep": prep, "item_id": ev["id"], "title": ev["title"],
                    "tell_the_user": f"This meeting is already prepped ({prepared_on}). Show that prep; prepare again only if they insist (then replace=true)."}
        day = store.now_local().strftime("%b %-d")
        conn.execute("INSERT OR REPLACE INTO meeting_private_notes (meeting_key, who, text, updated_at) VALUES (?, ?, ?, ?)",
                     (_key(ev), _me(), _with_prep(old, text, day)[:MAX], store.now_local().isoformat(timespec="minutes")))
    return {"saved": True, "replaced": bool(prep), "item_id": ev["id"], "title": ev["title"], "where": "the meeting's private notes"}


def write(event_id: int, text: str, mode: str = "append") -> dict:
    """append (the default: add at the end, keeping what is there) or replace (the whole note; empty clears it)."""
    ev = _event(event_id)
    text = text or ""
    with store._conn() as conn:
        _ready(conn)
        row = conn.execute("SELECT text FROM meeting_private_notes WHERE meeting_key = ? AND who = ?", (_key(ev), _me())).fetchone()
        old = row["text"] if row else ""
        if mode == "replace":
            new = text
        elif mode == "append":
            new = (old.rstrip() + "\n\n" + text.strip()) if old.strip() else text.strip()
        else:
            raise ValueError("mode is append or replace.")
        conn.execute("INSERT OR REPLACE INTO meeting_private_notes (meeting_key, who, text, updated_at) VALUES (?, ?, ?, ?)",
                     (_key(ev), _me(), new[:MAX], store.now_local().isoformat(timespec="minutes")))
    return get(event_id)


def add_to(events: list) -> None:
    """Put the viewer's own private note on each event (and drop the old shared field from what is sent)."""
    if not events:
        return
    with store._conn() as conn:
        _ready(conn)
        mine = {r["meeting_key"]: r["text"] for r in conn.execute("SELECT meeting_key, text FROM meeting_private_notes WHERE who = ?", (_me(),))}
    for e in events:
        if isinstance(e, dict) and e.get("id") is not None:
            e["private_notes"] = mine.get(_key(e), "")
            e["prep"] = split_prep(e["private_notes"])[0]       # Claude's prep inside it, for the Prep button on Home
            e.pop("my_notes", None)


def has_any(conn, ev) -> bool:
    """Does anyone have a private note on this meeting (so the sync keeps it)."""
    conn.executescript(SCHEMA)
    return bool(conn.execute("SELECT 1 FROM meeting_private_notes WHERE meeting_key = ? AND text != ''", (_key(dict(ev)),)).fetchone())


def register(app):
    from flask import jsonify, request

    def run(fn, *a):
        try:
            return jsonify(fn(*a))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/calendar/events/<int:event_id>/private-notes")
    def private_notes_get(event_id):
        return run(get, event_id)

    @app.put("/api/calendar/events/<int:event_id>/prep")
    def meeting_prep_put(event_id):
        b = request.get_json(silent=True) or {}
        return run(set_prep, event_id, b.get("text", ""), bool(b.get("replace")))

    @app.put("/api/calendar/events/<int:event_id>/private-notes")
    def private_notes_put(event_id):
        b = request.get_json(silent=True) or {}
        return run(write, event_id, b.get("text", ""), b.get("mode") or "replace")
