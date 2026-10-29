"""What is coming up with one contact, for the top of their page: meetings they are invited to, tasks linked to them
("send Anna the demo", lead_ref "contact:<id>"), and their outreach follow-ups.

Only what is still ahead: a meeting whose start has passed never shows, even if nobody marked it done. A task or follow-up
that is overdue and not done still shows (it is still to do), marked overdue.
"""

import json
from datetime import datetime, timedelta

from . import access, store

DAYS_AHEAD = 60


def _is_meeting(e) -> bool:
    return e.get("kind") == "meeting" or e.get("source") == "google" or bool(e.get("has_call")) or bool(e.get("expect_recording"))


def for_person(person_id: int) -> dict:
    from . import contacts
    pid = int(person_id)
    now = store.now_local()
    today = now.date()
    with contacts._crm() as c:
        emails = {str(r[0]).strip().lower() for r in c.execute(
            "SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email' AND channel_identifier LIKE '%@%'", (pid,))}
    name = (contacts.get_contact(pid) or {}).get("name") or ""
    with store._conn() as conn:
        lead_refs = {f"lead:{r[0]}" for r in conn.execute("SELECT id FROM leads WHERE person_id = ?", (pid,))}
        oldest = conn.execute("""SELECT MIN(e.start_date) FROM events e LEFT JOIN completions d ON d.event_id = e.id AND d.occ_date = e.start_date
                                 WHERE e.repeat = 'none' AND d.event_id IS NULL AND (e.lead_ref = ? OR e.lead_ref IN ({}))""".format(
                                 ",".join("?" for _ in lead_refs) or "''"), (f"contact:{pid}", *lead_refs)).fetchone()[0]
    start = min(today, datetime.fromisoformat(oldest).date()) if oldest else today      # overdue tasks and follow-ups that are still open
    start = max(start, today - timedelta(days=store.MAX_RANGE_DAYS - DAYS_AHEAD - 1))
    meetings, tasks, follow_ups = [], [], []
    for e in store.occurrences(start.isoformat(), (today + timedelta(days=DAYS_AHEAD)).isoformat()):
        if e.get("done") or not access.can_event(e):
            continue
        ref = e.get("lead_ref") or ""
        if _is_meeting(e):
            if e.get("meeting_status") == "cancelled" or e.get("untimed"):
                continue
            begins = datetime.fromisoformat(f"{e['date']}T{e['start_time']}")
            if begins <= now:                                  # it has started or passed: never "coming up"
                continue
            guests = {str(a.get("email") or "").strip().lower() for a in json.loads(e.get("attendees") or "[]") if isinstance(a, dict)}
            if emails & guests:
                meetings.append({"item_id": e["id"], "title": e["title"], "date": e["date"], "time": e["start_time"], "join_url": e.get("join_url") or ""})
        elif ref == f"contact:{pid}" or ref in lead_refs:
            title = e["title"]
            if name and e.get("lead") and e["lead"].get("contact") and e["lead"]["contact"] in title:
                title = title.replace(e["lead"]["contact"], name)        # "Follow up: anna@x.com" reads "Follow up: Anna Lee"
            row = {"item_id": e["id"], "title": title, "date": e["date"], "time": "" if e.get("untimed") else e["start_time"],
                   "overdue": e["date"] < today.isoformat()}
            (tasks if ref.startswith("contact:") else follow_ups).append(row)
    return {"meetings": meetings, "tasks": tasks, "follow_ups": follow_ups}


def register(app):
    from flask import jsonify

    @app.get("/api/crm/people/<int:person_id>/coming-up")
    def person_coming_up(person_id):
        return jsonify(for_person(person_id))
