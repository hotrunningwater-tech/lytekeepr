"""Local token storage for Gmail OAuth.

Supports one active Gmail account at a time for live API calls while
persisting token sets separately per connected Gmail account so switching
profiles can restore prior local state safely.
"""

import json
from pathlib import Path
from profiles.store import get_active_profile, get_active_gmail_account_id, set_active_gmail_account_id

STORE_PATH = Path(__file__).resolve().parent / "tokens.json"
TOKENS_DIR = Path(__file__).resolve().parent / "accounts"
ACTIVE_ACCOUNT_PATH = Path(__file__).resolve().parent / "active-account.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def account_tokens_path(account_id: str) -> Path:
    return TOKENS_DIR / f"{account_id}.json"


def list_token_account_ids() -> list[str]:
    if not TOKENS_DIR.is_dir():
        return []
    return sorted(path.stem for path in TOKENS_DIR.glob("*.json") if path.is_file())


def load_active_account_id() -> str | None:
    profile_account_id = get_active_gmail_account_id()
    if profile_account_id:
        return profile_account_id

    active_profile = get_active_profile()
    if active_profile.get("profile_id"):
        return None

    payload = _read_json(ACTIVE_ACCOUNT_PATH) or {}
    account_id = payload.get("account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def set_active_account_id(account_id: str) -> None:
    set_active_gmail_account_id(account_id)
    _write_json(ACTIVE_ACCOUNT_PATH, {"account_id": account_id})


def clear_active_account_id() -> None:
    set_active_gmail_account_id(None)
    if ACTIVE_ACCOUNT_PATH.is_file():
        ACTIVE_ACCOUNT_PATH.unlink()


def save_tokens(data: dict, account_id: str | None = None) -> None:
    target_account_id = account_id or load_active_account_id()
    if target_account_id:
        _write_json(account_tokens_path(target_account_id), data)
        return
    _write_json(STORE_PATH, data)


def load_tokens(account_id: str | None = None) -> dict | None:
    target_account_id = account_id or load_active_account_id()
    if target_account_id:
        tokens = _read_json(account_tokens_path(target_account_id))
        if tokens:
            return tokens
    return _read_json(STORE_PATH)


def has_refresh_token() -> bool:
    tokens = load_tokens()
    return bool(tokens and tokens.get("refresh_token"))


def account_has_refresh_token(account_id: str) -> bool:
    tokens = load_tokens(account_id)
    return bool(tokens and tokens.get("refresh_token"))


def clear_tokens(account_id: str | None = None) -> None:
    target_account_id = account_id or load_active_account_id()
    if target_account_id:
        path = account_tokens_path(target_account_id)
        if path.is_file():
            path.unlink()
        if load_active_account_id() == target_account_id:
            clear_active_account_id()
        return
    if STORE_PATH.is_file():
        STORE_PATH.unlink()


def clear_legacy_tokens() -> None:
    if STORE_PATH.is_file():
        STORE_PATH.unlink()
