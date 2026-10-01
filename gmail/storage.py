"""
Local structured storage for synced Gmail messages:

email-storage/
  accounts/
    <gmail-account-id>/
      messages/           <- full records, APPROVED (saved) messages only
        <gmail-message-id>.json
      pending/            <- full records for candidates awaiting a save/
                             dismiss decision; deleted once resolved either way
        <gmail-message-id>.json
      index.json          <- lightweight index of approved messages only
      scan-state.json     <- one entry per message ever scanned: status
                             (saved/not_relevant/pending_review/dismissed),
                             relevance verdict, reason, historyId at scan time
      sync-state.json     <- account-level bookkeeping (historyId cursor, etc.)

No Gmail API calls or OpenAI logic here — pure file I/O. Every message
Gmail returns gets a scan-state entry (so it's never re-classified for no
reason), but only approved messages get a full record under messages/.
"""

import json
from pathlib import Path

from .config import STORAGE_ROOT
from profiles.store import profile_component_dir


def _storage_root() -> Path:
    if STORAGE_ROOT is not None:
        return STORAGE_ROOT
    return profile_component_dir("email-storage")


def account_dir(account_id: str) -> Path:
    return _storage_root() / "accounts" / account_id


def list_account_ids() -> list[str]:
    accounts_dir = _storage_root() / "accounts"
    if not accounts_dir.is_dir():
        return []
    return sorted(path.name for path in accounts_dir.iterdir() if path.is_dir())


def has_meaningful_account_state(account_id: str) -> bool:
    root = account_dir(account_id)
    if not root.is_dir():
        return False

    if (root / "index.json").is_file() or (root / "sync-state.json").is_file() or (root / "scan-state.json").is_file():
        return True

    if any((root / "messages").glob("*.json")):
        return True
    if any((root / "pending").glob("*.json")):
        return True
    if any((root / "cache").glob("*.json")):
        return True

    return False


def list_account_ids_with_state() -> list[str]:
    return [account_id for account_id in list_account_ids() if has_meaningful_account_state(account_id)]


def messages_dir(account_id: str) -> Path:
    d = account_dir(account_id) / "messages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir(account_id: str) -> Path:
    d = account_dir(account_id) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_message(account_id: str, message: dict) -> None:
    path = messages_dir(account_id) / f"{message['id']}.json"
    path.write_text(json.dumps(message, indent=2))


def load_message(account_id: str, message_id: str) -> dict | None:
    path = messages_dir(account_id) / f"{message_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def delete_message(account_id: str, message_id: str) -> None:
    path = messages_dir(account_id) / f"{message_id}.json"
    if path.is_file():
        path.unlink()


def load_all_messages(account_id: str) -> list[dict]:
    out = []
    for path in messages_dir(account_id).glob("*.json"):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def save_cached_message(account_id: str, message: dict) -> None:
    path = cache_dir(account_id) / f"{message['id']}.json"
    path.write_text(json.dumps(message, indent=2))


def load_cached_message(account_id: str, message_id: str) -> dict | None:
    path = cache_dir(account_id) / f"{message_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def index_path(account_id: str) -> Path:
    return account_dir(account_id) / "index.json"


def load_index(account_id: str) -> dict:
    path = index_path(account_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_index(account_id: str, index: dict) -> None:
    account_dir(account_id).mkdir(parents=True, exist_ok=True)
    index_path(account_id).write_text(json.dumps(index, indent=2))


def _index_entry(message: dict) -> dict:
    return {
        "id": message["id"],
        "threadId": message.get("threadId", ""),
        "from": message.get("from", {}),
        "to": message.get("to", []),
        "subject": message.get("subject", ""),
        "receivedAt": message.get("receivedAt", ""),
        "snippet": message.get("snippet", ""),
        "labelIds": message.get("labelIds", []),
        "isUnread": message.get("isUnread", False),
        "isStarred": message.get("isStarred", False),
        "hasAttachments": message.get("hasAttachments", False),
    }


def upsert_index_entry(account_id: str, message: dict) -> None:
    index = load_index(account_id)
    index[message["id"]] = _index_entry(message)
    save_index(account_id, index)


def remove_index_entry(account_id: str, message_id: str) -> None:
    index = load_index(account_id)
    if message_id in index:
        del index[message_id]
        save_index(account_id, index)


def sync_state_path(account_id: str) -> Path:
    return account_dir(account_id) / "sync-state.json"


def load_sync_state(account_id: str) -> dict:
    path = sync_state_path(account_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_sync_state(account_id: str, state: dict) -> None:
    account_dir(account_id).mkdir(parents=True, exist_ok=True)
    sync_state_path(account_id).write_text(json.dumps(state, indent=2))


# ---------- scan progress (live status for the in-flight /api/gmail/scan
# request — this is what lets the UI show "Scanning… 12/45" instead of a
# bare spinner, without needing a background job queue) ----------


def scan_progress_path(account_id: str) -> Path:
    return account_dir(account_id) / "scan-progress.json"


def save_scan_progress(account_id: str, progress: dict) -> None:
    account_dir(account_id).mkdir(parents=True, exist_ok=True)
    scan_progress_path(account_id).write_text(json.dumps(progress, indent=2))


def load_scan_progress(account_id: str) -> dict:
    path = scan_progress_path(account_id)
    if not path.is_file():
        return {"phase": "idle"}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"phase": "idle"}


def clear_scan_progress(account_id: str) -> None:
    path = scan_progress_path(account_id)
    if path.is_file():
        path.unlink()


# ---------- pending candidates (temporary, pre-approval) ----------


def pending_dir(account_id: str) -> Path:
    d = account_dir(account_id) / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_pending(account_id: str, message: dict) -> None:
    path = pending_dir(account_id) / f"{message['id']}.json"
    path.write_text(json.dumps(message, indent=2))


def load_pending(account_id: str, message_id: str) -> dict | None:
    path = pending_dir(account_id) / f"{message_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def delete_pending(account_id: str, message_id: str) -> None:
    path = pending_dir(account_id) / f"{message_id}.json"
    if path.is_file():
        path.unlink()


# ---------- scan-state (minimal per-message tracking, every message ever seen) ----------


def scan_state_path(account_id: str) -> Path:
    return account_dir(account_id) / "scan-state.json"


def load_scan_state(account_id: str) -> dict:
    path = scan_state_path(account_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_scan_state(account_id: str, scan_state: dict) -> None:
    account_dir(account_id).mkdir(parents=True, exist_ok=True)
    scan_state_path(account_id).write_text(json.dumps(scan_state, indent=2))


def upsert_scan_entry(account_id: str, message_id: str, entry: dict) -> None:
    scan_state = load_scan_state(account_id)
    scan_state[message_id] = entry
    save_scan_state(account_id, scan_state)


def get_pending_review_ids(account_id: str) -> list[str]:
    scan_state = load_scan_state(account_id)
    return [mid for mid, entry in scan_state.items() if entry.get("status") == "pending_review"]
