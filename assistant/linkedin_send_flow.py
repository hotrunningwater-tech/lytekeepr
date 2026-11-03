"""Chat-driven draft + confirm staging for sending a real LinkedIn message.

Pending-action pattern: a tool call only
ever stages a draft (person + conversation_ref + text) to a local JSON file.
Nothing is actually sent to LinkedIn until confirm_pending_send() is called,
which only ever happens from an explicit user click in the UI (never from
chat text alone) — see /api/linkedin/send-pending/confirm in server.py.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crm import db as crm_db
from profiles.store import profile_component_dir

logger = logging.getLogger(__name__)

PENDING_SEND_TTL_MINUTES = 30


def _pending_send_path() -> Path:
    return profile_component_dir("assistant-state") / "pending-linkedin-send.json"


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


def clear_pending_send() -> None:
    try:
        _pending_send_path().unlink()
    except FileNotFoundError:
        pass


def load_pending_send() -> dict | None:
    payload = _load_json(_pending_send_path())
    if not payload:
        return None
    expires_at = payload.get("expiresAt")
    if not isinstance(expires_at, str):
        clear_pending_send()
        return None
    try:
        expires_dt = datetime.fromisoformat(expires_at)
    except ValueError:
        clear_pending_send()
        return None
    if expires_dt <= _utc_now():
        clear_pending_send()
        return None
    return payload


def resolve_linkedin_contact(person_name: str) -> dict:
    """Resolves a name against the CRM's LinkedIn contacts, tolerating small
    typos via fuzzy fallback. Returns {"status": "found", "person_name", "thread_id"}
    or {"status": "not_found"/"ambiguous", "message"}. Shared by the
    identity-lookup tool and the draft tool so a name is resolved the same
    way whether or not content is known yet."""
    conn = crm_db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT p.person_id, p.name, m.thread_id
            FROM people p
            JOIN messages m ON m.person_id = p.person_id AND m.channel = 'linkedin'
            WHERE p.name LIKE ? AND m.thread_id IS NOT NULL
            """,
            (f"%{person_name}%",),
        ).fetchall()

        # No substring match — likely a typo. Fall back to fuzzy-matching
        # against every known LinkedIn contact name rather than immediately
        # bouncing back to the user for an exact respelling.
        if not rows:
            all_rows = conn.execute(
                """
                SELECT DISTINCT p.person_id, p.name, m.thread_id
                FROM people p
                JOIN messages m ON m.person_id = p.person_id AND m.channel = 'linkedin'
                WHERE m.thread_id IS NOT NULL
                """
            ).fetchall()
            typed = person_name.strip().lower()
            best_row, best_ratio = None, 0.0
            for r in all_rows:
                candidate_words = r["name"].strip().lower().split() + [r["name"].strip().lower()]
                row_best = max(difflib.SequenceMatcher(None, typed, word).ratio() for word in candidate_words)
                if row_best > best_ratio:
                    best_row, best_ratio = r, row_best
            if best_row is not None and best_ratio >= 0.75:
                rows = [best_row]
    finally:
        conn.close()

    distinct_people = {row["person_id"]: row for row in rows}
    if not distinct_people:
        # No conversation yet: someone with a LinkedIn profile in LyteKeepr can still get a first message.
        from lk_calendar import linkedin_outbox
        conn = crm_db.get_connection()
        try:
            for r in conn.execute("SELECT DISTINCT p.person_id, p.name FROM people p JOIN channel_accounts ca ON ca.person_id = p.person_id AND ca.channel = 'linkedin' "
                                  "WHERE p.name LIKE ?", (f"%{person_name}%",)).fetchall():
                if linkedin_outbox.profile_slug(conn, r["person_id"]):
                    distinct_people[r["person_id"]] = {"person_id": r["person_id"], "name": r["name"], "thread_id": None}
        finally:
            conn.close()
    if not distinct_people:
        return {
            "status": "not_found",
            "message": f"No LinkedIn contact matching '{person_name}'.",
        }
    if len(distinct_people) > 1:
        names = sorted({row["name"] for row in distinct_people.values()})
        return {
            "status": "ambiguous",
            "message": f"More than one LinkedIn contact matches '{person_name}': {', '.join(names)}. Ask the user which one.",
        }

    row = next(iter(distinct_people.values()))
    return {"status": "found", "person_id": row["person_id"], "person_name": row["name"], "thread_id": row["thread_id"]}


def create_pending_send(person_name: str, message_text: str) -> dict:
    """Stages a draft once both a resolved contact and real content are
    known. Requires an unambiguous single match with a known conversation
    thread; anything else is reported back to the model instead of guessing."""
    if not message_text or not message_text.strip():
        return {"status": "invalid", "message": "Draft needs non-empty message text."}

    resolved = resolve_linkedin_contact(person_name)
    if resolved["status"] != "found":
        return resolved

    row = {"person_id": resolved["person_id"], "name": resolved["person_name"], "thread_id": resolved["thread_id"]}
    created_at = _utc_now()
    payload = {
        "type": "linkedin_send",
        "person_id": row["person_id"],
        "person_name": row["name"],
        "conversation_ref": row["thread_id"],
        "message_text": message_text.strip(),
        "createdAt": created_at.isoformat(),
        "expiresAt": (created_at + timedelta(minutes=PENDING_SEND_TTL_MINUTES)).isoformat(),
    }
    _save_json(_pending_send_path(), payload)
    return {
        "status": "pending_confirmation",
        "message": (
            f"Draft ready for {row['name']}: \"{payload['message_text']}\". "
            "Nothing has been sent yet — tell the user a Send now button is available "
            "in the UI and it only sends once they explicitly confirm there."
        ),
        "person_name": row["name"],
        "draft_text": payload["message_text"],
    }


def confirm_pending_send() -> dict:
    """The user said yes to exactly this message: it waits in the outbox until the LyteKeepr LinkedIn add-on on their computer sends it
    (lk_calendar/linkedin_outbox.py). Nothing goes to LinkedIn from the server."""
    payload = load_pending_send()
    if not payload:
        return {"status": "no_pending_send", "message": "There is no active draft to send."}
    message_text = (payload.get("message_text") or "").strip()
    if not message_text or not payload.get("person_id"):
        clear_pending_send()
        return {"status": "invalid_pending_send", "message": "The pending draft was invalid and was cleared."}
    from profiles.store import get_active_profile_id
    from lk_calendar import linkedin_link, linkedin_outbox
    if not linkedin_link.current_link(get_active_profile_id()):
        return {"status": "not_connected", "message": "LinkedIn isn't connected yet: connect it on Home (Get started), then send again. Nothing was sent."}
    linkedin_outbox.queue(int(payload["person_id"]), payload.get("person_name") or "", message_text, payload.get("conversation_ref"))
    clear_pending_send()
    return {"status": "queued", "person_name": payload.get("person_name"), "message_text": message_text,
            "message": f"Sending to {payload.get('person_name')} from your computer, through the LyteKeepr LinkedIn add-on: within a minute while your computer "
                       "and the Claude app are on, otherwise as soon as they are. It shows on their timeline once LinkedIn has it."}
