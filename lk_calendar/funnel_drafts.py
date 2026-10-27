"""One email for many people in a funnel, written once, saved, and sent one by one (for Claude; the desktop chat's batch stays as it was).

- A draft belongs to a funnel. Its recipients are a list of contacts: everyone with an email in one stage when it is made, or the people
  named; people can be added or removed later. The text may greet each person by name with {name} (their first name; a contact
  with no real name gets just "Hi,"), or be the same for everyone ("Hi, ..."): with no {name} it is sent word for word.
- Drafts keep until they are sent or deleted (funnel_drafts). Several can exist side by side, e.g. one per stage with its own opener.
- Before sending: the preview (one person's copy and the final list). Skipped on their own: no email address, already sent this
  draft, or wrote to the user after the draft was made (they replied).
- Sending: after the user's one yes, one email at a time, a minute apart, from the user's Gmail, through the same send as a single
  email (saved on the contact, "email sent" status), plus the follow-up the user chose once for the whole batch (0 = none).
  The first failure stops the batch and says who wasn't sent; sending again later continues with the rest (nobody gets it twice).
- The admin only: emails go out from the admin's Gmail."""

import json
import threading
import time
from datetime import datetime, timezone

from . import store

PAUSE_SECONDS = 60
MAX_RECIPIENTS = 100
STALE_SECONDS = 180            # a "sending" draft with no progress this long: the server restarted, the batch was interrupted
SCHEMA = """
CREATE TABLE IF NOT EXISTS funnel_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funnel_id INTEGER NOT NULL,
    funnel_name TEXT NOT NULL,
    stage_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    recipients TEXT NOT NULL DEFAULT '[]',     -- person ids
    status TEXT NOT NULL DEFAULT 'draft',      -- draft | sending | stopped | sent
    follow_up_days INTEGER,
    pause_seconds INTEGER NOT NULL DEFAULT 60,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    heartbeat REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS funnel_draft_sends (
    draft_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    status TEXT NOT NULL,                      -- sent | failed
    message TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (draft_id, person_id)
);
"""
_lock = threading.Lock()


class DraftError(ValueError):
    pass


def _conn():
    conn = store._conn()
    conn.executescript(SCHEMA)
    return conn


def _admin():
    from . import users
    if not users.is_admin():
        raise DraftError("Funnel emails go out from the admin's Gmail, so only the admin can draft and send them.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    """{name}, {first_name}, [Name] ... -> the engine's {{first_name}}; then the sign-off placeholder becomes the user's name."""
    import re
    from assistant import bulk_email
    text = re.sub(r"(?<!\{)\{\s*(?:first[_ ]?)?name\s*\}(?!\})", "{{first_name}}", text or "", flags=re.IGNORECASE)     # {name}, {first_name}
    text = bulk_email._AI_NAME_FORMS.sub("{{first_name}}", text)
    try:
        from . import profile
        text = profile.sign(text)
    except Exception:
        pass
    return text


def _funnel(text_or_id) -> dict:
    from crm import db as crm_db
    conn = crm_db.get_connection()
    try:
        funnels = crm_db.list_funnels(conn)
    finally:
        conn.close()
    if str(text_or_id).isdigit():
        hit = [f for f in funnels if f["funnel_id"] == int(text_or_id)]
    else:
        typed = str(text_or_id or "").strip().lower()
        hit = [f for f in funnels if f["name"].lower() == typed] or [f for f in funnels if typed and typed in f["name"].lower()]
    if len(hit) != 1:
        names = ", ".join(f["name"] for f in (hit or funnels))
        raise DraftError(("Which funnel? " if hit else f"No funnel matches '{text_or_id}'. ") + "Funnels: " + names + ".")
    return hit[0]


def _people(ids: list) -> list:
    """Each person with name, first name and email (or none)."""
    from assistant import bulk_email
    from . import contacts
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with contacts._crm() as c:
        rows = c.execute(f"""SELECT p.person_id, p.name, co.company_name,
                                    (SELECT ca.channel_identifier FROM channel_accounts ca WHERE ca.person_id = p.person_id AND ca.channel = 'email'
                                      ORDER BY ca.account_id LIMIT 1) AS email
                             FROM people p LEFT JOIN companies co ON co.company_id = p.company_id WHERE p.person_id IN ({marks})""", ids).fetchall()
    by = {r["person_id"]: dict(r) for r in rows}
    out = []
    for pid in ids:
        r = by.get(pid)
        if r:
            out.append({"contact_id": pid, "name": r["name"] or r["company_name"] or "Unnamed contact", "email": r["email"] or "",
                        "first_name": bulk_email._real_first_name(r["name"] or "", r["company_name"] or "", r["email"] or "")})
    return out


def _stage_ids(funnel_id: int, stage_text: str) -> tuple:
    from crm import db as crm_db
    conn = crm_db.get_connection()
    try:
        stages = crm_db.list_funnel_stages(conn, funnel_id)
        want = str(stage_text or "").strip().lower()
        hit = [s for s in stages if s["name"].lower() == want] or [s for s in stages if want and want in s["name"].lower()]
        if len(hit) != 1:
            raise DraftError(f"Which stage? Stages: {', '.join(s['name'] for s in stages)}.")
        ids = [r["person_id"] for r in conn.execute("SELECT person_id FROM people WHERE funnel_id = ? AND funnel_stage_id = ? ORDER BY name",
                                                    (funnel_id, hit[0]["stage_id"]))]
    finally:
        conn.close()
    return hit[0]["name"], ids


def _row(conn, draft_id: int) -> dict:
    r = conn.execute("SELECT * FROM funnel_drafts WHERE draft_id = ?", (int(draft_id),)).fetchone()
    if not r:
        raise DraftError("No such draft.")
    d = dict(r)
    d["recipients"] = json.loads(d["recipients"] or "[]")
    if d["status"] == "sending" and time.time() - (d["heartbeat"] or 0) > STALE_SECONDS:
        d["status"] = "stopped"
        d["message"] = "Interrupted (the server restarted). Send again to continue with the people who didn't get it."
        conn.execute("UPDATE funnel_drafts SET status = 'stopped', message = ? WHERE draft_id = ?", (d["message"], d["draft_id"]))
    return d


def _summary(d: dict, conn) -> dict:
    sent = conn.execute("SELECT COUNT(*) FROM funnel_draft_sends WHERE draft_id = ? AND status = 'sent'", (d["draft_id"],)).fetchone()[0]
    return {"draft_id": d["draft_id"], "funnel": d["funnel_name"], "stage": d["stage_name"], "subject": d["subject"], "status": d["status"],
            "recipients": len(d["recipients"]), "sent": sent, "greets_by_name": "{{first_name}}" in (d["subject"] + d["body"]),
            "updated_at": d["updated_at"]}


# ------------------------------------------------------------------ drafts
def create(funnel, subject: str, body: str, stage: str = "", contact_ids=None) -> dict:
    _admin()
    if not (body or "").strip():
        raise DraftError("The email needs text.")
    f = _funnel(funnel)
    if contact_ids:
        stage_name, ids = "", [int(i) for i in contact_ids]
    elif stage:
        stage_name, ids = _stage_ids(f["funnel_id"], stage)
    else:
        raise DraftError("Say who gets it: a stage of the funnel, or the contacts.")
    if len(ids) > MAX_RECIPIENTS:
        raise DraftError(f"{len(ids)} people; one draft is limited to {MAX_RECIPIENTS}. Split it (e.g. one draft per stage).")
    now = _now()
    with _conn() as conn:
        did = conn.execute("""INSERT INTO funnel_drafts (funnel_id, funnel_name, stage_name, subject, body, recipients, pause_seconds, created_at, updated_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (f["funnel_id"], f["name"], stage_name, _normalize(subject).strip(), _normalize(body).rstrip(), json.dumps(ids),
                            PAUSE_SECONDS, now, now)).lastrowid
    return preview(did)


def list_drafts(funnel=None) -> dict:
    _admin()
    with _conn() as conn:
        rows = conn.execute("SELECT draft_id FROM funnel_drafts ORDER BY updated_at DESC").fetchall()
        out = [_summary(_row(conn, r["draft_id"]), conn) for r in rows]
    if funnel:
        f = _funnel(funnel)
        out = [d for d in out if d["funnel"] == f["name"]]
    return {"drafts": out}


def update(draft_id: int, subject=None, body=None, add_contact_ids=None, remove_contact_ids=None, stage=None) -> dict:
    _admin()
    with _conn() as conn:
        d = _row(conn, draft_id)
        if d["status"] in ("sending", "sent"):
            raise DraftError(f"This draft is {d['status']}; it can't be changed. Copy it into a new draft instead.")
        ids = d["recipients"]
        stage_name = d["stage_name"]
        if stage:
            stage_name, ids = _stage_ids(d["funnel_id"], stage)
        for i in add_contact_ids or []:
            if int(i) not in ids:
                ids.append(int(i))
        gone = {int(i) for i in remove_contact_ids or []}
        ids = [i for i in ids if i not in gone]
        conn.execute("""UPDATE funnel_drafts SET subject = ?, body = ?, recipients = ?, stage_name = ?, updated_at = ? WHERE draft_id = ?""",
                     (_normalize(subject).strip() if subject is not None else d["subject"], _normalize(body).rstrip() if body is not None else d["body"],
                      json.dumps(ids), stage_name, _now(), d["draft_id"]))
    return preview(draft_id)


def delete(draft_id: int) -> dict:
    _admin()
    with _conn() as conn:
        d = _row(conn, draft_id)
        if d["status"] == "sending":
            raise DraftError("It is being sent right now.")
        conn.execute("DELETE FROM funnel_drafts WHERE draft_id = ?", (d["draft_id"],))
        conn.execute("DELETE FROM funnel_draft_sends WHERE draft_id = ?", (d["draft_id"],))
    return {"deleted": True, "draft_id": d["draft_id"]}


def _plan(d: dict, conn) -> tuple:
    """Who gets it now, and who is skipped and why (no email, already got it, replied after the draft was made)."""
    from . import contacts
    from .recent import _moment
    got = {r["person_id"] for r in conn.execute("SELECT person_id FROM funnel_draft_sends WHERE draft_id = ? AND status = 'sent'", (d["draft_id"],))}
    made = _moment(d["created_at"])
    send, skipped = [], []
    people = _people(d["recipients"])
    ids = [p["contact_id"] for p in people]
    replied = set()
    if ids:
        with contacts._crm() as c:
            for r in c.execute(f"SELECT person_id, timestamp FROM messages WHERE direction = 'incoming' AND person_id IN ({','.join('?' for _ in ids)})", ids):
                if _moment(r["timestamp"]) > made:
                    replied.add(r["person_id"])
    for p in people:
        why = ("no email address" if not p["email"] else "already got this email" if p["contact_id"] in got
               else "replied after this draft was made" if p["contact_id"] in replied else "")
        (skipped if why else send).append({**p, **({"reason": why} if why else {})})
    return send, skipped


def preview(draft_id: int) -> dict:
    from assistant import bulk_email
    _admin()
    with _conn() as conn:
        d = _row(conn, draft_id)
        send, skipped = _plan(d, conn)
        out = _summary(d, conn)
    sample = send[0] if send else {"first_name": ""}
    return {**out, "body": d["body"],
            "sample_copy": {"to": sample.get("name", ""), "subject": bulk_email.render(d["subject"], sample["first_name"]),
                            "body": bulk_email.render(d["body"], sample["first_name"])},
            "will_send_to": [{"contact_id": p["contact_id"], "name": p["name"], "email": p["email"]} for p in send],
            "skipped": [{"contact_id": p["contact_id"], "name": p["name"], "reason": p["reason"]} for p in skipped],
            "pause_seconds": d["pause_seconds"], "message": d["message"]}


# ------------------------------------------------------------------ sending
def send(draft_id: int, follow_up_days, pause_seconds=None) -> dict:
    """Start sending (after the user's one yes). Returns at once; the batch runs on its own."""
    _admin()
    if follow_up_days is None:
        raise DraftError("Ask the user once for the whole batch: follow up in 3 days, another number of days, or no follow-up (0).")
    with _lock:
        with _conn() as conn:
            d = _row(conn, draft_id)
            if d["status"] == "sending":
                raise DraftError("It is already being sent.")
            busy = [r for r in conn.execute("SELECT draft_id FROM funnel_drafts WHERE status = 'sending' AND draft_id != ?", (d["draft_id"],))
                    if _row(conn, r["draft_id"])["status"] == "sending"]
            if busy:
                raise DraftError("Another funnel email is being sent; wait for it to finish (Gmail limits how fast emails can go out).")
            todo, skipped = _plan(d, conn)
            if not todo:
                raise DraftError("Nobody left to send to." + (" Skipped: " + "; ".join(f"{p['name']} ({p['reason']})" for p in skipped) if skipped else ""))
            gap = max(5, min(int(pause_seconds or d["pause_seconds"] or PAUSE_SECONDS), 600))
            conn.execute("UPDATE funnel_drafts SET status = 'sending', follow_up_days = ?, pause_seconds = ?, message = '', heartbeat = ?, updated_at = ? WHERE draft_id = ?",
                         (max(0, int(follow_up_days)), gap, time.time(), _now(), d["draft_id"]))
    from profiles.store import get_active_profile_id
    threading.Thread(target=_run_in, args=(get_active_profile_id(), d["draft_id"], todo, gap, max(0, int(follow_up_days))),
                     name="funnel-draft-send", daemon=True).start()
    return {"started": True, "draft_id": d["draft_id"], "sending_to": len(todo), "skipped": [{"name": p["name"], "reason": p["reason"]} for p in skipped],
            "pause_seconds": gap, "about_minutes": round(len(todo) * gap / 60, 1)}


def _run_in(profile_id: str, *args) -> None:
    from profiles.store import use_profile
    with use_profile(profile_id):                    # the thread works inside the workspace that started it (its Gmail, its contacts)
        _run(*args)


def _run(draft_id: int, todo: list, gap: int, follow_up_days: int) -> None:
    from assistant import bulk_email, email_send_flow
    with _conn() as conn:
        d = _row(conn, draft_id)
    for i, p in enumerate(todo):
        subject, body = bulk_email.render(d["subject"], p["first_name"]), bulk_email.render(d["body"], p["first_name"])
        try:
            res = email_send_flow.send_email(p["contact_id"], p["name"], p["email"], subject, body, follow_up_days=follow_up_days)
        except Exception as exc:
            res = {"status": "failed", "message": f"Not sent to {p['name']}: {exc}"}
        with _conn() as conn:
            conn.execute("INSERT OR REPLACE INTO funnel_draft_sends (draft_id, person_id, status, message, at) VALUES (?, ?, ?, ?, ?)",
                         (draft_id, p["contact_id"], "sent" if res.get("status") == "sent" else "failed", res.get("message", ""), _now()))
            if res.get("status") != "sent":                # the first failure stops the batch: the user decides what next
                left = [x["name"] for x in todo[i:]]
                conn.execute("UPDATE funnel_drafts SET status = 'stopped', message = ?, updated_at = ? WHERE draft_id = ?",
                             (f"Stopped: {res.get('message', 'the send failed')}. Not sent: {', '.join(left)}.", _now(), draft_id))
                return
            conn.execute("UPDATE funnel_drafts SET heartbeat = ?, updated_at = ? WHERE draft_id = ?", (time.time(), _now(), draft_id))
        if i < len(todo) - 1:
            for _ in range(gap):                         # the pause, in one-second steps so the batch keeps showing it is alive
                time.sleep(1)
                if _ % 30 == 0:
                    with _conn() as conn:
                        conn.execute("UPDATE funnel_drafts SET heartbeat = ? WHERE draft_id = ?", (time.time(), draft_id))
    with _conn() as conn:
        conn.execute("UPDATE funnel_drafts SET status = 'sent', message = 'Sent to everyone.', updated_at = ? WHERE draft_id = ?", (_now(), draft_id))


def status(draft_id=None) -> dict:
    """How a batch is going: sent so far, who is left, and who didn't get it."""
    _admin()
    with _conn() as conn:
        if draft_id is None:
            r = conn.execute("SELECT draft_id FROM funnel_drafts WHERE status IN ('sending', 'stopped', 'sent') ORDER BY updated_at DESC LIMIT 1").fetchone()
            if not r:
                return {"message": "No funnel email has been sent yet."}
            draft_id = r["draft_id"]
        d = _row(conn, draft_id)
        sends = {r["person_id"]: dict(r) for r in conn.execute("SELECT * FROM funnel_draft_sends WHERE draft_id = ?", (d["draft_id"],))}
        people = _people(d["recipients"])
    got = [p["name"] for p in people if sends.get(p["contact_id"], {}).get("status") == "sent"]
    failed = [{"name": p["name"], "message": sends[p["contact_id"]]["message"]} for p in people if sends.get(p["contact_id"], {}).get("status") == "failed"]
    not_yet = [p["name"] for p in people if p["contact_id"] not in sends and p["email"]]
    no_email = [p["name"] for p in people if not p["email"]]
    return {"draft_id": d["draft_id"], "funnel": d["funnel_name"], "stage": d["stage_name"], "status": d["status"], "message": d["message"],
            "sent": len(got), "of": len(people), "got_it": got, "failed": failed, "not_sent_yet": not_yet, "skipped_no_email": no_email,
            "follow_up_days": d["follow_up_days"]}


def register(app):
    from flask import jsonify, request

    def run(fn, *a, **k):
        try:
            return jsonify(fn(*a, **k))
        except (DraftError, store.ValidationError) as exc:
            return jsonify({"error": str(exc)}), 400

    def b():
        return request.get_json(silent=True) or {}

    @app.get("/api/calendar/funnel-drafts")
    def fd_list():
        return run(list_drafts, request.args.get("funnel") or None)

    @app.post("/api/calendar/funnel-drafts")
    def fd_create():
        x = b()
        return run(create, x.get("funnel"), x.get("subject", ""), x.get("body", ""), x.get("stage", ""), x.get("contact_ids"))

    @app.get("/api/calendar/funnel-drafts/<int:draft_id>")
    def fd_preview(draft_id):
        return run(preview, draft_id)

    @app.put("/api/calendar/funnel-drafts/<int:draft_id>")
    def fd_update(draft_id):
        x = b()
        return run(update, draft_id, x.get("subject"), x.get("body"), x.get("add_contact_ids"), x.get("remove_contact_ids"), x.get("stage"))

    @app.delete("/api/calendar/funnel-drafts/<int:draft_id>")
    def fd_delete(draft_id):
        return run(delete, draft_id)

    @app.post("/api/calendar/funnel-drafts/<int:draft_id>/send")
    def fd_send(draft_id):
        x = b()
        return run(send, draft_id, x.get("follow_up_days"), x.get("pause_seconds"))

    @app.get("/api/calendar/funnel-drafts/status")
    def fd_status():
        return run(status, request.args.get("draft_id", type=int))
