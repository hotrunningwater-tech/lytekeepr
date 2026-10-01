"""
Persistence for chronology data:

chronology-data/
  events.json            <- confirmed events, keyed by event id
  review-queue.json      <- uncertain/conflicting events awaiting user action
  extraction-state.json  <- which sources have already been processed, so
                             re-running extraction doesn't reprocess/duplicate

Pure file I/O — no extraction logic, no OpenAI calls.
"""

import json
from pathlib import Path

from .config import get_storage_dir


def _events_path() -> Path:
    return get_storage_dir() / "events.json"


def _review_queue_path() -> Path:
    return get_storage_dir() / "review-queue.json"


def _extraction_state_path() -> Path:
    return get_storage_dir() / "extraction-state.json"


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    get_storage_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_events() -> dict:
    return _load(_events_path())


def save_events(events: dict) -> None:
    _save(_events_path(), events)


def load_review_queue() -> dict:
    return _load(_review_queue_path())


def save_review_queue(queue: dict) -> None:
    _save(_review_queue_path(), queue)


def load_extraction_state() -> dict:
    return _load(_extraction_state_path())


def save_extraction_state(state: dict) -> None:
    _save(_extraction_state_path(), state)


def load_processed_sources() -> dict:
    state = load_extraction_state()
    processed = state.get("processedSources")
    if isinstance(processed, dict):
        return processed

    legacy_ids = state.get("processedSourceIds", [])
    if not isinstance(legacy_ids, list):
        return {}

    out = {}
    for source_key in legacy_ids:
        if not isinstance(source_key, str) or ":" not in source_key:
            continue
        source_type, source_id = source_key.split(":", 1)
        out[source_key] = {
            "sourceType": source_type,
            "sourceId": source_id,
            "sourceLabel": "",
            "processedAt": state.get("lastRunAt"),
        }
    return out
