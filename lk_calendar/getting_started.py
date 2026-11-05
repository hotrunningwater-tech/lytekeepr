"""The short "Get started" list on Home for someone new. Steps tick themselves when done. It goes away when every required step is done, or when dismissed. Kept per person."""

import json
import os
import urllib.parse

from . import store


def _claude_step() -> dict:
    """Claude's own install link opens its "Add custom connector" form with LyteKeepr's address filled in: USER clicks Add, then Allow."""
    from flask import request
    address = f"{request.headers.get('X-Forwarded-Proto') or request.scheme}://{request.host}{os.getenv('LK_URL_PREFIX', '').rstrip('/')}/mcp"
    link = "https://claude.ai/customize/connectors?" + urllib.parse.urlencode({"modal": "add-custom-connector", "connectorName": "LyteKeepr", "connectorUrl": address})
    return {"key": "claude", "title": "Connect Claude", "why": "Click Connect. In Claude, click Add, then Allow.", "done": _claude_connected(), "action": "claude", "link": link}


def _state_key() -> str:
    from . import users
    return f"getting_started:{'admin' if users.is_admin() else users.current_id()}"


def _state() -> dict:
    try:
        return json.loads(store.get_state(_state_key()) or "{}")
    except (ValueError, TypeError):
        return {}


def _claude_connected() -> bool:
    from profiles.store import get_active_profile_id
    from . import claude_connector, users
    me = users.current_id() or 0
    who = (me, 0) if users.is_admin() else (me, me)
    with claude_connector._db() as conn:
        return bool(conn.execute("SELECT 1 FROM oauth_tokens WHERE profile_id = ? AND user_id IN (?, ?) LIMIT 1", (get_active_profile_id(), *who)).fetchone())


def _linkedin_connected() -> bool:
    from profiles.store import get_active_profile_id
    from . import linkedin_link
    return bool(linkedin_link.current_link(get_active_profile_id()))


def _email_waiting():
    """How many emails wait for review, or None when no mailbox has been read yet."""
    try:
        from gmail import storage
        from gmail.sync import get_active_account_id
        account = get_active_account_id()
        if not account or not storage.load_scan_state(account):
            return None
        return len(storage.get_pending_review_ids(account))
    except Exception:
        return None


def _linkedin_waiting() -> int:
    from crm import db as crm_db
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        return len(crm_db.list_conversations(conn, "linkedin", "pending_review"))
    finally:
        conn.close()


def status() -> dict:
    """The USER's 7 setup steps (account, Claude, business, email, LinkedIn, calendar, a list), each ticking itself when done.
    Team members only connect their own Claude (and, optionally, Fathom): the mailbox, LinkedIn and funnels are the admin's."""
    from google_oauth.store import has_refresh_token
    from . import calls, contacts, google_sync, users
    state = _state()
    admin = users.is_admin()
    if admin:
        gmail = bool(has_refresh_token())
        with contacts._crm() as conn:
            funnels = conn.execute("SELECT COUNT(*) FROM funnels").fetchone()[0]
            imported = bool(conn.execute("SELECT 1 FROM people WHERE IFNULL(source_file, '') != '' LIMIT 1").fetchone())
        waiting = _email_waiting()
        linked = _linkedin_connected()
        li_waiting = _linkedin_waiting() if linked else 0
        cal = google_sync.status()
        steps = [
            {"key": "account", "title": "Create your account", "why": "Signed in with Google, with your Gmail connected.",
             "done": gmail, "action": "gmail"},
            _claude_step(),
            {"key": "business", "title": "Tell Claude about your business",
             "why": "In Claude, describe it in your own words: what you sell, to whom, and how you find clients. Claude sets up your funnel.",
             "done": funnels > 0, "say": ""},
            {"key": "email", "title": "Sort your email" + (f": {waiting} waiting" if waiting else ""),
             "why": "Your last 3 months of email are coming in." if waiting is None else 'In Claude, say: "sort my email".',
             "done": waiting == 0, "say": "sort my email" if waiting else ""},
            {"key": "linkedin", "title": "Connect LinkedIn" if not linked else "Sort your LinkedIn" + (f": {li_waiting} waiting" if li_waiting else ""),
             "why": ("Brings your LinkedIn messages into LyteKeepr, through the Claude app on this computer. Click Download to start." if not linked
                     else 'In Claude, say: "sort my LinkedIn".'),
             "done": linked and li_waiting == 0, "action": "linkedin" if not linked else "", "say": "sort my LinkedIn" if linked and li_waiting else ""},
            {"key": "calendar", "title": "Your calendar",
             "why": "Synced from your Google Calendar." if cal.get("last_sync") and not cal.get("needs_reconnect") else "Connect Google to bring in your calendar.",
             "done": bool(cal.get("last_sync")) and not cal.get("needs_reconnect"), "action": "" if cal.get("last_sync") and not cal.get("needs_reconnect") else "gmail"},
            {"key": "import", "title": "Import a list", "why": "Optional: a spreadsheet of contacts you already have.", "done": imported, "optional": True, "action": "crm"},
        ]
    else:
        steps = [_claude_step()]
    steps.append({"key": "fathom", "title": "Connect Fathom", "why": "Optional: your call recordings land on the right contact.", "done": bool(calls.get_key()),
                  "optional": True, "action": "settings:calls"})
    required = [s for s in steps if not s.get("optional")]
    finished = all(s["done"] for s in required)
    return {"steps": steps, "done": sum(1 for s in required if s["done"]), "total": len(required),
            "show": not finished and not state.get("dismissed")}


def update(tick=None, untick=None, dismissed=None) -> dict:
    state = _state()
    if dismissed is not None:
        state["dismissed"] = bool(dismissed)
    store.set_state(_state_key(), json.dumps(state))
    return status()


def register(app):
    from flask import jsonify, request

    @app.get("/api/calendar/getting-started")
    def getting_started():
        return jsonify(status())

    @app.post("/api/calendar/getting-started")
    def getting_started_update():
        x = request.get_json(silent=True) or {}
        return jsonify(update(x.get("tick"), x.get("untick"), x.get("dismissed")))
