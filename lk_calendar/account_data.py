"""A customer's own data: download everything as a zip of spreadsheets, and close the account themselves.
Only the workspace's admin can do either. Other people's Personal tasks, goals and documents are never in the download.
Closing works like deleting from the Owner console: nobody can sign in any more and the workspace folder moves to the deleted folder, which erases it for good after 30 days."""

import csv
import io
import json
import sqlite3
import zipfile

from . import store


def _sheet(rows: list, columns: list) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in columns])
    return out.getvalue()


def _table(conn, sql, args=()) -> list:
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):                # a new workspace: lists only exist once first used, so that sheet is empty
            return []
        raise


def build_zip() -> bytes:
    from . import contacts, docs, goals
    with contacts._crm() as conn:
        people = _table(conn, """SELECT p.person_id, p.name, p.job_title, p.phone, p.notes, p.brand, co.company_name AS company, fn.name AS funnel, fs.name AS stage,
                                        (SELECT group_concat(ca.channel || ': ' || ca.channel_identifier, '; ') FROM channel_accounts ca WHERE ca.person_id = p.person_id) AS channels
                                 FROM people p LEFT JOIN companies co ON co.company_id = p.company_id LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id
                                 LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id ORDER BY p.person_id""")
        names = {p["person_id"]: p["name"] for p in people}
        messages = _table(conn, "SELECT person_id, channel, direction, sender, timestamp, message_text FROM messages ORDER BY timestamp")
        records = _table(conn, "SELECT person_id, kind, channel, happened_at, who, text FROM contact_records WHERE deleted_at = '' ORDER BY happened_at")
        calls = _table(conn, "SELECT title, started_at, duration_min, platform, share_url, invitees, summary, transcript FROM calls ORDER BY started_at")
        documents = _table(conn, "SELECT * FROM docs WHERE deleted_at = '' ORDER BY doc_id")
        documents = [d for d in documents if docs._see(d)]
        ids = [d["doc_id"] for d in documents]
        tabs = _table(conn, f"SELECT doc_id, name, body FROM doc_tabs WHERE deleted_at = '' AND doc_id IN ({','.join('?' for _ in ids)})", ids) if ids else []
    for row in messages + records:
        row["contact"] = names.get(row["person_id"], "")
    for c in calls:
        c["transcript"] = "\n".join(f"{t.get('speaker')}: {t.get('text')}" for t in json.loads(c["transcript"] or "[]"))
        c["invitees"] = ", ".join(i.get("email") or "" for i in json.loads(c["invitees"] or "[]"))
    with store._conn() as cal:
        events = _table(cal, "SELECT * FROM events ORDER BY start_date, start_time")
    events = [e for e in events if store.is_mine(e)]
    with goals._conn() as conn:
        my_goals = _table(conn, "SELECT period, period_start, brand, text, done, done_at FROM goals WHERE who = ? AND deleted_at = '' ORDER BY goal_id", (goals._me(),))
    titles = {d["doc_id"]: d["title"] for d in documents}
    for t in tabs:
        t["document"] = titles.get(t["doc_id"], "")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("contacts.csv", _sheet(people, ["name", "company", "job_title", "phone", "channels", "brand", "funnel", "stage", "notes"]))
        z.writestr("messages.csv", _sheet(messages, ["contact", "channel", "direction", "sender", "timestamp", "message_text"]))
        z.writestr("notes_and_conversations.csv", _sheet(records, ["contact", "kind", "channel", "happened_at", "who", "text"]))
        z.writestr("calls.csv", _sheet(calls, ["title", "started_at", "duration_min", "platform", "share_url", "invitees", "summary", "transcript"]))
        z.writestr("tasks_and_meetings.csv", _sheet(events, ["title", "kind", "project", "start_date", "start_time", "duration_min", "repeat", "meeting_status", "notes"]))
        z.writestr("goals.csv", _sheet(my_goals, ["period", "period_start", "brand", "text", "done", "done_at"]))
        z.writestr("documents.csv", _sheet(documents, ["title", "brand", "created_at", "updated_at", "body"]))
        z.writestr("document_tabs.csv", _sheet(tabs, ["document", "name", "body"]))
    return buf.getvalue()


def register(app):
    from flask import jsonify, make_response, request, send_file
    from . import directory, users

    def only_admin():
        return None if users.is_admin() else (jsonify({"error": "Only the account's admin can do this."}), 403)

    def account():
        from profiles.store import get_active_profile_id, home_profile_id
        pid = get_active_profile_id()
        return None if pid == home_profile_id() else directory.account_for_profile(pid)

    @app.get("/api/account/export")
    def account_export():
        if (no := only_admin()):
            return no
        return send_file(io.BytesIO(build_zip()), mimetype="application/zip", as_attachment=True,
                         download_name=f"lytekeepr-export-{store.now_local():%Y-%m-%d}.zip")

    @app.get("/api/account/status")
    def account_status():
        if (no := only_admin()):
            return no
        acc = account()
        return jsonify({"can_close": bool(acc), "owner_email": (acc or {}).get("owner_email", "")})

    @app.post("/api/account/close")
    def account_close():
        if (no := only_admin()):
            return no
        acc = account()
        if not acc:
            return jsonify({"error": "This workspace can't be closed from here."}), 400
        typed = str((request.get_json(silent=True) or {}).get("confirm", "")).strip().lower()
        if typed != (acc["owner_email"] or "").lower():
            return jsonify({"error": "Type your account's email exactly to confirm."}), 400
        email = acc["owner_email"]
        directory.delete_account(acc["id"])
        directory.log_activity(email, f"Closed their own account {email} (data moved to the deleted folder)")
        response = make_response(jsonify({"ok": True}))
        import auth_gate
        response.delete_cookie(auth_gate.COOKIE, path=auth_gate.os.getenv("LK_URL_PREFIX", "").rstrip("/") or "/")
        return response
