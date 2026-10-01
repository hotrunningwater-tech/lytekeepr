"""Ensures a usable (non-expired) access token for API calls, refreshing
via the stored refresh_token when needed. This is the only function Gmail
sync code should call — it never touches the client secret or the raw
token file directly."""

import time

from .errors import GoogleOAuthError
from .flow import refresh_access_token
from .store import load_tokens, save_tokens

REFRESH_MARGIN_SECONDS = 60


def get_valid_access_token() -> str:
    tokens = load_tokens()
    if not tokens or not tokens.get("refresh_token"):
        raise GoogleOAuthError("Gmail is not connected yet. Connect Gmail first.")

    expires_at = tokens.get("expires_at", 0)
    if tokens.get("access_token") and time.time() < expires_at - REFRESH_MARGIN_SECONDS:
        return tokens["access_token"]

    refreshed = refresh_access_token(tokens["refresh_token"])
    refreshed.setdefault("refresh_token", tokens["refresh_token"])
    save_tokens(refreshed)
    return refreshed["access_token"]
