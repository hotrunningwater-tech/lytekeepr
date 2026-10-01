"""Persistence for the document registry — a single JSON file, fully
rebuilt and overwritten on every check (see registry.build_registry). Keyed
by stable ids (email:<emailId>:<attachmentId> or local:<jobId>), so re-running
the check never produces duplicates — it's a snapshot, not an append log."""

import json

from .config import get_storage_dir


def _registry_path():
    return get_storage_dir() / "registry.json"


def _check_state_path():
    return get_storage_dir() / "check-state.json"


def _dismissed_path():
    return get_storage_dir() / "dismissed.json"


def save_registry(entries: dict) -> None:
    storage_dir = get_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(json.dumps(entries, indent=2))


def load_registry() -> dict:
    path = _registry_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_check_state() -> dict:
    path = _check_state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_check_state(state: dict) -> None:
    storage_dir = get_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    _check_state_path().write_text(json.dumps(state, indent=2))


def load_dismissed_ids() -> set[str]:
    """Registry entry ids the user explicitly removed from the attention
    list — persists across rebuilds, since build_registry recomputes the
    raw cross-reference from scratch every time and would otherwise keep
    resurfacing the same "still unmatched" attachment forever."""
    path = _dismissed_path()
    if not path.is_file():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def add_dismissed_ids(ids: list[str]) -> set[str]:
    dismissed = load_dismissed_ids()
    dismissed.update(ids)
    storage_dir = get_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    _dismissed_path().write_text(json.dumps(sorted(dismissed), indent=2))
    return dismissed
