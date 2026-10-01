"""Persistence for photographed-page attachment review state."""

import json
from datetime import datetime, timezone

from .config import get_storage_dir


def _state_path():
    return get_storage_dir() / "photo-review-state.json"


def load_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    storage_dir = get_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2))


def _key(message_id: str, attachment_id: str) -> str:
    return f"email:{message_id}:{attachment_id}"


def set_pending_review(message_id: str, attachment_id: str, review_job_id: str) -> None:
    state = load_state()
    state[_key(message_id, attachment_id)] = {
        "status": "pending_review",
        "reviewJobId": review_job_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)


def set_generated_pdf(message_id: str, attachment_id: str, review_job_id: str, generated_job_id: str) -> None:
    state = load_state()
    state[_key(message_id, attachment_id)] = {
        "status": "generated_pdf",
        "reviewJobId": review_job_id,
        "generatedJobId": generated_job_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
