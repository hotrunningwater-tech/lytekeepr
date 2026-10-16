from __future__ import annotations

import json
import os
import re
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PROFILE_ROOT = Path(os.getenv("LK_PROFILES_ROOT") or APP_DIR / "profiles")      # LK_PROFILES_ROOT: tests, or data kept outside the app
PROFILE_DATA_ROOT = PROFILE_ROOT / "data"
REGISTRY_PATH = PROFILE_ROOT / "registry.json"
ACTIVE_PROFILE_PATH = PROFILE_ROOT / "active-profile.json"
PROFILE_SYSTEM_VERSION = 1
VALID_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
VALID_STORAGE_ID_RE = re.compile(r"^\d{4}$")
LEGACY_STATE_DIRS = ("email-storage", "ocr-storage", "document-registry", "chronology-data", "assistant-state")
DEFAULT_PROFILE_ID = "profile-default"

# Each customer workspace (a USER) is one profile with its own data folder. A web request works inside exactly one of them: the
# login gate sets it at the start of the request (see auth_gate.py) and every data path (CRM, calendar, email, Google tokens,
# assistant state) is resolved from it. Outside a request (background jobs, scripts) the global active profile is used.
_REQUEST_PROFILE: ContextVar[str | None] = ContextVar("lytekeepr_request_profile", default=None)


def set_request_profile(profile_id: str | None):
    """Make this request work inside one workspace. Returns a token for reset_request_profile()."""
    return _REQUEST_PROFILE.set(_normalize_profile_id(profile_id) if profile_id else None)


def reset_request_profile(token) -> None:
    _REQUEST_PROFILE.reset(token)


@contextmanager
def use_profile(profile_id: str):
    """Run a block (a background job, a script) inside one workspace."""
    token = set_request_profile(profile_id)
    try:
        yield
    finally:
        _REQUEST_PROFILE.reset(token)


def home_profile_id() -> str:
    """The workspace the app was set up with (the global active profile). Until each person is mapped to their own workspace,
    everyone works here; background jobs also run here for now."""
    ensure_profile_bootstrap()
    active_profile_id = _load_active_profile_id()
    if active_profile_id:
        return active_profile_id
    registry = _load_registry()
    first = sorted(registry["profiles"].keys())[0]
    _save_active_profile_id(first)
    return first


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    if not cleaned:
        cleaned = "profile"
    if not cleaned.startswith("profile-"):
        cleaned = f"profile-{cleaned}"
    if len(cleaned) > 40:
        cleaned = cleaned[:40].rstrip("-_")
    return cleaned


def _normalize_profile_id(profile_id: str) -> str:
    candidate = (profile_id or "").strip().lower()
    if not VALID_PROFILE_ID_RE.fullmatch(candidate):
        raise ValueError("Profile ids must use 1-40 lowercase letters, numbers, hyphens, or underscores.")
    return candidate


def _load_registry() -> dict:
    payload = _read_json(REGISTRY_PATH) or {}
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    return {
        "version": int(payload.get("version") or PROFILE_SYSTEM_VERSION),
        "profiles": profiles,
    }


def _save_registry(payload: dict) -> None:
    _write_json(REGISTRY_PATH, payload)


def _load_active_profile_id() -> str | None:
    payload = _read_json(ACTIVE_PROFILE_PATH) or {}
    profile_id = payload.get("profile_id")
    return profile_id if isinstance(profile_id, str) and profile_id else None


def _save_active_profile_id(profile_id: str) -> None:
    _write_json(ACTIVE_PROFILE_PATH, {"profile_id": profile_id})


def _legacy_state_exists() -> bool:
    return any((APP_DIR / relative).exists() for relative in LEGACY_STATE_DIRS)


def _legacy_account_ids() -> list[str]:
    email_root = APP_DIR / "email-storage" / "accounts"
    if not email_root.is_dir():
        return []
    return sorted(path.name for path in email_root.iterdir() if path.is_dir())


def _legacy_active_account_id() -> str | None:
    payload = _read_json(APP_DIR / "google_oauth" / "active-account.json") or {}
    account_id = payload.get("account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def _storage_dir_name(profile: dict) -> str:
    storage_id = str(profile.get("storage_id") or "").strip()
    if VALID_STORAGE_ID_RE.fullmatch(storage_id):
        return storage_id
    legacy_profile_id = str(profile.get("profile_id") or "").strip()
    return legacy_profile_id


def _next_storage_id(registry: dict) -> str:
    used = set()
    for payload in registry.get("profiles", {}).values():
        raw = str(payload.get("storage_id") or "").strip()
        if VALID_STORAGE_ID_RE.fullmatch(raw):
            used.add(int(raw))
    next_value = 1
    while next_value in used:
        next_value += 1
    return f"{next_value:04d}"


def _profile_dir_from_payload(profile: dict) -> Path:
    return PROFILE_DATA_ROOT / _storage_dir_name(profile)


def _profile_payload(profile_id: str, display_name: str, *, storage_id: str) -> dict:
    return {
        "profile_id": profile_id,
        "display_name": display_name,
        "storage_id": storage_id,
        "created_at": _utc_now(),
        "last_active_at": _utc_now(),
        "attached_gmail_account_ids": [],
        "active_gmail_account_id": None,
    }


def _touch_profile(profile: dict) -> dict:
    profile["last_active_at"] = _utc_now()
    return profile



def ensure_profile_bootstrap() -> dict:
    registry = _load_registry()
    profiles = registry["profiles"]
    if profiles:
        PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
        PROFILE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        changed = False
        for profile_id in sorted(profiles.keys()):
            profile = profiles[profile_id]
            if not VALID_STORAGE_ID_RE.fullmatch(str(profile.get("storage_id") or "").strip()):
                profile["storage_id"] = _next_storage_id(registry)
                changed = True
            target_root = _profile_dir_from_payload(profile)
            legacy_root = PROFILE_DATA_ROOT / profile_id
            if legacy_root.exists() and legacy_root != target_root and not target_root.exists():
                shutil.move(str(legacy_root), str(target_root))
                changed = True
            target_root.mkdir(parents=True, exist_ok=True)
        active = _load_active_profile_id()
        if active and active in profiles:
            if changed:
                _save_registry(registry)
            return registry
        first = sorted(profiles.keys())[0]
        _save_active_profile_id(first)
        profiles[first] = _touch_profile(profiles[first])
        _save_registry(registry)
        return registry

    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    PROFILE_DATA_ROOT.mkdir(parents=True, exist_ok=True)

    if _legacy_state_exists():
        profile = _profile_payload(DEFAULT_PROFILE_ID, "Default profile", storage_id="0001")
        attached_accounts = _legacy_account_ids()
        profile["attached_gmail_account_ids"] = attached_accounts
        profile["active_gmail_account_id"] = _legacy_active_account_id() or (attached_accounts[0] if attached_accounts else None)
        profiles[DEFAULT_PROFILE_ID] = profile
        target_root = _profile_dir_from_payload(profile)
        target_root.mkdir(parents=True, exist_ok=True)
        for relative in LEGACY_STATE_DIRS:
            source = APP_DIR / relative
            if source.exists():
                shutil.move(str(source), str(target_root / relative))
        _save_registry(registry)
        _save_active_profile_id(DEFAULT_PROFILE_ID)
        return registry

    profile = _profile_payload(DEFAULT_PROFILE_ID, "Default profile", storage_id="0001")
    profiles[DEFAULT_PROFILE_ID] = profile
    _profile_dir_from_payload(profile).mkdir(parents=True, exist_ok=True)
    _save_registry(registry)
    _save_active_profile_id(DEFAULT_PROFILE_ID)
    return registry


def list_profiles() -> list[dict]:
    registry = ensure_profile_bootstrap()
    active_profile_id = get_active_profile_id()
    profiles = []
    for profile_id, payload in sorted(registry["profiles"].items()):
        item = dict(payload)
        item["is_active"] = profile_id == active_profile_id
        profiles.append(item)
    return profiles


def get_active_profile_id() -> str:
    """The workspace the current request (or job) works in."""
    current = _REQUEST_PROFILE.get()
    if current:
        return current
    return home_profile_id()


def get_active_profile() -> dict:
    profile_id = get_active_profile_id()
    registry = _load_registry()
    return dict(registry["profiles"][profile_id])


def get_profile_root(profile_id: str) -> Path:
    normalized = _normalize_profile_id(profile_id)
    registry = ensure_profile_bootstrap()
    profile = registry["profiles"].get(normalized)
    if not profile:
        raise ValueError(f"Unknown profile '{normalized}'.")
    return _profile_dir_from_payload(profile)


def get_active_profile_root() -> Path:
    return get_profile_root(get_active_profile_id())


def profile_component_dir(component: str, profile_id: str | None = None) -> Path:
    target_profile_id = profile_id or get_active_profile_id()
    root = get_profile_root(target_profile_id)
    path = root / component
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_profile(display_name: str, *, profile_id: str | None = None) -> dict:
    """A new workspace: its entry in the registry and its own empty data folder."""
    registry = ensure_profile_bootstrap()
    normalized_id = _normalize_profile_id(profile_id or _slugify(display_name))
    if normalized_id in registry["profiles"]:
        raise ValueError(f"Profile '{normalized_id}' already exists.")
    payload = _profile_payload(
        normalized_id,
        (display_name or normalized_id).strip() or normalized_id,
        storage_id=_next_storage_id(registry),
    )
    registry["profiles"][normalized_id] = payload
    _profile_dir_from_payload(payload).mkdir(parents=True, exist_ok=True)
    _save_registry(registry)
    return dict(payload)





def attach_gmail_account(profile_id: str | None, account_id: str) -> dict:
    normalized = _normalize_profile_id(profile_id or get_active_profile_id())
    registry = ensure_profile_bootstrap()
    profile = registry["profiles"].get(normalized)
    if not profile:
        raise ValueError(f"Unknown profile '{normalized}'.")
    accounts = list(profile.get("attached_gmail_account_ids") or [])
    if account_id not in accounts:
        accounts.append(account_id)
    profile["attached_gmail_account_ids"] = sorted(accounts)
    profile["active_gmail_account_id"] = account_id
    _save_registry(registry)
    return dict(profile)


def get_attached_gmail_account_ids(profile_id: str | None = None) -> list[str]:
    target_id = profile_id or get_active_profile_id()
    registry = ensure_profile_bootstrap()
    profile = registry["profiles"].get(target_id) or {}
    return list(profile.get("attached_gmail_account_ids") or [])


def get_active_gmail_account_id(profile_id: str | None = None) -> str | None:
    target_id = profile_id or get_active_profile_id()
    registry = ensure_profile_bootstrap()
    profile = registry["profiles"].get(target_id) or {}
    account_id = profile.get("active_gmail_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def set_active_gmail_account_id(account_id: str | None, profile_id: str | None = None) -> dict:
    target_id = profile_id or get_active_profile_id()
    registry = ensure_profile_bootstrap()
    profile = registry["profiles"].get(target_id)
    if not profile:
        raise ValueError(f"Unknown profile '{target_id}'.")
    attached = list(profile.get("attached_gmail_account_ids") or [])
    if account_id and account_id not in attached:
        attached.append(account_id)
    profile["attached_gmail_account_ids"] = sorted(attached)
    profile["active_gmail_account_id"] = account_id
    _save_registry(registry)
    return dict(profile)



