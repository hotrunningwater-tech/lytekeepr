"""The short "Get started" list on Home for someone new. Steps tick themselves when done. It goes away when every required step is done, or when dismissed. Kept per person."""

import json
import os
import urllib.parse

from . import store


FATHOM_KEY_PAGE = "https://fathom.video/api_settings/new"      # Fathom's "Generate API key" page (it asks to log in first)


FATHOM_SETTINGS = "https://fathom.video/customize"          # Fathom's Settings page
RECORD_GUIDE = ["The app you use says Fully Enabled. If not, click Connect and allow it.",     # as Fathom's Settings page shows it
                "Auto-capture unscheduled meetings is on.",
                "Once it's set up, come back here and click It's set up."]


def _record_step(state: dict, recorded: bool) -> dict:
    """After Connect Fathom: Fathom set up to record the calls (Zoom, Google Meet or Teams). Done by the first recorded call, or by USER."""
    return {"key": "fathom_record", "title": "Enable Fathom for your Zoom, Google Meet or Teams calls",
            "why": "In Fathom's settings, under Video Conferencing:",
            "done": recorded or bool(state.get("fathom_record_done")), "action": "", "link": FATHOM_SETTINGS, "guide": RECORD_GUIDE}


def _tell_claude(sentence: str, finish: str, then: str) -> list:
    """The numbered lines under a step that is done by talking to Claude, so nobody has to guess what "Tell Claude" means."""
    return ["Click Tell Claude. Claude opens with \u201c" + sentence.strip() + "\u201d already typed (it's also copied, so you can paste it).",
            finish, then]


def _claude_step() -> dict:
    """Claude's own install link opens its "Add custom connector" form with LyteKeepr's address filled in: USER clicks Add, then Allow."""
    from flask import request
    address = f"{request.headers.get('X-Forwarded-Proto') or request.scheme}://{request.host}{os.getenv('LK_URL_PREFIX', '').rstrip('/')}/mcp"
    link = "https://claude.ai/customize/connectors?" + urllib.parse.urlencode({"modal": "add-custom-connector", "connectorName": "LyteKeepr", "connectorUrl": address})
    return {"key": "claude", "title": "Connect Claude", "why": "Click Connect. In Claude, click Add, then Allow.", "done": _claude_connected(), "action": "claude", "link": link}


def _import_step(dropped, done: bool, unorganized: bool) -> dict:
    """Drop a file on Home → Claude plans it (asks a question or two) → USER checks the plan on the Import page and clicks Import.
    A list imported by hand on CRM → Import is organized by Claude afterwards (the older way, kept)."""
    step = {"key": "import", "title": "Import a list", "why": "A spreadsheet of contacts you already have. No list? Click Skip.",
            "done": done, "skippable": True, "action": "" if done else "import"}
    if done:
        return step
    step["start_over"] = bool(dropped or unorganized)         # back to a plain "Import a list" with Start
    if dropped and dropped["plan"]:
        return {**step, "title": "Import a list: check and import", "action": "import_review",
                "link": f"{os.getenv('LK_URL_PREFIX', '').rstrip('/')}/?import={dropped['token']}",
                "why": "Claude's plan for " + dropped["name"] + " is ready. Click Review, check it, then click Import."}
    if dropped:
        say = "In LyteKeepr, organize the list I just dropped."
        return {**step, "title": "Import a list: let Claude organize it", "action": "", "say": say, "chat": True,
                "how": _tell_claude(say, "Send it. Claude asks a question or two (for example: leads or just contacts, which funnel). Answer them.",
                                    "Claude gives you a link. Check its plan on LyteKeepr's Import page, then click Import.")}
    if unorganized:
        say = "In LyteKeepr, organize the list I just imported."
        return {**step, "title": "Import a list: let Claude organize it", "action": "", "say": say, "chat": True,
                "how": _tell_claude(say, "Send it.", "Claude shows how many people go in each stage. Say yes, or tell it what to change.")}
    return step


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


def _linkedin_waiting_approve():
    """The add-on on this USER's computer waiting for Approve: the Approve page for Home's button, in this same window."""
    from flask import current_app
    from . import linkedin_link
    who = current_app.config.get("LK_LINKEDIN_WHO")
    row = linkedin_link.waiting_here(who()) if who else None
    return f"{os.getenv('LK_URL_PREFIX', '').rstrip('/')}/linkedin/approve?l={row['link_id']}" if row else ""


def _linkedin_status() -> dict:
    from profiles.store import get_active_profile_id
    from . import linkedin_link
    try:
        return linkedin_link.status(get_active_profile_id())
    except Exception:                                  # never in the way of Home
        return {}


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
        organized = imported and contacts.import_organized()
        from . import import_plans
        dropped = import_plans.waiting()
        list_done = import_plans.any_planned_import() or (imported and organized)
        waiting = _email_waiting()
        linked = _linkedin_connected()
        li_waiting = _linkedin_waiting() if linked else 0
        approve = "" if linked else _linkedin_waiting_approve()
        li = _linkedin_status() if linked else {}
        reading = linked and not li.get("first_done")             # linked, first read not finished yet: say so, never "done"
        cal = google_sync.status()
        sorted_once = set(state.get("sorted") or [])         # setup is once: a sorted inbox stays done; new people wait in the inbox, day to day
        if waiting == 0 and gmail:
            sorted_once.add("email")
        if linked and not reading and li_waiting == 0:
            sorted_once.add("linkedin")
        if sorted_once != set(state.get("sorted") or []):
            state["sorted"] = sorted(sorted_once)
            store.set_state(_state_key(), json.dumps(state))
        email_done = gmail and "email" in sorted_once
        li_done = linked and "linkedin" in sorted_once
        if email_done:
            waiting = 0
        if li_done:
            reading, li_waiting = False, 0
        steps = [
            {"key": "account", "title": "Create your account", "why": "Signed in with Google, with your Gmail connected.",
             "done": gmail, "action": "gmail"},
            _claude_step(),
            {"key": "business", "title": "Tell Claude about your business",
             "why": "Click Tell Claude, then finish the sentence in your own words: what you sell, to whom, and how you find clients. Claude sets up your funnel.",
             "done": funnels > 0, "say": "In LyteKeepr, set up my funnel. My business: ", "chat": True,
             "how": _tell_claude("In LyteKeepr, set up my funnel. My business:",
                                 "Finish the sentence in your own words: what you sell, to whom, and how you find clients. Then send it.",
                                 "Claude shows you the funnel it will create. Say yes, or tell it what to change.")},
            {"key": "email", "title": "Sort your email" + (f": {waiting} waiting" if waiting else ""),
             "why": "Your last 3 months of email are coming in. This list updates by itself." if waiting is None
                    else "Click Tell Claude: Claude suggests who to keep as contacts and who to remove, and asks you first.",
             "done": email_done, "busy": waiting is None, "say": "In LyteKeepr, sort my email." if waiting else "", "chat": True,
             "how": _tell_claude("In LyteKeepr, sort my email.", "Send it.",
                                 "Claude shows who to keep as contacts and who to remove. Ask about anyone you're not sure of, then say yes.") if waiting else []},
            {"key": "linkedin", "title": "Connect LinkedIn" if not linked else "Reading your LinkedIn" if reading
                                         else "Sort your LinkedIn" + (f": {li_waiting} waiting" if li_waiting else ""),
             "why": ("The add-on is installed. Click Approve to connect it to this account." if approve
                     else "Brings your LinkedIn messages into LyteKeepr, through the Claude app on this computer. Click Download to start." if not linked
                     else "LinkedIn wants you to log in: use the LinkedIn window on your computer, then close it." if reading and li.get("state") == "needs_login"
                     else "Bringing in your last 3 months of LinkedIn messages. This takes a few minutes; this list updates by itself." if reading
                     else "Click Tell Claude: Claude suggests who to keep as contacts and who to remove, and asks you first." if li_waiting
                     else "Your LinkedIn messages are in and sorted."),
             "done": li_done, "busy": reading,
             "action": "linkedin_approve" if approve else "linkedin" if not linked else "", "link": approve,
             "say": "In LyteKeepr, sort my LinkedIn." if linked and not reading and li_waiting else "", "chat": True,
             "how": _tell_claude("In LyteKeepr, sort my LinkedIn.", "Send it.",
                                 "Claude shows who to keep as contacts and who to remove. Ask about anyone you're not sure of, then say yes.")
                    if linked and not reading and li_waiting else []},
            {"key": "calendar", "title": "Your calendar",
             "why": "Synced from your Google Calendar." if cal.get("last_sync") and not cal.get("needs_reconnect") else "Connect Google to bring in your calendar.",
             "done": bool(cal.get("last_sync")) and not cal.get("needs_reconnect"), "action": "" if cal.get("last_sync") and not cal.get("needs_reconnect") else "gmail"},
            _import_step(dropped, list_done, imported and not organized and contacts.latest_import()["key"] != state.get("import_set_aside")),
        ]
    else:
        steps = [_claude_step()]
    steps.append({"key": "fathom", "title": "Connect Fathom", "why": "You'll need a key from your Fathom account.",   # Home: Log in to Fathom, then paste the key
                  "done": bool(calls.get_key()), "skippable": True, "action": "fathom", "link": FATHOM_KEY_PAGE})
    if calls.get_key():                                # Fathom connected: it must also be recording the calls
        steps.append(_record_step(state, calls.has_calls()))
    skipped = set(state.get("skipped") or [])
    for st in steps:                                   # a step USER skipped counts as finished, and says so
        st["skipped"] = bool(st.get("skippable") and not st["done"] and st["key"] in skipped)
    finished = all(st["done"] or st["skipped"] for st in steps)
    return {"steps": steps, "done": sum(1 for st in steps if st["done"] or st["skipped"]), "total": len(steps),
            "complete": finished, "show": not state.get("dismissed")}      # it never goes away by itself: "Setup complete" waits for USER's click


def update(tick=None, untick=None, dismissed=None, skip=None, unskip=None, start_over=None) -> dict:
    state = _state()
    if tick == "fathom_record":                        # "It's set up": the next call may be days away
        state["fathom_record_done"] = True
    if start_over == "import":                         # the dropped file goes; an imported list stops asking to be organized
        from . import contacts, import_plans, users
        if users.is_admin():
            import_plans.discard_waiting()
            state["import_set_aside"] = contacts.latest_import()["key"]
    if dismissed is not None:
        state["dismissed"] = bool(dismissed)
    skipped = set(state.get("skipped") or [])
    if skip:
        skipped.add(str(skip))
    if unskip:
        skipped.discard(str(unskip))
    state["skipped"] = sorted(skipped)
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
        return jsonify(update(x.get("tick"), x.get("untick"), x.get("dismissed"), x.get("skip"), x.get("unskip"), x.get("start_over")))

    @app.post("/api/calendar/getting-started/told")
    def getting_started_told():
        """USER clicked Tell Claude: the sentence heads Claude's calls in the Owner console. Only Home's own sentences."""
        from profiles.store import get_active_profile_id
        from . import directory
        said = (request.get_json(silent=True) or {}).get("say") or ""
        if said and any(s.get("say") == said for s in status()["steps"]):
            directory.log_ai_told(get_active_profile_id(), "Claude", said)
        return jsonify({"ok": True})
