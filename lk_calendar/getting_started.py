"""The short "Get started" list on Home for someone new. Steps tick themselves when done (the Claude-side step is ticked by hand, LyteKeepr can't
see it). It goes away when every required step is done, or when dismissed. Kept per person."""

import json

from . import store


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


def status() -> dict:
    from google_oauth.store import has_refresh_token
    from . import access, calls, contacts, users
    state = _state()
    ticked = state.get("ticked") or []
    ids = access.person_ids()
    with contacts._crm() as conn:
        has_contact = bool(conn.execute("SELECT 1 FROM people LIMIT 1").fetchone()) if ids is None else bool(ids)
    steps = []
    if users.is_admin():
        steps.append({"key": "gmail", "title": "Connect your Gmail", "why": "So your emails show on each contact's timeline.", "done": bool(has_refresh_token()), "action": "gmail"})
    steps += [
        {"key": "claude", "title": "Connect Claude", "why": "So you can ask Claude to work in LyteKeepr.", "done": _claude_connected(), "action": "settings:claude"},
        {"key": "claude_google", "title": "Turn off Claude's own Gmail and Google Calendar connectors", "why": "In Claude's settings. LyteKeepr replaces them, so Claude can't change the wrong calendar or email.",
         "done": "claude_google" in ticked, "manual": True},
        {"key": "contact", "title": "Add your first contact", "why": "Add one yourself, or import a list.", "done": has_contact, "action": "crm"},
        {"key": "fathom", "title": "Connect Fathom", "why": "Optional: your call recordings land on the right contact.", "done": bool(calls.get_key()), "optional": True, "action": "settings:calls"},
    ]
    required = [s for s in steps if not s.get("optional")]
    finished = all(s["done"] for s in required)
    return {"steps": steps, "done": sum(1 for s in required if s["done"]), "total": len(required),
            "show": not finished and not state.get("dismissed")}


def update(tick=None, untick=None, dismissed=None) -> dict:
    state = _state()
    ticked = set(state.get("ticked") or [])
    if tick == "claude_google":
        ticked.add(tick)
    if untick == "claude_google":
        ticked.discard(untick)
    state["ticked"] = sorted(ticked)
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
