"""Meetings on autopilot, done by Claude on a schedule (every 2 hours). LyteKeepr only lists the work and keeps the results;
no AI runs here (Claude writes, on the user's own Claude plan, through the connector).
- Before a meeting: a prep for each meeting in the next 26 hours that has a contact as a guest and no prep yet. It goes into the
  person's private note on the meeting as Claude's prep section (meeting_notes.set_prep), the same place a prep asked for goes.
- After a meeting: once the Fathom transcript is in, the call summary (the call's own summary field) and a follow-up email draft.
  A transcript too short to mean anything (a test call) is listed as too short and left alone.
  A meeting with no recording is left alone until the user says what happened (Home asks 2 hours after it ends):
  "It happened" -> a follow-up draft from the notes and brief; "No-show" -> a short "sorry we missed you, reschedule?" draft.
  Recordings still on their way are listed as waiting, so nothing is written for them yet.
- Drafts (autopilot_drafts) are never sent: Home shows them under Calls to review; "Review & send" opens the usual send card.
  Emails go out from the admin's Gmail, so only the admin gets draft work.
Only the person's own meetings and calls, the same rule as Calls to review."""

import json
from datetime import datetime, timedelta

from . import access, meeting_notes, store

# The one prompt a customer pastes into a scheduled task in Claude (every 2 hours), shown in Settings and on the phone. It names no
# feature: the queue carries the instructions, so a new scheduled feature starts working without anyone pasting again.
PROMPT = ("Run the LyteKeepr autopilot: call autopilot_queue and follow the instructions it gives for each item, saving the results "
          "with the tools it names. Do not ask me anything and do not send any email; whatever needs me waits in LyteKeepr. "
          "If there is nothing to do, stop.")
DOES = ("Today: new emails and messages are read for interested people, messages waiting for your answer, and promises with deadlines; "
        "a welcome email draft and onboarding reminders when a deal is won; a prep in your private notes before each meeting with a contact; and after a recorded call the summary and a follow-up email draft (a \"want to reschedule?\" draft after a no-show).")
INSTRUCTIONS = {
    "meetings_needing_brief": "For each meeting: read get_contact and get_contact_history for its contacts (and get_meeting_notes), then write a short prep "
                              "(who they are, where things stand, the last conversations, open questions, what to aim for in this meeting). Save with save_meeting_prep.",
    "calls_needing_summary": "For each call: read it with get_call and summarize what was discussed, the decisions, and the next steps with who owns them. Save with save_call_summary.",
    "needing_followup_draft": "For each item: write a short follow-up email from what its `use` says, never inventing facts. Body without a subject line, "
                              "signed 'Best,' and [Your Name]. Save with save_followup_draft (call_id for a call, item_id for a meeting). It is not sent.",
    "too_short_to_summarize": "Nothing to do: too short to mean anything.",
    "waiting_for_recording": "Nothing to do yet: the recording may still arrive; the next run picks it up.",
    "asked_what_happened": "Nothing to do: the user tells LyteKeepr whether it happened.",
    "unsent_drafts": "Change nothing. In your summary of this run, list each one (who it is for, since when) so the user sees what is waiting to be sent.",
    "calls_without_contact": "Nothing to do: these recordings are not linked to a contact yet; the user links them first (on Home, or when they ask you about the call).",
}
BRIEF_HOURS = 26                # a 2-hourly run briefs every meeting well ahead of time
LOOK_BACK_DAYS = 7
MIN_TRANSCRIPT_CHARS = 300      # less than this is a test call or a dropped line
SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,              -- 'call:<call_id>' or 'event:<event_id>'
    kind TEXT NOT NULL,             -- 'call' | 'happened' | 'no_show'
    who TEXT NOT NULL,
    person_id INTEGER,
    to_email TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting',   -- waiting | dismissed
    created_at TEXT NOT NULL
);
"""


class AutopilotError(ValueError):
    pass


def _conn():
    conn = store._conn()
    conn.executescript(SCHEMA)
    return conn


def _mine(ev) -> bool:
    """The current person's own meeting (the rule Calls to review uses)."""
    from . import calls
    me = calls._me()
    return ev["owner_id"] == me if me else ev["owner_id"] not in calls._member_ids()


def _is_meeting(e) -> bool:
    return e.get("kind") == "meeting" or e.get("source") == "google" or bool(e.get("has_call")) or bool(e.get("expect_recording"))


def _guests(event_id: int) -> list:
    from . import calls
    return [a for a in calls.event_attendees(event_id) if a.get("person_id")]


def _first_email(person_ids) -> tuple:
    from . import contacts
    for pid in person_ids:
        info = contacts.get_contact(pid) or {}
        if info.get("email"):
            return pid, info["email"], info.get("name") or info["email"]
    return None, "", ""


def _has_draft(conn, ref: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM autopilot_drafts WHERE ref = ?", (ref,)).fetchone())


# ------------------------------------------------------------------ what Claude should do on this run
def queue() -> dict:
    from . import calls, users
    now = store.now_local()
    admin = users.is_admin()
    from . import inbox_watch, onboarding
    out = {"instructions": {**INSTRUCTIONS, "new_messages": inbox_watch.INSTRUCTIONS, "newly_won": onboarding.INSTRUCTIONS},
           "new_messages": inbox_watch.feed(), "newly_won": onboarding.newly_won(), "unsent_drafts": unsent(), "meetings_needing_brief": [], "calls_needing_summary": [], "too_short_to_summarize": [],
           "needing_followup_draft": [], "waiting_for_recording": [], "asked_what_happened": [], "calls_without_contact": []}
    with store._conn() as conn:
        meeting_notes._ready(conn)
        briefed = {r["meeting_key"] for r in conn.execute("SELECT meeting_key, text FROM meeting_private_notes WHERE who = ?", (meeting_notes._me(),))
                   if meeting_notes.split_prep(r["text"])[0]}
    for e in store.occurrences(now.date().isoformat(), (now + timedelta(hours=BRIEF_HOURS)).date().isoformat()):
        if not _is_meeting(e) or e.get("untimed") or e.get("meeting_status") == "cancelled" or not access.can_event(e) or not _mine(e):
            continue
        start = datetime.fromisoformat(f"{e['date']}T{e['start_time']}")
        if not (now < start <= now + timedelta(hours=BRIEF_HOURS)) or meeting_notes._key(e) in briefed:
            continue
        guests = _guests(e["id"])
        if guests:
            out["meetings_needing_brief"].append({"item_id": e["id"], "title": e["title"], "date": e["date"], "time": e["start_time"],
                                                  "contacts": [{"contact_id": g["person_id"], "name": g["contact_name"] or g.get("name") or g["email"]} for g in guests]})
    since = (now - timedelta(days=LOOK_BACK_DAYS)).strftime("%Y-%m-%dT%H:%M")
    cond, args = calls._mine_sql()
    with calls._crm() as c:
        rows = c.execute(f"""SELECT call_id, title, started_at, summary, transcript FROM calls
                             WHERE status = 'ready' AND transcript != '[]' AND started_at >= ? AND {cond} ORDER BY started_at""", (since, *args)).fetchall()
        people = calls._people_on(c, [r["call_id"] for r in rows])
        linked = {r["event_id"] for r in c.execute("SELECT event_id FROM calls WHERE event_id IS NOT NULL")}
    with _conn() as conn:
        for r in rows:
            item = {"call_id": r["call_id"], "title": r["title"], "started_at": r["started_at"],
                    "contacts": [{"contact_id": p["person_id"], "name": p["name"]} for p in people.get(r["call_id"], [])]}
            if not item["contacts"]:
                out["calls_without_contact"].append(item)          # the user links it first (Home, or when they ask Claude)
            elif not r["summary"].strip():
                text = calls.transcript_text({"transcript": json.loads(r["transcript"] or "[]")})
                out["too_short_to_summarize" if len(text) < MIN_TRANSCRIPT_CHARS else "calls_needing_summary"].append(item)
            elif admin and item["contacts"] and not _has_draft(conn, f"call:{r['call_id']}") and _first_email([p["contact_id"] for p in item["contacts"]])[1]:
                out["needing_followup_draft"].append({**item, "kind": "call", "use": "the call summary (get_call)"})
        for e in conn.execute("""SELECT * FROM events WHERE (has_call = 1 OR expect_recording = 1) AND start_date BETWEEN ? AND ?""",
                              ((now - timedelta(days=LOOK_BACK_DAYS)).date().isoformat(), now.date().isoformat())).fetchall():
            e = dict(e)
            if not _mine(e) or e["id"] in linked:
                continue
            end = datetime.fromisoformat(f"{e['start_date']}T{e['start_time']}") + timedelta(minutes=e["duration_min"])
            if end > now:
                continue
            brief = {"item_id": e["id"], "title": e["title"], "date": e["start_date"], "time": e["start_time"]}
            if e["meeting_status"] == "":
                key = "waiting_for_recording" if end > now - timedelta(hours=calls.NO_SHOW_AFTER_HOURS) else "asked_what_happened"
                out[key].append(brief)
            elif e["meeting_status"] in ("completed", "no_show") and admin and not _has_draft(conn, f"event:{e['id']}"):
                guests = _guests(e["id"])
                if guests and _first_email([g["person_id"] for g in guests])[1]:
                    out["needing_followup_draft"].append({**brief, "kind": "happened" if e["meeting_status"] == "completed" else "no_show",
                        "contacts": [{"contact_id": g["person_id"], "name": g["contact_name"]} for g in guests],
                        "use": "your private notes and the prep (get_meeting_notes)" if e["meeting_status"] == "completed"
                               else "nothing: a short, friendly 'sorry we missed each other, want to pick another time?'"})
    return out


# ------------------------------------------------------------------ saving what Claude wrote
def save_summary(call_id: int, summary: str, replace: bool = False, no_contact: bool = False) -> dict:
    """The call's summary (shown on the contact and in Calls to review). An earlier one is replaced only with replace=True (the user agreed)."""
    from . import calls
    call = calls.get_call(call_id)
    if not call:
        raise AutopilotError("No such call.")
    if not (summary or "").strip():
        raise AutopilotError("The summary is empty.")
    if not call.get("contacts") and not no_contact:       # a summary nobody's contact holds is lost a week later
        return {"saved": False, "needs_contact": True, "call_id": call_id, "title": call["title"],
                "tell_the_user": "This call is not linked to any contact. Ask who it was with (suggest from what they said or the names in the transcript), "
                                 "link it with link_call_to_contact after they confirm, then save the summary. Only if it was not with a contact, save with no_contact=true."}
    if (call.get("summary") or "").strip() and not replace:
        return {"saved": False, "already_summarized": True, "call_id": call_id, "title": call["title"], "existing_summary": call["summary"],
                "tell_the_user": "This call already has a summary (shown here). Ask if they want it replaced; save again with replace=true only after yes."}
    with calls._crm() as c:
        c.execute("UPDATE calls SET summary = ? WHERE call_id = ?", (summary.strip(), call_id))
    return {"call_id": call_id, "title": call["title"], "saved": True}


def save_draft(subject: str, body: str, call_id=None, item_id=None) -> dict:
    from . import calls, users
    if not users.is_admin():
        raise AutopilotError("Follow-up emails go out from the admin's Gmail, so only the admin's drafts are kept.")
    if not (body or "").strip():
        raise AutopilotError("The draft is empty.")
    if call_id:
        call = calls.get_call(int(call_id))
        if not call:
            raise AutopilotError("No such call.")
        ref, kind, title, pids = f"call:{call['call_id']}", "call", call["title"], [p["person_id"] for p in call["contacts"]]
    elif item_id:
        ev = meeting_notes._event(int(item_id))
        ref, kind, title = f"event:{ev['id']}", ("no_show" if ev.get("meeting_status") == "no_show" else "happened"), ev["title"]
        pids = [g["person_id"] for g in _guests(ev["id"])]
    else:
        raise AutopilotError("Give call_id or item_id.")
    pid, email, name = _first_email(pids)
    if not email:
        raise AutopilotError("No contact with an email address on that meeting.")
    return store_draft(ref, kind, pid, email, name, title, subject, body)


def store_draft(ref: str, kind: str, pid, email: str, name: str, title: str, subject: str, body: str) -> dict:
    """Keep a draft for Home (never sent). A newer draft for the same thing replaces an unread one."""
    try:
        from . import profile
        subject, body = profile.sign(subject or ""), profile.sign(body)
    except Exception:
        pass
    with _conn() as conn:
        conn.execute("DELETE FROM autopilot_drafts WHERE ref = ? AND state = 'waiting'", (ref,))
        did = conn.execute("""INSERT INTO autopilot_drafts (ref, kind, who, person_id, to_email, title, subject, body, created_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (ref, kind, meeting_notes._me(), pid, email, title, (subject or "").strip() or "Following up", body.strip(),
                            store.now_local().isoformat(timespec="minutes"))).lastrowid
    return {"draft_id": did, "to": f"{name} <{email}>", "saved": True, "sent": False}


# ------------------------------------------------------------------ a draft to one contact (Claude saves it as it writes; one per contact)
REMIND_AFTER_DAYS = 2


def save_contact_draft(person_id: int, subject: str, body: str) -> dict:
    """The latest version of an email to one contact: replaces the one before, shows in their activity as "Draft (not sent)", never sent here."""
    from . import access, contacts, users
    if not users.is_admin():
        raise AutopilotError("Emails go out from the admin's Gmail, so only the admin's drafts are kept.")
    if not access.can_person(person_id):
        raise AutopilotError("No such contact.")
    if not (body or "").strip():
        raise AutopilotError("The draft is empty.")
    info = contacts.get_contact(int(person_id)) or {}
    if not info.get("email"):
        raise AutopilotError(f"{info.get('name') or 'This contact'} has no email address; add one first.")
    recent = (store.now_local() - timedelta(minutes=15)).isoformat(timespec="minutes")
    signed = body.strip()
    try:
        from . import profile
        signed = profile.sign(body).strip()
    except Exception:
        pass
    with _conn() as conn:                                # the same text just saved for someone else: this is a group email
        twin = conn.execute("""SELECT 1 FROM autopilot_drafts WHERE kind = 'manual' AND state = 'waiting' AND person_id != ? AND body = ? AND created_at >= ?""",
                            (int(person_id), signed, recent)).fetchone()
    if twin:
        raise AutopilotError("The same email was just saved for someone else: for several people, save ONE draft with create_funnel_draft (contact_ids).")
    name = info.get("name") or info["email"]
    return store_draft(f"contact:{int(person_id)}", "manual", int(person_id), info["email"], name, f"Email to {name}", subject or "", body)


def contact_draft(person_id: int) -> dict | None:
    """The waiting draft(s) to this contact, newest first (their own, or one the scheduled run prepared for them)."""
    with _conn() as conn:
        r = conn.execute("SELECT * FROM autopilot_drafts WHERE person_id = ? AND state = 'waiting' ORDER BY created_at DESC LIMIT 1", (int(person_id),)).fetchone()
    return dict(r) if r else None


def draft_items(person_id: int) -> list:
    """Unsent drafts to this contact as entries of their activity (marked draft, next to their sent emails): one to them alone,
    and any several-people draft they are in and haven't been sent yet."""
    out = []
    d = contact_draft(person_id)
    if d:
        out.append({"message_id": f"draft:{d['draft_id']}", "draft_id": d["draft_id"], "draft": True, "channel": "email", "direction": "outgoing",
                    "sender": "You (draft, not sent)", "timestamp": d["created_at"],
                    "message_text": (f"Subject: {d['subject']}\n\n" if d["subject"] else "") + d["body"]})
    from . import funnel_drafts
    from assistant import bulk_email
    with funnel_drafts._conn() as conn:
        rows = conn.execute("SELECT * FROM funnel_drafts WHERE status IN ('draft', 'stopped')").fetchall()
        for r in rows:
            ids = json.loads(r["recipients"] or "[]")
            if int(person_id) not in ids or conn.execute("SELECT 1 FROM funnel_draft_sends WHERE draft_id = ? AND person_id = ? AND status = 'sent'",
                                                         (r["draft_id"], int(person_id))).fetchone():
                continue
            first = (funnel_drafts._people([int(person_id)]) or [{"first_name": ""}])[0]["first_name"]
            text = bulk_email.render(r["body"], first)
            out.append({"message_id": f"group-draft:{r['draft_id']}", "group_draft_id": r["draft_id"], "group_size": len(ids), "draft": True,
                        "as_reply": bool(r["as_reply"]), "channel": "email", "direction": "outgoing", "timestamp": r["updated_at"],
                        "sender": f"You (draft, not sent · group of {len(ids)})",
                        "message_text": (f"Subject: {bulk_email.render(r['subject'], first)}\n\n" if r["subject"] and not r["as_reply"] else "") + text})
    return out


def draft_item(person_id: int) -> dict | None:
    items = draft_items(person_id)
    return items[0] if items else None


def draft_sent(person_id) -> None:
    """An email went to this contact: their waiting drafts are done."""
    if not person_id:
        return
    with _conn() as conn:
        conn.execute("UPDATE autopilot_drafts SET state = 'sent' WHERE person_id = ? AND state = 'waiting'", (int(person_id),))
    from . import funnel_drafts
    funnel_drafts.person_emailed(person_id)           # and the several-people / reply drafts waiting on them


def unsent(days: int = REMIND_AFTER_DAYS) -> list:
    """Drafts left unsent for `days` days or more: one-contact drafts, the scheduled run's drafts, and funnel emails not sent yet."""
    from . import users
    if not users.is_admin():
        return []
    cutoff = (store.now_local() - timedelta(days=days)).isoformat(timespec="minutes")
    out = []
    with _conn() as conn:
        for r in conn.execute("SELECT * FROM autopilot_drafts WHERE state = 'waiting' AND created_at <= ? ORDER BY created_at", (cutoff,)):
            out.append({"kind": "contact" if r["kind"] == "manual" else r["kind"], "draft_id": r["draft_id"], "to": r["to_email"], "title": r["title"],
                        "subject": r["subject"], "since": r["created_at"][:10]})
    from . import funnel_drafts
    utc_cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    with funnel_drafts._conn() as conn:
        for r in conn.execute("SELECT * FROM funnel_drafts WHERE status IN ('draft', 'stopped') AND updated_at <= ? ORDER BY updated_at", (utc_cutoff,)):
            n = len(json.loads(r["recipients"] or "[]"))
            out.append({"kind": "funnel", "draft_id": r["draft_id"], "to": f"{n} {'person' if n == 1 else 'people'} in {r['funnel_name']}"
                        + (f" / {r['stage_name']}" if r["stage_name"] else ""), "title": r["subject"] or "Funnel email", "subject": r["subject"], "since": r["updated_at"][:10]})
    return out


def drafts() -> list:
    """The waiting drafts, for Home."""
    from . import users
    if not users.is_admin():
        return []
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM autopilot_drafts WHERE state = 'waiting' ORDER BY created_at DESC")]


def _admin_only():
    from . import users
    if not users.is_admin():
        raise AutopilotError("Only the admin can open follow-up drafts.")


def review(draft_id: int) -> dict:
    """Open a draft in the usual send card (edit, then Send now). Nothing is sent here."""
    from assistant import email_send_flow
    _admin_only()
    with _conn() as conn:
        d = conn.execute("SELECT * FROM autopilot_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    if not d:
        raise AutopilotError("No such draft.")
    result = email_send_flow.create_pending_send(person_name=d["to_email"], subject=d["subject"], body=d["body"])
    if result.get("status") != "pending_confirmation":
        raise AutopilotError(result.get("message") or "Could not open the draft.")
    return result                                    # it stays on Home until Dismiss, so closing the card loses nothing


def dismiss(draft_id: int) -> dict:
    _admin_only()
    with _conn() as conn:
        conn.execute("UPDATE autopilot_drafts SET state = 'dismissed' WHERE draft_id = ?", (draft_id,))
    return {"ok": True}


def register(app):
    from flask import jsonify, request

    def run(fn, *a, **k):
        try:
            return jsonify(fn(*a, **k))
        except (AutopilotError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    def b():
        return request.get_json(silent=True) or {}

    @app.get("/api/calendar/autopilot/prompt")
    def autopilot_prompt():
        return jsonify({"prompt": PROMPT, "every_hours": 2, "does": DOES})

    @app.get("/api/calendar/autopilot/queue")
    def autopilot_queue():
        return run(queue)

    @app.put("/api/calls/<int:call_id>/summary")
    def autopilot_summary(call_id):
        return run(save_summary, call_id, b().get("summary", ""), bool(b().get("replace")), bool(b().get("no_contact")))

    @app.post("/api/calendar/autopilot/drafts")
    def autopilot_draft_save():
        x = b()
        return run(save_draft, x.get("subject", ""), x.get("body", ""), call_id=x.get("call_id"), item_id=x.get("item_id"))

    @app.get("/api/crm/people/<int:person_id>/draft")
    def contact_draft_get(person_id):
        return run(lambda: {"draft": contact_draft(person_id)})

    @app.put("/api/crm/people/<int:person_id>/draft")
    def contact_draft_save(person_id):
        x = b()
        return run(save_contact_draft, person_id, x.get("subject", ""), x.get("body", ""))

    @app.get("/api/calendar/autopilot/unsent")
    def autopilot_unsent():
        return run(lambda: {"drafts": unsent()})

    @app.get("/api/calendar/autopilot/drafts")
    def autopilot_drafts():
        return run(lambda: {"drafts": drafts()})

    @app.post("/api/calendar/autopilot/drafts/<int:draft_id>/review")
    def autopilot_draft_review(draft_id):
        return run(review, draft_id)

    @app.post("/api/calendar/autopilot/drafts/<int:draft_id>/dismiss")
    def autopilot_draft_dismiss(draft_id):
        return run(dismiss, draft_id)
