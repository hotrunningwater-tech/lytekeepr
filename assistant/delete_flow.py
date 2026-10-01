"""Chat-driven delete staging and execution for local scanned documents."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chronology import storage as chronology_storage
from doc_registry.scheduler import run_check_now
from profiles.store import profile_component_dir
from services.ocr.adapter import get_ocr_storage_root, list_recent_scans

APP_DIR = Path(__file__).resolve().parent.parent
PENDING_DELETE_TTL_MINUTES = 30


def _state_dir() -> Path:
    return profile_component_dir("assistant-state")


def _pending_delete_path() -> Path:
    return _state_dir() / "pending-delete.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _list_scanned_documents() -> list[dict]:
    return list_recent_scans(limit=None)


def _resolve_target(*, job_id: str | None = None, filename: str | None = None) -> tuple[dict | None, dict | None]:
    docs = _list_scanned_documents()
    if job_id:
        for doc in docs:
            if doc.get("job_id") == job_id.strip():
                return doc, None
        return None, {"status": "not_found", "message": f"No local scanned document found for job_id '{job_id}'."}

    name = _normalize(filename)
    if not name:
        return None, {"status": "invalid", "message": "Delete request needs a job_id or filename."}

    exact = [doc for doc in docs if _normalize(doc.get("filename")) == name]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, {
            "status": "ambiguous",
            "message": f"Multiple local scanned documents match '{filename}'. Use the exact job_id instead.",
            "matches": [
                {"job_id": doc.get("job_id"), "filename": doc.get("filename"), "page_count": doc.get("page_count", 0)}
                for doc in exact
            ],
        }

    partial = [doc for doc in docs if name in _normalize(doc.get("filename"))]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, {
            "status": "ambiguous",
            "message": f"More than one local scanned document partially matches '{filename}'. Use the exact filename or job_id.",
            "matches": [
                {"job_id": doc.get("job_id"), "filename": doc.get("filename"), "page_count": doc.get("page_count", 0)}
                for doc in partial[:5]
            ],
        }
    return None, {"status": "not_found", "message": f"No local scanned document found matching '{filename}'."}


def clear_pending_delete() -> None:
    try:
        _pending_delete_path().unlink()
    except FileNotFoundError:
        pass


def load_pending_delete() -> dict | None:
    payload = _load_json(_pending_delete_path())
    if not payload:
        return None
    expires_at = payload.get("expiresAt")
    if not isinstance(expires_at, str):
        clear_pending_delete()
        return None
    try:
        expires_dt = datetime.fromisoformat(expires_at)
    except ValueError:
        clear_pending_delete()
        return None
    if expires_dt <= _utc_now():
        clear_pending_delete()
        return None
    return payload


def create_pending_delete(*, job_id: str | None = None, filename: str | None = None) -> dict:
    target, error = _resolve_target(job_id=job_id, filename=filename)
    if error:
        return error

    created_at = _utc_now()
    payload = {
        "type": "delete_scanned_document",
        "target": {
            "job_id": target["job_id"],
            "filename": target["filename"],
            "page_count": target.get("page_count", 0),
            "status": target.get("status"),
        },
        "createdAt": created_at.isoformat(),
        "expiresAt": (created_at + timedelta(minutes=PENDING_DELETE_TTL_MINUTES)).isoformat(),
    }
    _save_json(_pending_delete_path(), payload)
    return {
        "status": "pending_confirmation",
        "message": (
            f"Ready to delete the local scanned document '{target['filename']}' "
            f"(job_id {target['job_id']}). Nothing has been deleted yet. "
            "Tell the user to type exact CONFIRM in chat to proceed."
        ),
        "target": payload["target"],
        "expiresAt": payload["expiresAt"],
    }


def _remove_processed_document_source(job_id: str) -> None:
    state = chronology_storage.load_extraction_state()
    processed_sources = chronology_storage.load_processed_sources()
    key = f"document:{job_id}"
    processed_sources.pop(key, None)
    legacy_ids = [source_id for source_id in state.get("processedSourceIds", []) if source_id != key]
    chronology_storage.save_extraction_state(
        {
            "processedSourceIds": sorted(set(legacy_ids) | set(processed_sources.keys())),
            "processedSources": processed_sources,
            "lastRunAt": state.get("lastRunAt"),
        }
    )


def delete_scanned_document(job_id: str) -> dict:
    job_dir = get_ocr_storage_root() / "jobs" / job_id
    if not job_dir.is_dir():
        return {"status": "not_found", "message": f"Local scanned document job '{job_id}' no longer exists."}

    filename = None
    result_payload = _load_json(job_dir / "results" / "result.json") or {}
    if isinstance(result_payload, dict):
        filename = result_payload.get("original_filename")

    shutil.rmtree(job_dir)
    _remove_processed_document_source(job_id)
    entries = run_check_now()

    return {
        "status": "deleted",
        "message": f"Deleted local scanned document '{filename or job_id}'.",
        "deleted": {"job_id": job_id, "filename": filename or job_id},
        "documentCounts": {
            "matched": sum(1 for entry in entries.values() if entry["status"] == "matched_scanned"),
            "pendingScan": sum(1 for entry in entries.values() if entry["status"] == "pending_scan"),
            "unresolved": sum(1 for entry in entries.values() if entry["status"] == "unresolved_local"),
        },
    }


def confirm_pending_delete() -> dict:
    payload = load_pending_delete()
    if not payload:
        return {
            "status": "no_pending_delete",
            "message": "There is no active delete request to confirm.",
        }

    target = payload.get("target") or {}
    job_id = target.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        clear_pending_delete()
        return {
            "status": "invalid_pending_delete",
            "message": "The pending delete request is invalid and was cleared.",
        }

    try:
        result = delete_scanned_document(job_id)
    finally:
        clear_pending_delete()
    return result
