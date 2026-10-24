"""Private notes on a meeting: prep before a call, and anything added during or after. One note per meeting per person, seen only by them.

- Kept in LyteKeepr only (meeting_private_notes), never sent to Google, never in invites or emails, never in any guest-visible field.
  (The event's "notes" field is NOT used: for meetings booked from LyteKeepr it becomes the Google description guests see.)
- A meeting is identified by Google's iCalUID when it has one, so the note follows the meeting even when several people's calendars hold a copy.
- The older shared "my notes" on a meeting are moved into its owner's private note once.
"""

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
    from . import autopilot
    with_brief = [dict(ev)]
    autopilot.add_briefs(with_brief)
    return {"item_id": ev["id"], "title": ev["title"], "date": ev["start_date"], "time": ev["start_time"],
            "private_notes": row["text"] if row else "", "updated_at": row["updated_at"] if row else "", "brief": with_brief[0].get("brief", "")}


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
            e.pop("my_notes", None)
    from . import autopilot
    autopilot.add_briefs(events)                      # and the brief Claude wrote for them, if any


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

    @app.put("/api/calendar/events/<int:event_id>/private-notes")
    def private_notes_put(event_id):
        b = request.get_json(silent=True) or {}
        return run(write, event_id, b.get("text", ""), b.get("mode") or "replace")
