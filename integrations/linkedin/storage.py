"""
Sync bookkeeping only (has a backfill run, when did we last sync) — the
actual people/messages data lives in the CRM database (crm/db.py) now,
not here. Kept as a tiny separate JSON file since this is sync-process
state, not CRM data itself.
"""

import json
from pathlib import Path

from profiles.store import profile_component_dir

_FILENAME = "sync-state.json"

_EMPTY_STATE = {
    "backfilled": False,
    "backfilled_at": None,
    "last_synced_at": None,
}


def _storage_path() -> Path:
    return profile_component_dir("linkedin-storage") / _FILENAME


def load() -> dict:
    path = _storage_path()
    if not path.exists():
        return dict(_EMPTY_STATE)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(_EMPTY_STATE)


def save(state: dict) -> None:
    path = _storage_path()
    path.write_text(json.dumps(state, indent=2))
