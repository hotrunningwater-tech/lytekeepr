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
# Everything requested at sign-in, space-separated as Google expects. Existing Gmail
# access is unchanged; Calendar is read-only and nothing uses it yet.
REQUESTED_SCOPES = (GMAIL_MODIFY_SCOPE, CALENDAR_READONLY_SCOPE)


def build_authorization_url(state: str | None = None) -> str:
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
