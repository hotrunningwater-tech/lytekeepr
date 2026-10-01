"""Helpers for loading a Gmail message record with full attachment metadata."""

from . import client, storage
from .errors import GmailSyncError
from .parser import normalize_message
from .sync import get_active_account_id


def load_full_message(message_id: str) -> dict | None:
    account_id = get_active_account_id()
    if not account_id:
        return None

    stored = storage.load_message(account_id, message_id)
    if stored:
        return stored

    cached = storage.load_cached_message(account_id, message_id)
    if cached:
        return cached

    pending = storage.load_pending(account_id, message_id)
    if pending:
        return pending

    try:
        raw = client.get_message(message_id)
    except GmailSyncError:
        return None
    message = normalize_message(raw, account_id)
    storage.save_cached_message(account_id, message)
    return message
