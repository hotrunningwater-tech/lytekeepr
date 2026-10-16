"""The name the user signs emails with. It is suggested from the connected Google account (the display name on the Gmail address),
but only used after the user confirms or edits it."""

import logging
import re
import time

from . import store

_last_try = 0.0


def refresh_suggested(force: bool = False) -> str:
    """Ask Gmail for the display name and keep it as a suggestion (never as the confirmed name). Tries at most once an hour unless forced."""
    global _last_try
    if store.get_setting("sender_name").strip():
        return ""
    if not force and time.time() - _last_try < 3600:
        return store.get_setting("sender_name_suggested")
    _last_try = time.time()
    try:
        from gmail import client as gmail_client
        name = gmail_client.get_sender_name()
    except Exception:
        logging.getLogger(__name__).info("could not read the name from the Google account")
        return store.get_setting("sender_name_suggested")
    if name:
        store.set_setting("sender_name_suggested", name[:80])
    return name


def current() -> dict:
    confirmed = store.get_setting("sender_name").strip()
    suggested = "" if confirmed else (store.get_setting("sender_name_suggested").strip() or refresh_suggested())
    return {"name": confirmed, "suggested": suggested}


_PLACEHOLDER = re.compile(r"\[\s*(?:your|my|sender'?s?)?\s*(?:full\s+)?name\s*\]|\{\{\s*(?:your\s+)?name\s*\}\}|<\s*your\s+name\s*>", re.I)


def sign(text: str) -> str:
    """AI text with any "[Your Name]" style placeholder replaced by the user's confirmed name. This is plain code, so it does not depend on the AI remembering."""
    name = store.get_setting("sender_name").strip()
    return _PLACEHOLDER.sub(lambda _m: name, text) if name and text else text
