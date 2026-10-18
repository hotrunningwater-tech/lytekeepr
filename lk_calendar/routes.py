from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

from . import access, brands, users, chat, contact_ai, contacts, google_sync, leads, profile, store

bp = Blueprint("lk_calendar", __name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.errorhandler(store.ValidationError)
def _bad_request(err):
    return jsonify({"error": str(err)}), 400


def _one_per_meeting(events: list) -> list:
    """A meeting that is in several people's Google Calendars (you invited a team member, or both were invited) shows once:
    the viewer's own copy when they have one, else the first. Repeats keep one per date."""
    admin, me = users.is_admin(), users.current_id()
    def mine(e):
        return (e.get("external_id") or "").startswith("google:primary:") if admin else e.get("owner_id") == me
    best = {}
    for e in events:
        if not e.get("ical_uid"):
            continue
        k = (e["ical_uid"], e.get("date") or e.get("start_date"))
        if k not in best or (mine(e) and not mine(best[k])):
            best[k] = e
    return [e for e in events if not e.get("ical_uid") or best.get((e["ical_uid"], e.get("date") or e.get("start_date"))) is e]


def _with_people(events: list) -> list:
    """Whose each item is (who_id, who_name): the person who created it or whose Google Calendar it came from; the admin's otherwise."""
    with store._conn() as conn:
        rows = conn.execute("SELECT id, name, email, role FROM users").fetchall()
    names = {r["id"]: (r["name"] or (r["email"] or "").split("@")[0]) for r in rows}
    admin_id = next((r["id"] for r in rows if r["role"] == "admin"), None)
    for e in events:
        e["who_id"] = e.get("owner_id") or admin_id
        e["who_name"] = names.get(e["who_id"], "")
    return events


@bp.get("/api/calendar/events")
def list_events():
    start, end = request.args.get("start"), request.args.get("end")
    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400
    events = _one_per_meeting([e for e in store.occurrences(start, end) if access.can_event(e)])      # "Everything" sees the admin's Google meetings too
    brands.meeting_brands(events)
    return jsonify({"now": store.now_local().strftime("%Y-%m-%dT%H:%M"), "events": _with_people(events), "me": users.current_id()})


@bp.post("/api/calendar/overdue/move")
def move_overdue():
    return jsonify(store.move_overdue(str(_payload().get("to", "today"))))


@bp.get("/api/calendar/agenda")
def agenda():
    data = store.agenda()
    for key in ("overdue", "today", "upcoming"):
        data[key] = _with_people(_one_per_meeting([e for e in data[key] if access.can_event(e)]))
        brands.meeting_brands(data[key])
    data["badge"] = len(data["overdue"]) + sum(1 for o in data["today"] if not o["done"])
    return jsonify(data)


@bp.post("/api/calendar/events")
def create_event():
    body = _payload()
    store.check_not_past(str(body.get("start_date", "")), str(body.get("start_time", "")), bool(body.get("untimed")))
    event = store.create_event(body)
    mine = users.current_id()
    if mine and event.get("id"):                       # who created it: they always see their own events
        with store._conn() as conn:
            conn.execute("UPDATE events SET owner_id = ? WHERE id = ?", (mine, event["id"]))
    return jsonify(event), 201


@bp.put("/api/calendar/events/<int:event_id>")
def update_event(event_id):
    event = store.update_event(event_id, _payload())
    return (jsonify(event), 200) if event else (jsonify({"error": "not found"}), 404)


@bp.delete("/api/calendar/events/<int:event_id>")
def delete_event(event_id):
    return (jsonify({"ok": True}), 200) if store.delete_event(event_id) else (jsonify({"error": "not found"}), 404)


@bp.post("/api/calendar/events/<int:event_id>/done")
def mark_done(event_id):
    data = _payload()
    if not data.get("date"):
        return jsonify({"error": "date is required"}), 400
    ok = store.set_done(event_id, data["date"], bool(data.get("done", True)))
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": "not found"}), 404)


@bp.post("/api/calendar/events/done-bulk")
def done_bulk():
    data = _payload()
    items = data.get("items") or []
    n = 0
    for it in items[:500]:
        if store.set_done(int(it["id"]), str(it["date"]), bool(data.get("done", True))):
            n += 1
    return jsonify({"ok": True, "updated": n})


@bp.post("/api/calendar/followups/move")
def followups_move():
    data = _payload()
    ids = [int(i) for i in (data.get("lead_ids") or [])]
    return jsonify(leads.reschedule_follow_ups(str(data.get("to_date", "")), ids))


@bp.get("/api/calendar/leads")
def list_leads():
    rows = leads.list_leads(request.args.get("channel"))
    ids = access.person_ids()
    if ids is not None:
        rows = [r for r in rows if r.get("person_id") in ids]
    return jsonify({"leads": rows})


@bp.post("/api/calendar/leads/import")
def import_leads():
    channel = request.form.get("channel", "")
    if request.form.get("sample"):
        raw = leads.sample_csv(channel).encode()
    else:
        upload = request.files.get("file")
        if not upload:
            return jsonify({"error": "choose a CSV file"}), 400
        raw = upload.read()
    return jsonify(leads.import_csv(channel, raw))


@bp.put("/api/calendar/leads/<int:lead_id>")
def update_lead(lead_id):
    data = _payload()
    if data.get("follow_up_date") and not data.get("status"):          # only the next follow-up date (used by the Claude connector)
        lead = leads.set_follow_up(lead_id, str(data["follow_up_date"]))
        return (jsonify({"ok": True, "lead_id": lead_id, "follow_up_date": str(data["follow_up_date"])}), 200) if lead else (jsonify({"error": "not found"}), 404)
    lead = leads.set_status(lead_id, str(data.get("status", "")))
    return (jsonify(lead), 200) if lead else (jsonify({"error": "not found"}), 404)


@bp.get("/api/calendar/people/<int:person_id>/outreach")
def person_outreach(person_id):
    return jsonify(contacts.outreach(person_id))


@bp.post("/api/calendar/people/<int:person_id>/outreach")
def person_outreach_set(person_id):
    data = _payload()
    return jsonify(contacts.set_outreach(person_id, str(data.get("channel", "")), str(data.get("status", ""))))


@bp.get("/api/calendar/profile")
def profile_get():
    return jsonify(profile.current())


@bp.put("/api/calendar/profile")
def profile_put():
    name = str(_payload().get("name", "")).strip()[:80]
    store.set_setting("sender_name", name)
    return jsonify(profile.current())


@bp.post("/api/calendar/chat")
def chat_message():
    messages = chat.clean_messages(_payload().get("messages"))
    if messages is None:
        return jsonify({"error": "Send a non-empty list of {role, content} ending with a user message."}), 400
    try:
        out = chat.reply(messages)
        store.chat_append("user", messages[-1]["content"])      # saved only after a successful answer
        store.chat_append("assistant", out["reply"])
        return jsonify(out)
    except chat.RateLimited as exc:
        return jsonify({"error": str(exc)}), 429
    except chat.OpenAIServiceError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.post("/api/calendar/contact-ai")
def contact_ai_message():
    data = _payload()
    messages = chat.clean_messages(data.get("messages"))
    if messages is None:
        return jsonify({"error": "Send a non-empty list of {role, content} ending with a user message."}), 400
    try:
        if data.get("mode") == "ask" and data.get("person_id"):
            return jsonify(contact_ai.ask(messages, int(data["person_id"])))
        return jsonify(contact_ai.reply(messages, data.get("contact") if isinstance(data.get("contact"), dict) else {}, bool(data.get("pending"))))
    except chat.RateLimited as exc:
        return jsonify({"error": str(exc)}), 429
    except chat.OpenAIServiceError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.post("/api/calendar/chat/confirm")
def chat_confirm():
    data = _payload()
    result = chat.resolve_pending(str(data.get("id", "")), bool(data.get("approve")))
    store.chat_append("assistant", result.get("message", ""))
    return jsonify(result)


@bp.get("/api/calendar/chat/history")
def chat_history():
    return jsonify({"messages": store.chat_history()})


@bp.delete("/api/calendar/chat/history")
def chat_history_clear():
    return jsonify({"cleared": store.chat_clear()})


@bp.post("/api/calendar/google/sync")
def google_sync_now():
    try:
        return jsonify(google_sync.sync(trigger="manual"))
    except google_sync.GoogleAuthError as exc:
        return jsonify({"error": str(exc), "needs_reconnect": not exc.never_connected}), 502
    except google_sync.GoogleSyncError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.get("/api/calendar/google/calendars")
def google_calendars():
    try:
        return jsonify({"calendars": google_sync.list_calendars()})
    except google_sync.GoogleSyncError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.put("/api/calendar/google/calendars")
def google_calendars_choose():
    data = _payload()
    try:
        chosen = google_sync.choose_calendars(data.get("sync") or [], str(data.get("meetings") or ""))
        result = google_sync.sync(trigger="calendars")
    except google_sync.GoogleSyncError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"chosen": chosen, "sync": result})


@bp.get("/api/calendar/google/status")
def google_status():
    return jsonify(google_sync.status())


@bp.put("/api/calendar/events/<int:event_id>/meeting")
def update_meeting(event_id):
    data = _payload()
    event = store.set_meeting(event_id, data.get("meeting_status"), data.get("my_notes"))
    return (jsonify(event), 200) if event else (jsonify({"error": "not found"}), 404)


@bp.post("/api/calendar/import/scan")
def import_scan():
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "choose a file"}), 400
    return jsonify(contacts.scan(upload.read(), upload.filename or "file"))


@bp.post("/api/calendar/import/run")
def import_run():
    data = _payload()
    return jsonify(contacts.import_rows(str(data.get("token", "")), data.get("mapping") or {}, data.get("options") or {}))


@bp.get("/api/calendar/import/mappings")
def import_mappings():
    return jsonify({"mappings": contacts.list_mappings()})


@bp.delete("/api/calendar/import/mappings/<int:mapping_id>")
def import_mapping_delete(mapping_id):
    return (jsonify({"ok": True}), 200) if contacts.delete_mapping(mapping_id) else (jsonify({"error": "not found"}), 404)


@bp.get("/api/calendar/companies")
def companies_list():
    out = contacts.list_companies(request.args.get("q", ""))
    if not access.is_full():
        key = "companies" if isinstance(out, dict) and "companies" in out else None
        rows = out[key] if key else out
        rows = [c for c in rows if access.can_company(c.get("id") or c.get("company_id"))]
        out = {**out, key: rows} if key else rows
    return jsonify(out)


@bp.post("/api/calendar/companies")
def company_create():
    return jsonify(contacts.save_company(None, _payload())), 201


@bp.get("/api/calendar/companies/<int:company_id>")
def company_get(company_id):
    c = contacts.get_company(company_id)
    return (jsonify(c), 200) if c else (jsonify({"error": "not found"}), 404)


@bp.put("/api/calendar/companies/<int:company_id>")
def company_update(company_id):
    return jsonify(contacts.save_company(company_id, _payload()))


@bp.get("/api/calendar/contacts")
def contacts_list():
    rows = contacts.list_contacts(request.args.get("q", ""), request.args.get("company_id"), request.args.get("limit", 200))
    ids = access.person_ids()
    if ids is not None:
        rows = [c for c in rows if c.get("id") in ids]
    return jsonify({"contacts": rows})


@bp.post("/api/calendar/contacts")
def contact_create():
    return jsonify(contacts.create_contact(_payload())), 201


@bp.get("/api/calendar/contacts/duplicate")
def contact_duplicate():
    exclude = request.args.get("exclude", type=int)
    dup = contacts.find_duplicate(request.args.get("email", ""), request.args.get("url", ""), exclude, request.args.get("phone", ""))
    if dup and not access.can_person(dup["id"]):       # it exists, but this person may not see it: say so without showing who it is
        dup = {"id": dup["id"], "name": "a contact you cannot see", "company": "", "raw_notes": "", "reason": dup.get("reason", ""), "hidden": True}
    return jsonify({"duplicate": dup})


@bp.get("/api/calendar/duplicates")
def duplicates_list():
    groups = contacts.find_duplicate_groups()
    ids = access.person_ids()
    if ids is not None:
        groups = [g for g in groups if all(i in ids for i in g["ids"])]
    return jsonify({"groups": groups})


@bp.post("/api/calendar/duplicates/dismiss")
def duplicates_dismiss():
    contacts.dismiss_duplicates(_payload().get("ids") or [])
    return jsonify({"ok": True})


@bp.get("/api/calendar/contacts/<int:contact_id>")
def contact_get(contact_id):
    c = contacts.get_contact(contact_id)
    return (jsonify(c), 200) if c else (jsonify({"error": "not found"}), 404)


@bp.post("/api/calendar/contacts/<int:contact_id>/raw-notes")
def contact_raw_notes(contact_id):
    contacts.append_raw_notes(contact_id, str(_payload().get("text", "")))
    return jsonify({"ok": True})


@bp.delete("/api/calendar/contacts/<int:contact_id>")
def contact_delete(contact_id):
    return (jsonify({"ok": True}), 200) if contacts.delete_contact(contact_id) else (jsonify({"error": "not found"}), 404)


@bp.put("/api/calendar/contacts/<int:contact_id>")
def contact_update(contact_id):
    c = contacts.update_contact(contact_id, _payload())
    return (jsonify(c), 200) if c else (jsonify({"error": "not found"}), 404)


@bp.get("/lk-calendar/<path:subpath>")
def assets(subpath):
    mimetype = "text/javascript" if subpath.endswith(".js") else None
    response = send_from_directory(STATIC_DIR, subpath, mimetype=mimetype)
    response.headers["Cache-Control"] = "no-store"
    return response


def register(app):
    app.register_blueprint(bp)
