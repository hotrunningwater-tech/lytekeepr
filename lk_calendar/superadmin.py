"""The owner console (for the LyteKeepr owner; "super admin" in the code): every account, approving and suspending, billing
switches, health and errors, and a log of what the owner changed.

Only a super admin (see directory.super_admins) who is signed in as an admin gets in; everyone else gets a plain refusal.
It shows counts and status only: never anyone's contacts, emails or notes. Billing switches are recorded here; nothing is charged
until Stripe is connected (a later step).
"""

import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import g, jsonify, request, send_from_directory

from . import directory

log = logging.getLogger(__name__)
BACKUP_LOG = Path("/opt/lytekeepr-calendar/backups/backup.log")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _allowed() -> bool:
    user = getattr(g, "user", None)
    return bool(user) and user.get("role") == "admin" and directory.is_super_admin(user.get("email") or "")


def _me() -> str:
    return (getattr(g, "user", None) or {}).get("email") or "owner"


# ------------------------------------------------------------------ counts (read-only, per workspace)
def usage(profile_id: str) -> dict:
    """How much a workspace is used: counts only, read from its own databases."""
    from crm import db as crm_db
    from google_oauth.store import has_refresh_token
    from profiles.store import use_profile
    from . import google_sync, store
    out = {"contacts": 0, "funnels": 0, "emails_30d": 0, "team": 0, "last_active": "", "gmail": False, "google_reconnect": False, "last_gmail_sync": ""}
    try:
        with use_profile(profile_id):
            crm_db.init_db()
            conn = crm_db.get_connection()
            try:
                out["contacts"] = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
                out["funnels"] = conn.execute("SELECT COUNT(*) FROM funnels").fetchone()[0]
                since = (datetime.now() - timedelta(days=30)).isoformat()
                out["emails_30d"] = conn.execute("SELECT COUNT(*) FROM messages WHERE channel = 'email' AND direction = 'outgoing' AND timestamp >= ?",
                                                 (since,)).fetchone()[0]
            finally:
                conn.close()
            with store._conn() as c:
                out["team"] = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                out["last_active"] = c.execute("SELECT MAX(last_login_at) FROM users").fetchone()[0] or ""
            out["gmail"] = has_refresh_token()
            out["google_reconnect"] = google_sync.status()["needs_reconnect"]
            out["last_gmail_sync"] = store.get_state("gmail_last_daily_scan") or ""
    except Exception:
        log.exception("could not count usage for workspace %s", profile_id)
    return out


def _effective_billing(account: dict, settings: dict) -> str:
    if settings.get("waive_all") == "1" or not account.get("charge"):
        return "waived"
    return "charged"


# ------------------------------------------------------------------ health
def health() -> list[dict]:
    """A short traffic-light list: is the app answering, are backups running, is there disk space, do syncs work."""
    items = [{"name": "LyteKeepr app", "state": "ok", "detail": "answering"}]
    if BACKUP_LOG.exists():
        last = BACKUP_LOG.read_text().strip().splitlines()[-1:] or [""]
        stamp = last[0][:19]
        try:
            age_h = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 3600
        except ValueError:
            age_h = 999
        failed = "FAILED" in last[0]
        items.append({"name": "Nightly backup", "state": "bad" if failed or age_h > 30 else "ok", "detail": f"last run {stamp}" + (" (failed)" if failed else "")})
    else:
        items.append({"name": "Nightly backup", "state": "warn", "detail": "no backup log on this machine (only the server makes backups)"})
    disk = shutil.disk_usage(Path(__file__).resolve().parent)
    used = round(100 * (disk.total - disk.free) / disk.total)
    items.append({"name": "Disk space", "state": "bad" if used > 90 else "warn" if used > 80 else "ok", "detail": f"{used}% full"})
    accounts = [a for a in directory.list_accounts() if a["status"] == "active"]
    counts = {a["id"]: usage(a["profile_id"]) for a in accounts}
    no_gmail = [a["name"] or a["owner_email"] for a in accounts if not counts[a["id"]]["gmail"]]
    reconnect = [a["name"] or a["owner_email"] for a in accounts if counts[a["id"]]["google_reconnect"]]
    items.append({"name": "Google connections", "state": "warn" if reconnect else "ok",
                  "detail": ("needs reconnecting: " + ", ".join(reconnect)) if reconnect else f"{len(accounts) - len(no_gmail)} of {len(accounts)} active accounts have Gmail connected"})
    open_errors = directory.list_errors()
    items.append({"name": "Errors", "state": "bad" if open_errors else "ok", "detail": f"{len(open_errors)} open" if open_errors else "none open"})
    return items


# ------------------------------------------------------------------ errors: recorded from the app's own log, emailed to the owner
class _ErrorRecorder(logging.Handler):
    """Every ERROR the app logs becomes a row in the console (which account, what kind, a short message with emails removed)."""
    _busy = threading.local()

    def emit(self, record):
        if getattr(self._busy, "on", False) or record.name.startswith(__name__):
            return
        self._busy.on = True
        try:
            from profiles.store import _REQUEST_PROFILE, home_profile_id
            kind = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else record.name
            text = EMAIL_RE.sub("[email]", record.getMessage())[:240]
            profile_id = _REQUEST_PROFILE.get() or home_profile_id()
            error_id, new_today = directory.record_error(kind, text, profile_id)
            if new_today and directory.console_settings().get("alert_errors") == "1":
                _alert(kind, text, profile_id)
        except Exception:
            pass
        finally:
            self._busy.on = False


_alerts_sent: list[float] = []


def _alert(kind: str, text: str, profile_id: str) -> None:
    """Email the owner about a new kind of problem: at most one per kind per day, and at most 10 a day overall."""
    now = time.time()
    _alerts_sent[:] = [t for t in _alerts_sent if now - t < 86400]
    if len(_alerts_sent) >= 10:
        return
    _alerts_sent.append(now)

    def send():
        try:
            from email.mime.text import MIMEText
            from gmail import client as gmail_client
            from profiles.store import home_profile_id, use_profile
            account = directory.account_for_profile(profile_id) or {}
            with use_profile(home_profile_id()):
                for owner in sorted(directory.super_admins()):
                    mime = MIMEText(f"Something broke in LyteKeepr.\n\nAccount: {account.get('name') or account.get('owner_email') or profile_id}\n"
                                    f"Problem: {kind}\n{text}\n\nSee Health & errors in the owner console.\n")
                    mime["To"] = owner
                    mime["Subject"] = f"LyteKeepr problem: {kind}"
                    gmail_client.send_message(mime.as_bytes())
        except Exception:
            pass
    threading.Thread(target=send, name="console-alert", daemon=True).start()


# ------------------------------------------------------------------ routes
def register(app, app_dir):
    if not any(isinstance(h, _ErrorRecorder) for h in logging.getLogger().handlers):
        logging.getLogger().addHandler(_ErrorRecorder(level=logging.ERROR))      # the app's errors (they reach the root logger)

    def guard():
        return None if _allowed() else (jsonify({"error": "only the LyteKeepr owner can see this"}), 403)

    @app.get("/owner")
    @app.get("/superadmin")
    def superadmin_page():
        if not _allowed():
            return "This page is only for the LyteKeepr owner.", 403
        response = send_from_directory(app_dir, "superadmin.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/superadmin/overview")
    def console_overview():
        if (denied := guard()):
            return denied
        accounts = directory.list_accounts()
        settings = directory.console_settings()
        counts = {a["id"]: usage(a["profile_id"]) for a in accounts}
        week = (datetime.now() - timedelta(days=7)).isoformat()
        start = datetime.now().date() - timedelta(days=13)
        signups = [{"day": (start + timedelta(days=i)).isoformat(),
                    "count": sum(1 for a in accounts if (a["created_at"] or "")[:10] == (start + timedelta(days=i)).isoformat())} for i in range(14)]
        attention = [{"kind": "waiting", "text": f"{a['owner_email']} is waiting for approval", "account_id": a["id"]} for a in accounts if a["status"] == "waiting"]
        attention += [{"kind": "error", "text": f"{e['kind']}: {e['message'][:90]}", "account_id": e["account_id"]} for e in directory.list_errors()[:5]]
        attention += [{"kind": "google", "text": f"{a['name'] or a['owner_email']}: Google needs reconnecting", "account_id": a["id"]}
                      for a in accounts if counts[a["id"]]["google_reconnect"]]
        return jsonify({
            "accounts": len(accounts), "active": sum(1 for a in accounts if a["status"] == "active"),
            "waiting": sum(1 for a in accounts if a["status"] == "waiting"), "suspended": sum(1 for a in accounts if a["status"] == "suspended"),
            "people": sum(c["team"] for c in counts.values()), "active_7d": sum(1 for c in counts.values() if c["last_active"] >= week),
            "charged": sum(1 for a in accounts if _effective_billing(a, settings) == "charged"), "waive_all": settings["waive_all"] == "1",
            "open_errors": len(directory.list_errors()), "signups": signups, "attention": attention})

    @app.get("/api/superadmin/accounts")
    def superadmin_accounts():
        if (denied := guard()):
            return denied
        settings = directory.console_settings()
        out = []
        for a in directory.list_accounts():
            u = usage(a["profile_id"])
            out.append({**a, "contacts": u["contacts"], "team": u["team"], "last_active": u["last_active"], "gmail": u["gmail"],
                        "billing_effective": _effective_billing(a, settings)})
        return jsonify({"accounts": out})

    @app.get("/api/superadmin/accounts/<int:account_id>")
    def console_account(account_id):
        if (denied := guard()):
            return denied
        account = directory.get_account(account_id)
        if not account:
            return jsonify({"error": "no such account"}), 404
        from profiles.store import use_profile
        from . import store
        people = []
        with use_profile(account["profile_id"]):
            with store._conn() as c:
                people = [{"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"], "active": bool(r["active"]),
                           "last_login_at": r["last_login_at"]} for r in c.execute("SELECT * FROM users ORDER BY role = 'admin' DESC, email")]
        return jsonify({"account": {**account, "billing_effective": _effective_billing(account, directory.console_settings())},
                        "people": people, "usage": usage(account["profile_id"]),
                        "activity": [x for x in directory.list_activity(500) if x["account_id"] == account_id][:30]})

    @app.post("/api/superadmin/accounts/<int:account_id>/status")
    def superadmin_status(account_id):
        if (denied := guard()):
            return denied
        try:
            status = str((request.get_json(silent=True) or {}).get("status", ""))
            account = directory.set_status(account_id, status)
        except directory.DirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        directory.log_activity(_me(), {"active": "Approved / reactivated", "suspended": "Suspended", "waiting": "Set back to waiting"}[status]
                               + f" {account['owner_email']}", account_id)
        return jsonify({"account": account})

    @app.post("/api/superadmin/accounts/<int:account_id>/billing")
    def console_billing(account_id):
        if (denied := guard()):
            return denied
        body = request.get_json(silent=True) or {}
        fields = {}
        if "charge" in body:
            fields["charge"] = 1 if body["charge"] else 0
        if "card_required" in body:
            fields["card_required"] = 1 if body["card_required"] else 0
        if "plan" in body:
            fields["plan"] = str(body["plan"])[:40]
        if "monthly_price" in body:
            fields["monthly_price"] = str(body["monthly_price"])[:20]
        try:
            account = directory.update_account(account_id, **fields)
        except directory.DirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        said = {"charge": lambda v: "Charging turned " + ("on" if v else "off (waived)"), "card_required": lambda v: "Card on file " + ("required" if v else "not required"),
                "plan": lambda v: f"Plan set to {v}", "monthly_price": lambda v: f"Price set to {v}"}
        for k, v in fields.items():
            directory.log_activity(_me(), f"{said[k](v)} for {account['owner_email']}", account_id)
        return jsonify({"account": account})

    @app.post("/api/superadmin/accounts/<int:account_id>/notes")
    def console_notes(account_id):
        if (denied := guard()):
            return denied
        try:
            account = directory.update_account(account_id, notes=str((request.get_json(silent=True) or {}).get("notes", ""))[:4000])
        except directory.DirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        directory.log_activity(_me(), f"Edited private notes for {account['owner_email']}", account_id)
        return jsonify({"ok": True})

    @app.post("/api/superadmin/accounts/<int:account_id>/people/<int:user_id>/active")
    def console_person_active(account_id, user_id):
        if (denied := guard()):
            return denied
        account = directory.get_account(account_id)
        if not account:
            return jsonify({"error": "no such account"}), 404
        from profiles.store import use_profile
        from . import store, users
        active = bool((request.get_json(silent=True) or {}).get("active"))
        try:
            with use_profile(account["profile_id"]):
                person = users.set_active(user_id, active)
        except store.ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        directory.log_activity(_me(), f"Turned {'on' if active else 'off'} {person['email']} in {account['owner_email']}'s account", account_id)
        return jsonify({"ok": True})

    @app.get("/api/superadmin/settings")
    def console_settings_get():
        if (denied := guard()):
            return denied
        return jsonify({"settings": directory.console_settings(), "stripe_connected": False})

    @app.put("/api/superadmin/settings")
    def console_settings_put():
        if (denied := guard()):
            return denied
        body = request.get_json(silent=True) or {}
        names = {"waive_all": "Waive all charges", "card_for_new": "Ask new accounts for a card", "trial_days": "Free trial days", "alert_errors": "Email me about errors"}
        try:
            for key, value in body.items():
                directory.set_console_setting(key, value)
                said = ("turned on" if str(value) == "1" else "turned off") if key != "trial_days" else f"set to {value}"
                directory.log_activity(_me(), f"{names.get(key, key)} {said}")
        except directory.DirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"settings": directory.console_settings()})

    @app.get("/api/superadmin/health")
    def console_health():
        if (denied := guard()):
            return denied
        return jsonify({"services": health(), "errors": directory.list_errors(include_handled=request.args.get("all") == "1")})

    @app.post("/api/superadmin/errors/<int:error_id>/handled")
    def console_error_handled(error_id):
        if (denied := guard()):
            return denied
        directory.mark_error_handled(error_id)
        return jsonify({"ok": True})

    @app.get("/api/superadmin/activity")
    def console_activity():
        if (denied := guard()):
            return denied
        return jsonify({"activity": directory.list_activity()})
