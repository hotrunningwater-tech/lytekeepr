"""Local token storage for Gmail OAuth.

Supports one active Gmail account at a time for live API calls while
persisting token sets separately per connected Gmail account so switching
profiles can restore prior local state safely.
"""

import json
from pathlib import Path
from profiles.store import get_active_profile, get_active_gmail_account_id, set_active_gmail_account_id

# Each workspace keeps its own Google tokens in <workspace>/google/. Before workspaces existed they lived next to this file; those are
# copied once into the first workspace and then never read again. The three names below stay None unless a test points them elsewhere.
LEGACY_DIR = Path(__file__).resolve().parent
STORE_PATH = None
TOKENS_DIR = None
ACTIVE_ACCOUNT_PATH = None


def _google_dir() -> Path:
    from profiles.store import get_active_profile_id, home_profile_id, profile_component_dir
    folder = profile_component_dir("google")
    if get_active_profile_id() == home_profile_id() and not (folder / "adopted-from.txt").exists():
        _adopt_legacy(folder)
    return folder


def _adopt_legacy(folder: Path) -> None:
    import os
    import shutil
    legacy_accounts = LEGACY_DIR / "accounts"
    (folder / "accounts").mkdir(parents=True, exist_ok=True)
    for src in sorted(legacy_accounts.glob("*.json")) if legacy_accounts.is_dir() else []:
        dst = folder / "accounts" / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
    for name in ("active-account.json", "tokens.json"):
        src, dst = LEGACY_DIR / name, folder / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
    (folder / "adopted-from.txt").write_text(str(LEGACY_DIR) + "\n")


def _store_path() -> Path:
    return STORE_PATH or _google_dir() / "tokens.json"


def _tokens_dir() -> Path:
    return TOKENS_DIR or _google_dir() / "accounts"


def _active_account_path() -> Path:
    return ACTIVE_ACCOUNT_PATH or _google_dir() / "active-account.json"


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
    return _tokens_dir() / f"{account_id}.json"


def list_token_account_ids() -> list[str]:
    folder = _tokens_dir()
    if not folder.is_dir():
        return []
    return sorted(path.stem for path in folder.glob("*.json") if path.is_file())


def load_active_account_id() -> str | None:
    profile_account_id = get_active_gmail_account_id()
    if profile_account_id:
        return profile_account_id

    active_profile = get_active_profile()
    if active_profile.get("profile_id"):
        return None

    payload = _read_json(_active_account_path()) or {}
    account_id = payload.get("account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def set_active_account_id(account_id: str) -> None:
    set_active_gmail_account_id(account_id)
    _write_json(_active_account_path(), {"account_id": account_id})


def clear_active_account_id() -> None:
    set_active_gmail_account_id(None)
    if _active_account_path().is_file():
        _active_account_path().unlink()


def save_tokens(data: dict, account_id: str | None = None) -> None:
    target_account_id = account_id or load_active_account_id()
    if target_account_id:
        _write_json(account_tokens_path(target_account_id), data)
        return
    _write_json(_store_path(), data)


def load_tokens(account_id: str | None = None) -> dict | None:
    target_account_id = account_id or load_active_account_id()
    if target_account_id:
        tokens = _read_json(account_tokens_path(target_account_id))
        if tokens:
            return tokens
    return _read_json(_store_path())


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
    if _store_path().is_file():
        _store_path().unlink()


def clear_legacy_tokens() -> None:
    if _store_path().is_file():
        _store_path().unlink()
