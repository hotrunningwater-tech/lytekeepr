from __future__ import annotations

import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from gmail import storage as gmail_storage
from profiles.store import ensure_profile_bootstrap, list_profiles

PROFILE_DATA_ROOT = APP_DIR / "profiles" / "data"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _profile_root(storage_id: str) -> Path:
    return PROFILE_DATA_ROOT / storage_id


def _count_registry_entries(root: Path) -> int:
    payload = _read_json(root / "document-registry" / "registry.json")
    return len(payload) if isinstance(payload, dict) else 0


def _count_chronology_events(root: Path) -> int:
    payload = _read_json(root / "chronology-data" / "events.json")
    return len(payload) if isinstance(payload, dict) else 0


def _local_accounts_with_state(root: Path) -> list[str]:
    previous = gmail_storage.STORAGE_ROOT
    try:
        gmail_storage.STORAGE_ROOT = root / "email-storage"
        return gmail_storage.list_account_ids_with_state()
    finally:
        gmail_storage.STORAGE_ROOT = previous


def _component_dirs(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def main() -> None:
    ensure_profile_bootstrap()
    print("Profile isolation audit")
    for profile in list_profiles():
        root = _profile_root(str(profile.get("storage_id") or ""))
        print("---")
        print(f"profile_id={profile.get('profile_id')}")
        print(f"display_name={profile.get('display_name')}")
        print(f"storage_id={profile.get('storage_id')}")
        print(f"is_active={profile.get('is_active')}")
        print(f"attached_gmail_account_ids={profile.get('attached_gmail_account_ids') or []}")
        print(f"active_gmail_account_id={profile.get('active_gmail_account_id')}")
        print(f"local_accounts_with_state={_local_accounts_with_state(root)}")
        print(f"document_registry_entries={_count_registry_entries(root)}")
        print(f"chronology_events={_count_chronology_events(root)}")
        print(f"component_dirs={_component_dirs(root)}")


if __name__ == "__main__":
    main()
