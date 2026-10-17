"""A team member's own Google Calendar.

The workspace's Google connection (Gmail + Calendar) belongs to the admin. Each team member can connect their own Google
Calendar too: only calendar permissions are asked for (never Gmail), the tokens are kept in the workspace folder under
google/members/<user id>.json (readable only by the app), and google_sync uses them whenever that member is signed in, or
when the hourly job runs for them. Their meetings land in the same calendar, owned by them.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events")


def _folder():
    from profiles.store import profile_component_dir
    return profile_component_dir("google") / "members"


def _path(user_id: int):
    return _folder() / f"{int(user_id)}.json"


def load(user_id: int) -> dict | None:
    try:
        return json.loads(_path(user_id).read_text())
    except (OSError, ValueError):
        return None


def _save(user_id: int, data: dict) -> None:
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = _path(user_id)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def has_token(user_id: int) -> bool:
    return bool((load(user_id) or {}).get("refresh_token"))


def scopes(user_id: int) -> list:
    return ((load(user_id) or {}).get("scope") or "").split()


def remove(user_id: int) -> None:
    try:
        _path(user_id).unlink()
    except FileNotFoundError:
        pass


def connected_members() -> list[int]:
    """Active team members of this workspace who connected their own Google Calendar."""
    from . import store
    folder = _folder()
    if not folder.is_dir():
        return []
    with store._conn() as conn:
        active = {r["id"] for r in conn.execute("SELECT id FROM users WHERE role != 'admin' AND active = 1")}
    return sorted(int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit() and int(p.stem) in active and has_token(int(p.stem)))


def access_token(user_id: int) -> str:
    """A valid access token for this member, refreshed when needed. Raises google_sync's errors so callers handle them as usual."""
    from google_oauth.errors import GoogleOAuthError
    from google_oauth.flow import refresh_access_token
    from .google_sync import GoogleAuthError, GoogleSyncError
    tokens = load(user_id)
    if not tokens or not tokens.get("refresh_token"):
        raise GoogleAuthError("Your Google Calendar is not connected yet. Press Connect my Google Calendar.", never_connected=True)
    if tokens.get("access_token") and time.time() < tokens.get("expires_at", 0) - 60:
        return tokens["access_token"]
    try:
        fresh = refresh_access_token(tokens["refresh_token"])
    except GoogleOAuthError as exc:
        if "could not reach" in str(exc).lower():
            raise GoogleSyncError(f"Could not reach Google to refresh authorization: {exc}") from exc
        raise GoogleAuthError("Your Google Calendar connection expired. Press Reconnect Google.") from exc
    fresh.setdefault("refresh_token", tokens["refresh_token"])
    fresh.setdefault("scope", tokens.get("scope", ""))
    _save(user_id, fresh)
    return fresh["access_token"]


def _primary_calendar_email(access: str) -> str:
    """The email of the Google account that was just connected (its primary calendar's id)."""
    req = urllib.request.Request("https://www.googleapis.com/calendar/v3/calendars/primary?fields=id")
    req.add_header("Authorization", f"Bearer {access}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return (json.loads(resp.read().decode()).get("id") or "").lower()


def register(app):
    from flask import g, redirect, request

    def home(query: str = "") -> str:
        return os.getenv("LK_URL_PREFIX", "").rstrip("/") + "/" + (("?" + query) if query else "")

    def fail(reason: str):
        return redirect(home("calendar=error&reason=" + urllib.parse.quote(reason)))

    @app.get("/auth/google/calendar/start")
    def member_calendar_start():
        """A team member connects their own Google Calendar (calendar permissions only)."""
        from google_oauth.errors import GoogleOAuthError
        from google_oauth.flow import build_authorization_url
        from profiles.store import get_active_profile_id
        from itsdangerous import URLSafeTimedSerializer          # the same signed state the main Google connect uses
        signer = URLSafeTimedSerializer(os.getenv("LYTEKEEPR_SECRET_KEY") or os.getenv("LYTEKEEPR_PASSWORD") or "local-dev-only", salt="google-oauth-state")
        user = getattr(g, "user", None) or {}
        if not user.get("id"):
            return redirect(home())
        email = (user.get("email") or "").lower()
        state = signer.dumps({"p": "calendar", "w": get_active_profile_id(), "u": int(user["id"]), "e": email})
        try:
            return redirect(build_authorization_url(state=state, login_hint=email or None, scopes=CALENDAR_SCOPES))
        except GoogleOAuthError as exc:
            return fail(str(exc))

    def finish(code: str, purpose: dict):
        """Google's return trip for a member's calendar (runs inside the workspace named in the signed state)."""
        from google_oauth.errors import GoogleOAuthError
        from google_oauth.flow import exchange_code_for_tokens
        from . import store
        user_id, expected = int(purpose.get("u") or 0), (purpose.get("e") or "").lower()
        with store._conn() as conn:
            row = conn.execute("SELECT id FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
        if not row:
            return fail("That team member is not active in this workspace.")
        try:
            tokens = exchange_code_for_tokens(code, save=False)
            email = _primary_calendar_email(tokens.get("access_token", ""))
        except (GoogleOAuthError, urllib.error.URLError, ValueError) as exc:
            return fail(str(exc))
        if expected and email and email != expected:          # the calendar must be the Google account they sign in with
            return fail("Connect the Google account you sign in with: " + expected)
        _save(user_id, tokens)
        try:
            from . import google_sync
            with google_sync.as_member(user_id):
                google_sync.sync(trigger="connected")
        except Exception:                                       # the hourly job will try again; the connection itself is saved
            pass
        return redirect(home("calendar=connected"))

    app.config["LK_MEMBER_CALENDAR_FINISH"] = finish
