"""Contact notes and logged conversations, and the combined communication history of an opportunity.

Two kinds of record in one table (contact_records):
- kind 'note': a general note about a contact (not a conversation).
- kind 'conversation': a phone call, meeting, in-person talk or other conversation that no integration captured. It shows in the contact's
  communication history next to LinkedIn messages, emails and call recordings. It can include other contacts (contact_record_people)
  and can name the opportunity it was about.
Nothing already stored elsewhere (messages, saved emails, call recordings) is copied: histories are put together when asked for.
Records are kept when deleted (marked), like documents.
"""

from contextlib import contextmanager
from datetime import datetime

from . import store

TYPES = {"phone": "phone call", "call": "phone call", "phone call": "phone call", "meeting": "meeting", "in person": "in person",
         "in-person": "in person", "inperson": "in person", "video": "video call", "video call": "video call", "other": "conversation",
         "conversation": "conversation", "chat": "conversation"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- 'note' | 'conversation'
    channel TEXT NOT NULL DEFAULT '',   -- conversations: phone call, meeting, in person, video call, conversation
    text TEXT NOT NULL,
    happened_at TEXT NOT NULL,
    opportunity_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    who TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_contact_records_person ON contact_records(person_id, kind);
CREATE TABLE IF NOT EXISTS contact_record_people (
    record_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    PRIMARY KEY (record_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_record_people ON contact_record_people(person_id);
"""


class RecordError(ValueError):
    pass


@contextmanager
def _crm():
    from .contacts import _crm as crm
    with crm() as conn:
        conn.executescript(SCHEMA)
        yield conn


def _now() -> str:
    return store.now_local().isoformat(timespec="minutes")


def _who():
    from .opportunities import _actor
    from . import users
    return _actor(), users.current_id()


def _when(text) -> str:
    """'2026-10-18', '2026-10-18 14:30' or '2026-10-18T14:30' → an ISO time; empty → now."""
    t = str(text or "").strip().replace(" ", "T")
    if not t:
        return _now()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t[:19] if "T" in t else t[:10], fmt).isoformat(timespec="minutes")
        except ValueError:
            pass
    raise RecordError("Dates look like 2026-10-18 or 2026-10-18 14:30.")


def _can(person_id) -> bool:
    from . import access
    return access.can_person(person_id)


def _row(conn, r) -> dict:
    d = dict(r)
    d["mine"] = _mine(r)                              # may this person change or delete it
    others = [x["person_id"] for x in conn.execute("SELECT person_id FROM contact_record_people WHERE record_id = ?", (d["record_id"],))]
    ids = [d["person_id"]] + [p for p in others if p != d["person_id"]]
    names = {x["person_id"]: x["name"] for x in conn.execute(f"SELECT person_id, name FROM people WHERE person_id IN ({','.join('?' for _ in ids)})", ids)}
    d["people"] = [{"contact_id": p, "name": names.get(p, "") or f"contact #{p}"} for p in ids if _can(p)]
    if d["opportunity_id"]:
        try:
            o = conn.execute("SELECT name FROM opportunities WHERE opportunity_id = ?", (d["opportunity_id"],)).fetchone()
        except Exception:
            o = None
        d["opportunity"] = o["name"] if o else ""
    return d


def _mine(r) -> bool:
    """Only whoever saved a note or conversation (or the admin) may change or delete it."""
    from . import users
    return users.is_admin() or (r["user_id"] is not None and r["user_id"] == users.current_id())


def _record(conn, record_id) -> dict:
    r = conn.execute("SELECT * FROM contact_records WHERE record_id = ? AND deleted_at = ''", (int(record_id),)).fetchone()
    if not r or not _can(r["person_id"]):
        raise RecordError("No such note.")
    if not _mine(r):
        raise RecordError(f"Only {r['who'] or 'whoever saved it'} or the admin can change or delete this.")
    return dict(r)


# ------------------------------------------------------------------ writing
def add(person_id: int, kind: str, text: str, channel: str = "", when=None, opportunity_id=None, others: list | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise RecordError("Write something first.")
    if kind not in ("note", "conversation"):
        raise RecordError("kind is note or conversation.")
    if not _can(person_id):
        raise RecordError("You don't have access to that contact.")
    ch = ""
    if kind == "conversation":
        ch = TYPES.get(str(channel or "conversation").strip().lower())
        if not ch:
            raise RecordError("The type is one of: phone call, meeting, in person, video call, other.")
    others = [int(p) for p in (others or []) if int(p) != int(person_id)]
    for p in others:
        if not _can(p):
            raise RecordError("You don't have access to one of those contacts.")
    if opportunity_id:
        from . import opportunities, access
        o = opportunities.get(int(opportunity_id))
        if not o or not access.can_opportunity(o):
            raise RecordError("No such opportunity.")
    who, uid = _who()
    with _crm() as conn:
        if not conn.execute("SELECT 1 FROM people WHERE person_id = ?", (int(person_id),)).fetchone():
            raise RecordError("No such contact.")
        cur = conn.execute("""INSERT INTO contact_records (person_id, kind, channel, text, happened_at, opportunity_id, created_at, updated_at, who, user_id)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (int(person_id), kind, ch, text[:50000], _when(when), int(opportunity_id) if opportunity_id else None, _now(), _now(), who, uid))
        for p in others:
            conn.execute("INSERT OR IGNORE INTO contact_record_people (record_id, person_id) VALUES (?, ?)", (cur.lastrowid, p))
        return _row(conn, conn.execute("SELECT * FROM contact_records WHERE record_id = ?", (cur.lastrowid,)).fetchone())


def edit(record_id: int, text=None, when=None, channel=None, opportunity_id=None) -> dict:
    with _crm() as conn:
        r = _record(conn, record_id)
        sets = {}
        if text is not None and text.strip():
            sets["text"] = text.strip()[:50000]
        if when:
            sets["happened_at"] = _when(when)
        if channel and r["kind"] == "conversation":
            ch = TYPES.get(str(channel).strip().lower())
            if not ch:
                raise RecordError("The type is one of: phone call, meeting, in person, video call, other.")
            sets["channel"] = ch
        if opportunity_id is not None:
            sets["opportunity_id"] = int(opportunity_id) if opportunity_id else None
        if sets:
            sets["updated_at"] = _now()
            conn.execute(f"UPDATE contact_records SET {', '.join(k + ' = :' + k for k in sets)} WHERE record_id = :id", {**sets, "id": r["record_id"]})
        return _row(conn, conn.execute("SELECT * FROM contact_records WHERE record_id = ?", (r["record_id"],)).fetchone())


def delete(record_id: int) -> None:
    with _crm() as conn:
        r = _record(conn, record_id)
        conn.execute("UPDATE contact_records SET deleted_at = ? WHERE record_id = ?", (_now(), r["record_id"]))


# ------------------------------------------------------------------ reading
def for_person(person_id: int, kind: str | None = None) -> list:
    """A contact's records (as the main contact or as someone else in the conversation), newest first."""
    with _crm() as conn:
        rows = conn.execute("""SELECT * FROM contact_records WHERE deleted_at = '' AND (person_id = ? OR record_id IN
                               (SELECT record_id FROM contact_record_people WHERE person_id = ?)) ORDER BY happened_at DESC, record_id DESC""",
                            (int(person_id), int(person_id))).fetchall()
        return [_row(conn, r) for r in rows if kind is None or r["kind"] == kind]


def as_message(r: dict) -> dict:
    """A logged conversation shaped like the other entries of a communication history."""
    return {"message_id": f"record:{r['record_id']}", "record_id": r["record_id"], "channel": r["channel"] or "conversation", "logged": True,
            "direction": "logged", "sender": r["who"] or "", "message_text": r["text"], "timestamp": r["happened_at"],
            "people": r["people"], "opportunity_id": r["opportunity_id"], "opportunity": r.get("opportunity", ""), "mine": r.get("mine", False)}


def person_history(person_id: int) -> list:
    """Everything that happened with a contact, oldest first: their messages (LinkedIn, email, saved emails; for the admin only, as on the contact page),
    call recordings and no-shows, and logged conversations."""
    from crm import db as crm_db
    from . import contacts, users, calls
    items = []
    if users.is_admin():
        conn = crm_db.get_connection()
        try:
            detail = crm_db.get_person_detail(conn, person_id) or {}
        finally:
            conn.close()
        items += contacts.messages_with_saved_emails(person_id, detail.get("messages") or [])
    for c in calls.calls_for_person(person_id) + calls.no_shows_for_person(person_id):
        items.append({"message_id": f"call:{c.get('call_id') or c.get('event_id')}", "channel": "call", "timestamp": c.get("started_at"), "call": c,
                      "message_text": c.get("summary") or "", "direction": "call"})
    items += [as_message(r) for r in for_person(person_id, "conversation")]
    if users.is_admin():                                 # a waiting draft to them, marked as a draft (not sent)
        from . import autopilot
        draft = autopilot.draft_item(person_id)
        if draft:
            items.append(draft)
    return _sorted(items)


def _sorted(items: list) -> list:
    def moment(m):
        from .contacts import _when as parse
        d = parse(m.get("timestamp"))
        return d.timestamp() if d else 0.0
    return sorted(items, key=moment)


def opportunity_history(opp_id: int) -> dict:
    """The combined history of everyone on an opportunity (the ones this person may see), oldest first; each entry says who was in it.
    An email or call shared by several of them appears once."""
    from . import opportunities
    o = opportunities.get(opp_id)
    if not o:
        raise RecordError("No such opportunity.")
    people = [{"contact_id": p["contact_id"], "name": p["name"], "active": p["active"]} for p in o["participants"]]
    merged: dict = {}
    for p in people:
        for m in person_history(p["contact_id"]):
            key = m.get("message_id") or f"{m.get('channel')}:{m.get('timestamp')}:{(m.get('message_text') or '')[:80]}"
            if key not in merged:
                merged[key] = {**m, "contacts": []}
            if p["name"] not in [x["name"] for x in merged[key]["contacts"]]:
                merged[key]["contacts"].append({"contact_id": p["contact_id"], "name": p["name"]})
    return {"opportunity_id": o["opportunity_id"], "name": o["name"], "people": people, "history": _sorted(list(merged.values()))}


def search(q: str, person_id=None, opportunity_id=None, kind=None, limit: int = 30) -> list:
    """Notes and logged conversations whose text has every word, newest first; only for contacts this person may see."""
    words = [w for w in (q or "").lower().split() if w]
    if not words:
        raise RecordError("What should I look for?")
    with _crm() as conn:
        rows = conn.execute("SELECT * FROM contact_records WHERE deleted_at = '' ORDER BY happened_at DESC").fetchall()
        out = []
        for r in rows:
            if kind and r["kind"] != kind:
                continue
            if opportunity_id and r["opportunity_id"] != int(opportunity_id):
                continue
            if person_id and r["person_id"] != int(person_id) and not conn.execute(
                    "SELECT 1 FROM contact_record_people WHERE record_id = ? AND person_id = ?", (r["record_id"], int(person_id))).fetchone():
                continue
            if not all(w in (r["text"] or "").lower() for w in words) or not _can(r["person_id"]):
                continue
            out.append(_row(conn, r))
            if len(out) >= limit:
                break
        return out


def merged(keep_id: int, merge_id: int) -> None:
    """Two contacts became one: the other one's notes and conversations move over."""
    with _crm() as conn:
        conn.execute("UPDATE contact_records SET person_id = ? WHERE person_id = ?", (keep_id, merge_id))
        conn.execute("UPDATE OR IGNORE contact_record_people SET person_id = ? WHERE person_id = ?", (keep_id, merge_id))
        conn.execute("DELETE FROM contact_record_people WHERE person_id = ?", (merge_id,))
        conn.execute("DELETE FROM contact_record_people WHERE record_id IN (SELECT record_id FROM contact_records WHERE person_id = ?) AND person_id = ?", (keep_id, keep_id))


def contact_deleted(person_id: int) -> None:
    """A contact was deleted: their notes and conversations go (others in a shared conversation keep it)."""
    with _crm() as conn:
        conn.execute("UPDATE contact_records SET deleted_at = ? WHERE person_id = ? AND deleted_at = ''", (_now(), person_id))
        conn.execute("DELETE FROM contact_record_people WHERE person_id = ?", (person_id,))


# ------------------------------------------------------------------ routes (the app and Claude use the same ones)
def register(app):
    from functools import wraps
    from flask import jsonify, request
    from . import access

    def guarded(fn):
        @wraps(fn)
        def inner(*a, **k):
            try:
                return jsonify(fn(*a, **k))
            except (RecordError, store.ValidationError) as exc:
                return jsonify({"error": str(exc)}), 400
        return inner

    def b():
        return request.get_json(silent=True) or {}

    @app.get("/api/crm/people/<int:person_id>/notes")
    @guarded
    def records_notes(person_id):
        return {"notes": for_person(person_id, "note")}

    @app.post("/api/crm/people/<int:person_id>/notes")
    @guarded
    def records_note_add(person_id):
        return add(person_id, "note", b().get("text", ""), when=b().get("when"))

    @app.post("/api/crm/people/<int:person_id>/conversations")
    @guarded
    def records_conversation_add(person_id):
        x = b()
        return add(person_id, "conversation", x.get("text", ""), x.get("type") or x.get("channel") or "", x.get("when"),
                   x.get("opportunity_id"), x.get("other_contact_ids"))

    @app.get("/api/crm/people/<int:person_id>/history")
    @guarded
    def records_person_history(person_id):
        return {"history": person_history(person_id)}

    @app.put("/api/crm/records/<int:record_id>")
    @guarded
    def records_edit(record_id):
        x = b()
        return edit(record_id, x.get("text"), x.get("when"), x.get("type") or x.get("channel"), x.get("opportunity_id"))

    @app.post("/api/crm/records/<int:record_id>/delete")
    @guarded
    def records_delete(record_id):
        delete(record_id)
        return {"ok": True}

    @app.get("/api/crm/records/search")
    @guarded
    def records_search():
        a = request.args
        return {"results": search(a.get("q", ""), a.get("contact_id"), a.get("opportunity_id"), a.get("kind"))}

    @app.get("/api/crm/opportunities/<int:opp_id>/history")
    @guarded
    def records_opportunity_history(opp_id):
        from . import opportunities
        o = opportunities.get(opp_id)
        if not o or not access.can_opportunity(o):
            raise RecordError("No such opportunity.")
        return opportunity_history(opp_id)
