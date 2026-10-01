"""Chat-driven draft + confirm staging for sending a real email via Gmail.

Mirrors assistant/linkedin_send_flow.py's pattern exactly: a tool call only
ever stages a draft (person + subject + body) to a local JSON file. Nothing
is actually sent until confirm_pending_send() is called, which only ever
happens from an explicit user click in the UI — see
/api/email/send-pending/confirm in server.py. Unlike LinkedIn (browser
automation, self-reported outcome not trustworthy), Gmail is an official
API: a non-error response IS real confirmation, no separate re-check needed.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from crm import db as crm_db
from gmail import client as gmail_client
from gmail.errors import GmailSyncError
from gmail.sync import get_active_account_id
from profiles.store import profile_component_dir

PENDING_SEND_TTL_MINUTES = 30


def _pending_send_path() -> Path:
    return profile_component_dir("assistant-state") / "pending-email-send.json"


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


def _resolve_from_gmail_index(person_name: str) -> dict | None:
    """Fallback when there's no CRM contact yet: search the raw Gmail
    index (the same store the email-search/summary tool reads from) for a
    sender whose name matches. Otherwise you can search/quote an email
    from someone but not draft a reply to them until they're separately
    saved into the CRM — which is not how it looks from the chat."""
    from gmail import storage as gmail_storage

    typed = person_name.strip().lower()
    matches: dict[str, str] = {}
    for account_id in gmail_storage.list_account_ids():
        for entry in gmail_storage.load_index(account_id).values():
            sender = entry.get("from") or {}
            name = (sender.get("name") or "").strip()
            email = (sender.get("email") or "").strip()
            if email and typed in name.lower():
                matches[email] = name

    if not matches:
        return None
    if len(matches) > 1:
        names = sorted(set(matches.values()))
        return {
            "status": "ambiguous",
            "message": f"More than one email in your inbox matches '{person_name}': {', '.join(names)}. Ask the user which one.",
        }
    email, name = next(iter(matches.items()))
    return {"status": "found", "person_name": name, "email": email}


def resolve_email_contact(person_name: str) -> dict:
    """Resolves a name against the CRM's email contacts, tolerating small
    typos via fuzzy fallback — same approach as
    linkedin_send_flow.resolve_linkedin_contact(). Returns
    {"status": "found", "person_name", "email"} or
    {"status": "not_found"/"ambiguous", "message"}."""
    conn = crm_db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT p.person_id, p.name, ca.channel_identifier AS email
            FROM people p
            JOIN channel_accounts ca ON ca.person_id = p.person_id AND ca.channel = 'email'
            WHERE p.name LIKE ?
            """,
            (f"%{person_name}%",),
        ).fetchall()

        if not rows:
            all_rows = conn.execute(
                """
                SELECT DISTINCT p.person_id, p.name, ca.channel_identifier AS email
                FROM people p
                JOIN channel_accounts ca ON ca.person_id = p.person_id AND ca.channel = 'email'
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
        fallback = _resolve_from_gmail_index(person_name)
        if fallback is not None:
            return fallback
        return {"status": "not_found", "message": f"No email contact matching '{person_name}'."}
    if len(distinct_people) > 1:
        names = sorted({row["name"] for row in distinct_people.values()})
        return {
            "status": "ambiguous",
            "message": f"More than one email contact matches '{person_name}': {', '.join(names)}. Ask the user which one.",
        }

    row = next(iter(distinct_people.values()))
    return {"status": "found", "person_name": row["name"], "email": row["email"]}


def create_pending_send(person_name: str, subject: str, body: str) -> dict:
    """Stages a draft once a resolved contact and real content are known.
    Requires an unambiguous single email-channel match; anything else is
    reported back to the model instead of guessing."""
    if not body or not body.strip():
        return {"status": "invalid", "message": "Draft needs non-empty message text."}

    resolved = resolve_email_contact(person_name)
    if resolved["status"] != "found":
        return resolved

    created_at = _utc_now()
    payload = {
        "type": "email_send",
        "person_name": resolved["person_name"],
        "to_email": resolved["email"],
        "subject": (subject or "").strip(),
        "body": body.strip(),
        "createdAt": created_at.isoformat(),
        "expiresAt": (created_at + timedelta(minutes=PENDING_SEND_TTL_MINUTES)).isoformat(),
    }
    _save_json(_pending_send_path(), payload)
    return {
        "status": "pending_confirmation",
        "message": (
            f"Draft ready for {resolved['person_name']} ({resolved['email']}) — "
            f"subject: \"{payload['subject']}\". Nothing has been sent yet — tell the user a "
            "Send now button is available in the UI and it only sends once they explicitly confirm there."
        ),
        "person_name": resolved["person_name"],
        "to_email": resolved["email"],
        "subject": payload["subject"],
        "draft_text": payload["body"],
    }


def confirm_pending_send() -> dict:
    payload = load_pending_send()
    if not payload:
        return {"status": "no_pending_send", "message": "There is no active draft to send."}

    to_email = payload.get("to_email")
    body = payload.get("body")
    if not to_email or not body:
        clear_pending_send()
        return {"status": "invalid_pending_send", "message": "The pending draft was invalid and was cleared."}

    account_id = get_active_account_id()
    if not account_id:
        clear_pending_send()
        return {"status": "failed", "message": "No Gmail account is connected."}

    mime = MIMEText(body)
    mime["To"] = to_email
    mime["Subject"] = payload.get("subject") or ""

    try:
        result = gmail_client.send_message(mime.as_bytes())
    except GmailSyncError as exc:
        # A real API error here means it genuinely did not send — unlike
        # LinkedIn's browser automation, Gmail's response is trustworthy,
        # so no separate re-check/verification pass is needed on top of it.
        clear_pending_send()
        return {"status": "failed", "message": f"Not sent to {payload.get('person_name')} — {exc}", "person_name": payload.get("person_name")}

    clear_pending_send()

    conn = crm_db.get_connection()
    try:
        person_id, crm_account_id = crm_db.get_or_create_person_for_channel(
            conn, "email", to_email, payload.get("person_name")
        )
        subject = payload.get("subject") or ""
        full_text = f"Subject: {subject}\n\n{body}" if subject else body
        crm_db.insert_message(
            conn,
            person_id=person_id,
            account_id=crm_account_id,
            thread_id=result.get("threadId"),
            channel="email",
            sender="You",
            direction="outgoing",
            message_text=full_text,
            timestamp=datetime.now().isoformat(),
            external_message_id=result.get("id"),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "sent",
        "message": f"Sent to {payload.get('person_name')}.",
        "person_name": payload.get("person_name"),
    }
