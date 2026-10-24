"""Won deals and onboarding (P2), done by the scheduled Claude run.
- A funnel stage can be marked Won (in the stage editor, or by Claude). Reaching it = a won deal.
- Stage moves have no dates, so "newly won" is found by comparing: whoever is in a Won stage and was not there on an earlier check.
  When a stage is marked Won, the people already in it are noted as they are (no onboarding for old customers); after that, each
  newcomer is picked up by the next check (each autopilot run, every 2 hours).
- The run gets the newly won contacts in the queue (newly_won). Claude writes a welcome email (a draft, never sent; it waits on Home
  next to the other drafts) and up to a few onboarding reminders (tasks), saved with save_onboarding. A contact without an email
  gets only the reminders. The admin only (emails go out from the admin's Gmail).
Kept in won_seen (person_id, the stage, when noticed, whether handled)."""

from datetime import timedelta

from . import store

MAX_REMINDERS = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS won_seen (
    person_id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    handled INTEGER NOT NULL DEFAULT 0      -- 1 = onboarding prepared, or already a customer when the stage was marked Won
);
"""
INSTRUCTIONS = ("For each newly won contact: read get_contact and get_contact_history, then write a short, warm welcome email (what happens next, "
                "who to reach, nothing invented; body without a subject line, signed 'Best,' and [Your Name]) and 2 to 4 onboarding reminders for the user "
                "(e.g. 'Send onboarding guide', 'Check in after week one'), each with days_from_now. Save with save_onboarding. "
                "If the contact has no email, save only the reminders.")


class OnboardingError(ValueError):
    pass


def _conn():
    conn = store._conn()
    conn.executescript(SCHEMA)
    return conn


def _admin() -> bool:
    from . import users
    return users.is_admin()


def _won_people() -> list:
    from . import contacts
    with contacts._crm() as conn:
        return [dict(r) for r in conn.execute("""SELECT p.person_id, p.name, p.funnel_stage_id AS stage_id, s.name AS stage, f.name AS funnel
                                                 FROM people p JOIN funnel_stages s ON s.stage_id = p.funnel_stage_id JOIN funnels f ON f.funnel_id = p.funnel_id
                                                 WHERE s.is_won = 1""")]


def set_won(stage_id: int, won: bool) -> dict:
    """Mark a stage Won (or not). The people already in it count as old customers: no onboarding for them."""
    from . import contacts
    if not _admin():
        raise OnboardingError("Only the admin changes funnel stages.")
    with contacts._crm() as conn:
        if not conn.execute("SELECT 1 FROM funnel_stages WHERE stage_id = ?", (int(stage_id),)).fetchone():
            raise OnboardingError("No such stage.")
        conn.execute("UPDATE funnel_stages SET is_won = ? WHERE stage_id = ?", (1 if won else 0, int(stage_id)))
        here = [r["person_id"] for r in conn.execute("SELECT person_id FROM people WHERE funnel_stage_id = ?", (int(stage_id),))]
    if won:
        now = store.now_local().isoformat(timespec="minutes")
        with _conn() as conn:
            for pid in here:
                conn.execute("INSERT OR IGNORE INTO won_seen (person_id, stage_id, seen_at, handled) VALUES (?, ?, ?, 1)", (pid, int(stage_id), now))
    return {"stage_id": int(stage_id), "is_won": bool(won), "already_there": len(here)}


def newly_won() -> list:
    """Note newcomers to Won stages, and list the ones not prepared yet (for the queue)."""
    if not _admin():
        return []
    people = _won_people()
    now = store.now_local().isoformat(timespec="minutes")
    with _conn() as conn:
        for p in people:
            conn.execute("INSERT OR IGNORE INTO won_seen (person_id, stage_id, seen_at) VALUES (?, ?, ?)", (p["person_id"], p["stage_id"], now))
        waiting = {r["person_id"]: r["seen_at"] for r in conn.execute("SELECT person_id, seen_at FROM won_seen WHERE handled = 0")}
    from .autopilot import _first_email
    out = []
    for p in people:
        if p["person_id"] in waiting:
            out.append({"contact_id": p["person_id"], "name": p["name"], "funnel": p["funnel"], "stage": p["stage"],
                        "has_email": bool(_first_email([p["person_id"]])[1]), "won_noticed": waiting[p["person_id"]]})
    return out


def save(contact_id: int, subject: str, body: str, reminders: list) -> dict:
    from . import access, contacts
    from .autopilot import _first_email, store_draft
    if not _admin():
        raise OnboardingError("Only the admin's onboarding is prepared.")
    if not access.can_person(contact_id):
        raise OnboardingError("No such contact.")
    info = contacts.get_contact(int(contact_id)) or {}
    out = {"contact_id": int(contact_id), "draft": None, "reminders": []}
    pid, email, name = _first_email([int(contact_id)])
    if email and (body or "").strip():
        out["draft"] = store_draft(f"won:{int(contact_id)}", "welcome", pid, email, name, f"Welcome {info.get('name') or name}", subject or "Welcome", body)
    today = store.now_local().date()
    project = store.default_project(info.get("brand") or "")
    for r in (reminders or [])[:MAX_REMINDERS]:
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        days = max(0, min(int(r.get("days_from_now") or 0), 180))
        ev = store.create_event({"title": f"{title}: {info.get('name') or name or 'new customer'}", "project": project, "kind": "task",
                                 "start_date": (today + timedelta(days=days)).isoformat(), "start_time": "08:00", "untimed": True,
                                 "notes": "Onboarding (prepared by Claude after the deal was won)"})
        out["reminders"].append({"item_id": ev.get("id"), "title": ev.get("title"), "date": ev.get("start_date")})
    with _conn() as conn:
        conn.execute("UPDATE won_seen SET handled = 1 WHERE person_id = ?", (int(contact_id),))
    return out


def register(app):
    from flask import jsonify, request

    def run(fn, *a):
        try:
            return jsonify(fn(*a))
        except OnboardingError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/crm/funnels/stages/<int:stage_id>/won")
    def stage_won(stage_id):
        return run(set_won, stage_id, bool((request.get_json(silent=True) or {}).get("won")))

    @app.post("/api/calendar/autopilot/onboarding")
    def onboarding_save():
        b = request.get_json(silent=True) or {}
        return run(save, int(b.get("contact_id") or 0), b.get("subject", ""), b.get("body", ""), b.get("reminders") or [])
