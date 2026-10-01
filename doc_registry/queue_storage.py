"""Persistence for the user-confirmed scan queue — documents the user has
explicitly selected from the pending-documents view to actually download
and OCR. Separate from registry.json (which is a full rebuilt snapshot);
this is a small user-curated list that persists across registry rebuilds."""

import json

from .config import get_storage_dir


def _queue_path():
    return get_storage_dir() / "scan-queue.json"


def load_queue() -> list[dict]:
    path = _queue_path()
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def save_queue(queue: list[dict]) -> None:
    storage_dir = get_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    _queue_path().write_text(json.dumps(queue, indent=2))


def add_to_queue(entries: list[dict]) -> list[dict]:
    queue = load_queue()
    existing_ids = {item["id"] for item in queue}
    for entry in entries:
        if entry["id"] not in existing_ids:
            queue.append(entry)
            existing_ids.add(entry["id"])
    save_queue(queue)
    return queue


def remove_from_queue(ids: set[str]) -> list[dict]:
    queue = [item for item in load_queue() if item["id"] not in ids]
    save_queue(queue)
    return queue
