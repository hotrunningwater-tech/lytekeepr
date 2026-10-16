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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crm import db as crm_db
from integrations.linkedin import client
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
    payload = load_pending_send()
    if not payload:
        return {"status": "no_pending_send", "message": "There is no active draft to send."}

    conversation_ref = payload.get("conversation_ref")
    message_text = payload.get("message_text")
    if not conversation_ref or not message_text:
        clear_pending_send()
        return {"status": "invalid_pending_send", "message": "The pending draft was invalid and was cleared."}

    # Prefer sending via the person's profile rather than the existing
    # conversation thread — LinkedIn can flag a thread as a "group"
    # conversation even when it looks like a plain 1:1, and the send tool
    # refuses those; targeting the profile's own Message button sidesteps
    # that entirely. Falls back to conversation_ref if we can't resolve one.
    profile_slug = None
    search_res = client.search_people(payload.get("person_name", ""))
    if search_res.get("ok"):
        candidates = ((search_res.get("result") or {}).get("people")) or []
        match = next(
            (c for c in candidates if c.get("name", "").strip().lower() == payload.get("person_name", "").strip().lower()),
            candidates[0] if len(candidates) == 1 else None,
        )
        if match:
            profile_slug = match.get("profile_slug")

    try:
        res = client.send_message(message_text, conversation_ref=conversation_ref, profile_slug=profile_slug)
    finally:
        clear_pending_send()

    logger.warning("linkedin send_message raw response: %s", json.dumps(res)[:2000])

    if not res.get("ok"):
        return {"status": "failed", "message": res.get("error", "Send failed."), "person_name": payload.get("person_name")}

    # The send call returning ok just means the request round-tripped
    # without an exception — it is NOT confirmation anything was actually
    # sent. The only real confirmation is an independent re-check: wait a
    # moment for LinkedIn's UI to settle, then re-read the conversation
    # ourselves via a second MCP call and look for the exact text we sent
    # as the latest outgoing message.
    time.sleep(3)
    verify = client.get_conversation(conversation_ref, max_messages=5)
    logger.warning("linkedin post-send verify raw response: %s", json.dumps(verify)[:2000])
    matched_message = None
    if verify.get("ok"):
        recent_messages = ((verify.get("result") or {}).get("conversation") or {}).get("messages") or []
        matched_message = next(
            (
                m for m in recent_messages
                if m.get("direction") == "outgoing" and (m.get("text") or "").strip() == message_text.strip()
            ),
            None,
        )
    verified_in_history = matched_message is not None

    if verified_in_history:
        # We already have the confirmed message in hand from the check
        # above — save it into the CRM now instead of waiting for the next
        # scheduled sync, so it shows up on the board immediately.
        conn = crm_db.get_connection()
        try:
            channel_identifier = f"name:{payload.get('person_name')}"
            person_id, account_id = crm_db.get_or_create_person_for_channel(
                conn, "linkedin", channel_identifier, payload.get("person_name")
            )
            crm_db.insert_message(
                conn,
                person_id=person_id,
                account_id=account_id,
                thread_id=conversation_ref,
                channel="linkedin",
                sender="You",
                direction="outgoing",
                message_text=message_text,
                timestamp=datetime.now().isoformat(),
                external_message_id=matched_message.get("message_ref"),
            )
            conn.commit()
        finally:
            conn.close()

        from assistant import followup_engine  # local import: avoids a circular import with followup_engine -> linkedin_send_flow
        followup_engine.record_outreach(payload.get("person_id"), "linkedin", conversation_ref)

    return {
        "status": "sent" if verified_in_history else "not_confirmed",
        "message": (
            f"Sent to {payload.get('person_name')}."
            if verified_in_history
            else f"Not sent to {payload.get('person_name')}."
        ),
        "person_name": payload.get("person_name"),
        "message_text": message_text,
    }
