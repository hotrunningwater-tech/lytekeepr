"""
Server-side Google OAuth (authorization code flow), stdlib only.

The client secret and any tokens never leave this module — the frontend
only ever sees a redirect URL to navigate to, and a boolean connected
status (see server.py's /auth/google/* and /api/google/status routes).
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import GoogleOAuthConfig
from .errors import GoogleOAuthError
from .store import save_tokens

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"      # create meetings (with a Meet link) from LyteKeepr
# Everything requested when Gmail/Google is connected, space-separated as Google expects. (Signing in only asks who you are.)
REQUESTED_SCOPES = (GMAIL_MODIFY_SCOPE, CALENDAR_READONLY_SCOPE, CALENDAR_EVENTS_SCOPE)


def build_authorization_url(state: str | None = None, login_hint: str | None = None) -> str:
    config = GoogleOAuthConfig.from_env().require()
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": " ".join(REQUESTED_SCOPES),
        "access_type": "offline",
        # Forces Google to always return a refresh_token, even on repeat
        # connections from the same account (otherwise it's only issued
        # the very first time an account grants access).
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state      # signed by the app; the callback refuses requests without a valid one
    if login_hint:
        params["login_hint"] = login_hint      # Google opens with this account already chosen (no account picker)
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    config = GoogleOAuthConfig.from_env().require()
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()

    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            tokens = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise GoogleOAuthError(f"Google rejected the authorization code: {body}") from exc
    except urllib.error.URLError as exc:
        raise GoogleOAuthError(f"Could not reach Google's token endpoint: {exc}") from exc

    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    save_tokens(tokens)
    return tokens


def refresh_access_token(refresh_token: str) -> dict:
    config = GoogleOAuthConfig.from_env().require()
    data = urllib.parse.urlencode(
        {
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "grant_type": "refresh_token",
        }
    ).encode()

    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            tokens = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise GoogleOAuthError(f"Google rejected the refresh token: {body}") from exc
    except urllib.error.URLError as exc:
        raise GoogleOAuthError(f"Could not reach Google's token endpoint: {exc}") from exc

    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    return tokens


# ---- Sign-in with Google ("who is this?"): only the email and name are asked for, and nothing is stored ----
LOGIN_SCOPES = ("openid", "email", "profile")
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


def build_login_url(state: str) -> str:
    config = GoogleOAuthConfig.from_env().require()
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "prompt": "select_account",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def login_identity(code: str) -> dict:
    """Trade the sign-in code for {email, email_verified, name}. The tokens are used once and thrown away (never saved)."""
    config = GoogleOAuthConfig.from_env().require()
    data = urllib.parse.urlencode({
        "code": code, "client_id": config.client_id, "client_secret": config.client_secret,
        "redirect_uri": config.redirect_uri, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            tokens = json.loads(response.read().decode())
        info_req = urllib.request.Request(USERINFO_ENDPOINT)
        info_req.add_header("Authorization", f"Bearer {tokens['access_token']}")
        with urllib.request.urlopen(info_req, timeout=15) as response:
            info = json.loads(response.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError) as exc:
        raise GoogleOAuthError(f"Google sign-in failed: {exc}") from exc
    return {"email": (info.get("email") or "").strip().lower(), "email_verified": bool(info.get("email_verified")), "name": info.get("name") or ""}
