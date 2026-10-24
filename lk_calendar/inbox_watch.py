"""New messages on autopilot (#11 unanswered emails and potential customers, #12 commitments): every scheduled Claude run gets
what arrived since the last one and keeps only clear flags. No AI here: Claude, on the user's own plan, does the reading.

The feed (the admin's, whose mailbox it is; oldest first, at most BATCH items a run, nothing older than LOOK_BACK_DAYS):
- messages saved to contacts (email, LinkedIn, Instagram), both directions, since the user's own promises count too;
- emails from people who are not contacts yet. Gmail's Primary inbox only (Promotions, Social, Updates and Forums never get
  this far), minus automated senders (noreply@, notifications@, mailing tools; the same list the relationship graph uses);
- call transcripts that came in (Claude reads them with get_call).
Claude answers with one save_message_review call: the keys it went through (an item is offered again until then, so a run that
breaks off loses nothing), the flags, and any time a lead gave for the next contact (that moves the lead's follow-up date: the
job promises.py used our paid AI for).

Flags: interested (a potential customer), waiting (a real message waiting for the user's answer), commitment (a clear promise or
deadline, ours or theirs). One flag per item and kind. A "waiting" flag closes by itself once the user writes to that person.
Home lists the open flags with Done and Dismiss."""

from datetime import timedelta

from . import store

LOOK_BACK_DAYS = 3
BATCH = 40
TEXT_CHARS = 1500
KINDS = ("interested", "waiting", "commitment")
SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_seen (key TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox_flags (
    flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT '',          -- commitments: 'ours' or 'theirs'
    person_id INTEGER,
    who TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    due TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT '',            -- when the message was sent
    state TEXT NOT NULL DEFAULT 'open',     -- open | done | dismissed | answered
    created_at TEXT NOT NULL,
    UNIQUE (key, kind)
);
"""
INSTRUCTIONS = (
    "Read each item (for a call, read it with get_call). Most items deserve NO flag; flag only what clearly needs the user:\n"
    "- interested: someone showing real or tentative interest in buying or working together. Not a polite thank-you, not a vendor pitching the user, not spam or scams.\n"
    "- waiting: a real person's message that asks or needs an answer from the user. Skip FYIs, thank-yous, automated mail, and threads the user already answered "
    "(check get_contact_history when unsure).\n"
    "- commitment: a clear promise, deadline or deliverable with a who and roughly a when ('I'll send the deck Friday', 'we decide by the 30th'). "
    "side = 'ours' if the user promised, 'theirs' if they did; due = the date if one is said (YYYY-MM-DD). Skip vague intentions, casual remarks and personal plans.\n"
    "Each flag: key, kind, a one-line summary in plain words, and the exact quote. "
    "If a message from or to a lead (lead: true) gives a time for the next contact ('call me next week', 'let's talk in November'), add a follow_ups entry "
    "{contact_id, date (YYYY-MM-DD, worked out from the message's date), quote}. "
    "Then call save_message_review ONCE with every key you read in `reviewed`, even when there are no flags.")


class InboxError(ValueError):
    pass


def _conn():
    conn = store._conn()
    conn.executescript(SCHEMA)
    return conn


def _admin() -> bool:
    from . import users
    return users.is_admin()


def _since() -> str:
    return (store.now_local().date() - timedelta(days=LOOK_BACK_DAYS)).isoformat()


# ------------------------------------------------------------------ the feed
def _contact_messages(since: str) -> list:
    from . import contacts
    with contacts._crm() as conn:
        rows = conn.execute("""SELECT m.message_id, m.person_id, p.name, m.channel, m.direction, m.message_text, m.timestamp FROM messages m
                               JOIN people p ON p.person_id = m.person_id WHERE m.timestamp >= ? ORDER BY m.timestamp""", (since,)).fetchall()
    with store._conn() as cal:
        leads = {r["person_id"] for r in cal.execute("SELECT person_id FROM leads WHERE person_id IS NOT NULL")}
    return [{"key": f"msg:{r['message_id']}", "source": "contact_message", "channel": r["channel"], "direction": r["direction"],
             "contact_id": r["person_id"], "name": r["name"], "lead": r["person_id"] in leads,
             "text": (r["message_text"] or "")[:TEXT_CHARS], "at": r["timestamp"]} for r in rows]


def _new_sender_emails(since: str) -> list:
    from . import relationships
    try:
        from gmail import storage, sync
    except Exception:
        return []
    out = []
    for account_id in storage.list_account_ids():
        try:
            pending = sync.get_pending_candidates(account_id)
        except Exception:
            continue
        for c in pending:
            if (c.get("receivedAt") or "") < since or relationships.is_generic((c.get("senderEmail") or "").lower()):
                continue
            full = storage.load_pending(account_id, c["id"]) or {}
            out.append({"key": f"mail:{account_id}:{c['id']}", "source": "new_sender_email", "channel": "email", "direction": "incoming",
                        "name": c.get("senderName") or "", "email": (c.get("senderEmail") or "").lower(), "subject": c.get("subject") or "",
                        "text": (full.get("textBody") or c.get("preview") or "")[:TEXT_CHARS], "at": c.get("receivedAt") or ""})
    return out


def _new_calls(since: str) -> list:
    from . import calls
    with calls._crm() as conn:
        rows = conn.execute("SELECT call_id, title, started_at FROM calls WHERE transcript != '[]' AND started_at >= ? ORDER BY started_at", (since,)).fetchall()
        people = calls._people_on(conn, [r["call_id"] for r in rows])
    return [{"key": f"call:{r['call_id']}", "source": "call", "call_id": r["call_id"], "title": r["title"], "at": r["started_at"],
             "contacts": [{"contact_id": p["person_id"], "name": p["name"]} for p in people.get(r["call_id"], [])]} for r in rows]


def feed() -> list:
    """What Claude should read on this run (the admin only: it is the admin's mailbox)."""
    if not _admin():
        return []
    since = _since()
    items = _contact_messages(since) + _new_sender_emails(since) + _new_calls(since)
    with _conn() as conn:
        seen = {r["key"] for r in conn.execute("SELECT key FROM inbox_seen")}
    from .recent import _moment
    items = sorted((i for i in items if i["key"] not in seen), key=lambda i: _moment(i["at"]))
    return items[:BATCH]


# ------------------------------------------------------------------ saving what Claude found
def _describe(key: str) -> dict:
    """Who and when an item is, worked out here from its key (never taken from the AI)."""
    from . import calls, contacts
    kind, _, rest = key.partition(":")
    if kind == "msg":
        with contacts._crm() as conn:
            r = conn.execute("""SELECT m.person_id, p.name, m.timestamp FROM messages m JOIN people p ON p.person_id = m.person_id
                                WHERE m.message_id = ?""", (int(rest),)).fetchone()
        if r:
            return {"person_id": r["person_id"], "who": r["name"], "email": "", "at": r["timestamp"]}
    elif kind == "mail":
        account_id, _, mid = rest.partition(":")
        try:
            from gmail import storage
            m = storage.load_pending(account_id, mid) or storage.load_message(account_id, mid) or {}
        except Exception:
            m = {}
        frm = m.get("from") or {}
        if m:
            return {"person_id": None, "who": frm.get("name") or frm.get("email") or "", "email": (frm.get("email") or "").lower(), "at": m.get("receivedAt") or ""}
    elif kind == "call":
        call = calls.get_call(int(rest))
        if call:
            first = (call.get("contacts") or [{}])[0]
            return {"person_id": first.get("person_id"), "who": first.get("name") or call["title"], "email": "", "at": call["started_at"]}
    raise InboxError(f"Unknown item {key}.")


def _follow_up(contact_id: int, day: str, quote: str) -> bool:
    """A lead gave a time for the next contact: move its follow-up there (the rule promises.py follows)."""
    from datetime import date
    from . import leads
    try:
        d = date.fromisoformat(str(day))
    except ValueError:
        return False
    if d < store.now_local().date():
        return False
    with store._conn() as conn:
        buckets = store.bucket_map(conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM leads WHERE person_id = ?", (int(contact_id),))]
    done = False
    for lead in rows:
        if buckets.get((lead["channel"], lead["status"]), "none") == "closed":
            continue
        leads.set_follow_up(lead["id"], d.isoformat())
        if quote:
            with store._conn() as conn:
                conn.execute("UPDATE events SET notes = ? WHERE lead_ref = ? AND source = 'auto'", (f"They said: “{quote[:200]}”", f"lead:{lead['id']}"))
        done = True
    return done


def save_review(reviewed: list, flags: list, follow_ups: list) -> dict:
    if not _admin():
        raise InboxError("Only the admin's mailbox is reviewed.")
    now = store.now_local().isoformat(timespec="minutes")
    saved, moved = 0, 0
    with _conn() as conn:
        for f in flags or []:
            kind = str(f.get("kind") or "")
            if kind not in KINDS or not str(f.get("summary") or "").strip():
                continue
            d = _describe(str(f.get("key") or ""))
            side = str(f.get("side") or "") if kind == "commitment" else ""
            cur = conn.execute("""INSERT OR IGNORE INTO inbox_flags (key, kind, side, person_id, who, email, summary, quote, due, at, created_at)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (f["key"], kind, side if side in ("ours", "theirs") else "", d["person_id"], d["who"], d["email"],
                                str(f["summary"]).strip()[:300], str(f.get("quote") or "").strip()[:400], str(f.get("due") or "")[:10], d["at"], now))
            saved += cur.rowcount
    for fu in follow_ups or []:
        if fu.get("contact_id") and _follow_up(fu["contact_id"], fu.get("date"), str(fu.get("quote") or "")):
            moved += 1
    with _conn() as conn:
        for key in reviewed or []:
            conn.execute("INSERT OR IGNORE INTO inbox_seen (key, seen_at) VALUES (?, ?)", (str(key), now))
    return {"reviewed": len(reviewed or []), "flags_saved": saved, "follow_ups_moved": moved}


# ------------------------------------------------------------------ Home
def _answered(conn, flag) -> bool:
    """A 'waiting' flag is answered once the user wrote to that contact after the message."""
    if not flag["person_id"]:
        return False
    from . import contacts
    from .recent import _moment
    with contacts._crm() as crm:
        rows = crm.execute("SELECT timestamp FROM messages WHERE person_id = ? AND direction = 'outgoing'", (flag["person_id"],)).fetchall()
    return any(_moment(r["timestamp"]) > _moment(flag["at"]) for r in rows)


def open_flags() -> list:
    if not _admin():
        return []
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM inbox_flags WHERE state = 'open' ORDER BY at DESC")]
        out = []
        for f in rows:
            if f["kind"] == "waiting" and _answered(conn, f):
                conn.execute("UPDATE inbox_flags SET state = 'answered' WHERE flag_id = ?", (f["flag_id"],))
                continue
            out.append(f)
    return out


def set_state(flag_id: int, state: str) -> dict:
    if not _admin():
        raise InboxError("Only the admin's flags.")
    if state not in ("done", "dismissed", "open"):
        raise InboxError("state is done, dismissed or open.")
    with _conn() as conn:
        conn.execute("UPDATE inbox_flags SET state = ? WHERE flag_id = ?", (state, int(flag_id)))
    return {"flag_id": int(flag_id), "state": state}


def register(app):
    from flask import jsonify, request

    def run(fn, *a):
        try:
            return jsonify(fn(*a))
        except InboxError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/calendar/inbox/flags")
    def inbox_flags():
        return run(lambda: {"flags": open_flags()})

    @app.post("/api/calendar/inbox/flags/<int:flag_id>")
    def inbox_flag_state(flag_id):
        return run(set_state, flag_id, str((request.get_json(silent=True) or {}).get("state", "")))

    @app.post("/api/calendar/inbox/review")
    def inbox_review():
        b = request.get_json(silent=True) or {}
        return run(save_review, b.get("reviewed") or [], b.get("flags") or [], b.get("follow_ups") or [])
