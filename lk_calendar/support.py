"""Is it working, and who to tell when it isn't.

problems(): what has stopped syncing in this workspace (email, LinkedIn), in plain words with the one step that fixes it. Home shows it to
USER; the owner console's health list shows it for every account.
Help: the "Help" button in the header. USER writes what's wrong; it goes to the LyteKeepr owner by email (reply goes straight back to
USER) and into the console's errors list, with the account and its open errors attached.
"""

import logging
from datetime import datetime, timedelta

from . import store

log = logging.getLogger(__name__)
EMAIL_STALE_HOURS = 3           # email syncs every 15 minutes: 3 hours is a dozen missed turns
LINKEDIN_STALE_DAYS = 3         # LinkedIn syncs only while USER's computer is on: a weekend off is normal


def _ago(stamp: str) -> timedelta | None:
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    now = store.now_local()
    if then.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif then.tzinfo is not None and now.tzinfo is None:
        then = then.replace(tzinfo=None)
    return now - then


def _day(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).strftime("%b %-d, %-I:%M %p")
    except (TypeError, ValueError):
        return "a while"


def email_synced() -> None:
    """Called after every email sync that finished without an error."""
    store.set_state("gmail_last_ok", store.now_local().isoformat())


def problems() -> list[dict]:
    """The workspace's admin-level sync problems (Gmail and LinkedIn belong to the admin). Each: key, title, fix, action."""
    from google_oauth.store import has_refresh_token
    from profiles.store import get_active_profile_id
    from . import google_sync, linkedin_link
    out = []
    if has_refresh_token():
        if google_sync.status()["needs_reconnect"]:
            out.append({"key": "google", "title": "Google needs you to sign in again",
                        "fix": "Your email and calendar stopped updating. Click Reconnect and sign in with Google.", "action": "gmail"})
        else:
            last = store.get_state("gmail_last_ok") or ""
            if not last:                                     # first look since this check existed: start counting from now
                email_synced()
            elif (_ago(last) or timedelta(0)) > timedelta(hours=EMAIL_STALE_HOURS):
                out.append({"key": "email", "title": f"Email hasn't updated since {_day(last)}",
                            "fix": "Click Reconnect and sign in with Google. If that doesn't fix it, click Help and we'll sort it out.", "action": "gmail"})
    link = linkedin_link.current_link(get_active_profile_id())
    if link:
        state = linkedin_link.status(get_active_profile_id())
        seen = link["last_seen"] or link["connected_at"]
        if state["addon_update_needed"]:
            out.append({"key": "linkedin_update", "title": "LinkedIn needs the new add-on",
                        "fix": "Open the LinkedIn tab, click Update the add-on, then double-click the file and click Install in Claude.", "action": "linkedin"})
        if link["last_state"] == "needs_login":
            out.append({"key": "linkedin_login", "title": "LinkedIn is logged out",
                        "fix": f"Open the Claude app on {link['computer'] or 'your computer'} and log in to LinkedIn in the window that opens.", "action": "linkedin"})
        elif (_ago(seen) or timedelta(0)) > timedelta(days=LINKEDIN_STALE_DAYS):
            out.append({"key": "linkedin_quiet", "title": f"LinkedIn hasn't synced since {_day(seen)}",
                        "fix": f"Turn on {link['computer'] or 'the computer with LinkedIn'} and open the Claude app. It catches up by itself.", "action": "linkedin"})
        if state["outbox_problems"]:
            out.append({"key": "linkedin_send", "title": "A LinkedIn message may not have been sent",
                        "fix": "Open the LinkedIn tab to see which one, and check it on LinkedIn.", "action": "linkedin"})
    return out


def _send_help(text: str, user: dict, profile_id: str) -> None:
    from email.mime.text import MIMEText
    from gmail import client as gmail_client
    from profiles.store import home_profile_id, use_profile
    from . import directory
    account = directory.account_for_profile(profile_id) or {}
    open_errors = [e for e in directory.list_errors(limit=200) if e.get("account_id") == account.get("id") and e["kind"] != "Help request"][:5]
    with use_profile(profile_id):
        try:
            sync = problems()
        except Exception:
            sync = []
    body = (f"{user.get('name') or user.get('email')} ({user.get('email')}) asked for help.\n\n{text}\n\n"
            f"Account: {account.get('name') or account.get('owner_email') or profile_id}\n"
            f"Not syncing: {'; '.join(p['title'] for p in sync) or 'nothing'}\n"
            f"Open errors: {'; '.join(e['kind'] + ': ' + e['message'] for e in open_errors) or 'none'}\n\n"
            "Reply to this email to answer them.\n")
    with use_profile(home_profile_id()):
        for owner in sorted(directory.super_admins()):
            mime = MIMEText(body)
            mime["To"] = owner
            mime["Reply-To"] = user.get("email") or owner
            mime["Subject"] = f"LyteKeepr help: {account.get('name') or user.get('email')}"
            gmail_client.send_message(mime.as_bytes())


def register(app):
    from flask import g, jsonify, request
    from profiles.store import get_active_profile_id
    from . import directory, users

    @app.get("/api/calendar/sync-problems")
    def sync_problems():
        if not users.is_admin():
            return jsonify({"problems": []})
        try:
            return jsonify({"problems": problems()})
        except Exception:
            log.exception("could not check what is syncing")
            return jsonify({"problems": []})

    @app.post("/api/calendar/help")
    def ask_for_help():
        text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
        if len(text) < 3:
            return jsonify({"error": "Write a few words about what's wrong."}), 400
        user = getattr(g, "user", None) or {}
        profile_id = get_active_profile_id()
        directory.record_error("Help request", f"{user.get('email') or 'someone'}: {text[:400]}", profile_id)
        try:
            _send_help(text[:4000], user, profile_id)
        except Exception:
            log.warning("help request saved but the email to the owner failed", exc_info=True)
        return jsonify({"ok": True, "reply_to": user.get("email") or ""})
