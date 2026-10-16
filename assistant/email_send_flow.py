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
import secrets
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from crm import db as crm_db
from gmail import client as gmail_client
from gmail.errors import GmailSyncError
from gmail.sync import get_active_account_id
from profiles.store import profile_component_dir

PENDING_SEND_TTL_MINUTES = 24 * 60       # an unsent draft is kept for a day


def _pending_send_path() -> Path:
    return profile_component_dir("assistant-state") / "pending-email-send.json"


MAX_SAVED_DRAFTS = 5


def _drafts_path() -> Path:
    return profile_component_dir("assistant-state") / "email-drafts.json"


def _load_drafts() -> list[dict]:
    data = _load_json(_drafts_path())
    drafts = data.get("drafts") if isinstance(data, dict) else None
    return [d for d in drafts if isinstance(d, dict) and d.get("id")] if isinstance(drafts, list) else []


def _save_drafts(drafts: list[dict]) -> None:
    _save_json(_drafts_path(), {"drafts": drafts[-MAX_SAVED_DRAFTS:]})     # only the last five are kept


def list_drafts() -> list[dict]:
    """The saved, unsent drafts, newest first."""
    return list(reversed(_load_drafts()))


def _remember_draft(entry: dict) -> None:
    drafts = [d for d in _load_drafts() if d["id"] != entry["id"]]
    drafts.append(entry)
    _save_drafts(drafts)


def discard_draft(draft_id: str | None) -> None:
    """Remove a saved draft (used after it is sent, or when the user discards it). If it is the current draft, that goes too."""
    if not draft_id:
        return
    _save_drafts([d for d in _load_drafts() if d["id"] != draft_id])
    current = load_pending_send()
    if current and current.get("draftId") == draft_id:
        clear_pending_send()


def discard_drafts_for(person_id: int, emails: list[str]) -> None:
    """A contact was deleted: their saved drafts go too, and so does the draft on screen if it was addressed to them."""
    addresses = {e.strip().lower() for e in emails if e}
    def theirs(d: dict) -> bool:
        return d.get("person_id") == person_id or (d.get("to_email") or "").strip().lower() in addresses
    _save_drafts([d for d in _load_drafts() if not theirs(d)])
    current = load_pending_send()
    if current and theirs(current):
        clear_pending_send()


def repoint_drafts(old_person_id: int, new_person_id: int) -> None:
    """Two contacts were merged: saved drafts and the draft on screen now belong to the contact that was kept."""
    drafts = _load_drafts()
    for d in drafts:
        if d.get("person_id") == old_person_id:
            d["person_id"] = new_person_id
    _save_drafts(drafts)
    current = load_pending_send()
    if current and current.get("person_id") == old_person_id:
        current["person_id"] = new_person_id
        _save_json(_pending_send_path(), current)


def open_draft(draft_id: str) -> dict | None:
    """Make a saved draft the current one again (it can then be edited and sent)."""
    entry = next((d for d in _load_drafts() if d["id"] == draft_id), None)
    if entry is None:
        return None
    created_at = _utc_now()
    payload = {
        "type": "email_send", "draftId": entry["id"], "person_id": entry.get("person_id"), "person_name": entry.get("person_name"),
        "to_email": entry.get("to_email"), "reply_in_thread": bool(entry.get("reply_in_thread")), "subject": entry.get("subject", ""),
        "body": entry.get("body", ""), "createdAt": created_at.isoformat(),
        "expiresAt": (created_at + timedelta(minutes=PENDING_SEND_TTL_MINUTES)).isoformat(),
    }
    _save_json(_pending_send_path(), payload)
    return payload


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
    """Resolves what the user said (a name, a company, an email address) to ONE CRM contact that has an email address, tolerating small
    typos. Returns {"status": "found", "person_id", "person_name", "email"} or {"status": "not_found"/"ambiguous", "message"}."""
    from assistant.contact_lookup import describe, find_people

    matches = find_people(person_name, email_only=True)
    if not matches:
        any_match = find_people(person_name)
        if any_match:
            names = ", ".join(describe(m) for m in any_match[:5])
            return {"status": "not_found", "message": f"Found {names} in the CRM, but no email address is saved for them. Ask the user for one, or to add it to the contact."}
        fallback = _resolve_from_gmail_index(person_name)
        if fallback is not None:
            return fallback
        return {"status": "not_found", "message": f"No email contact matching '{person_name}'."}
    if len(matches) > 1:
        return {"status": "ambiguous", "message": f"More than one email contact matches '{person_name}': " + "; ".join(describe(m) for m in matches[:8]) + ". Ask the user which one."}
    m = matches[0]
    return {"status": "found", "person_id": m["person_id"], "person_name": m["name"], "email": m["email"]}


def create_pending_send(person_name: str, subject: str, body: str, reply_in_thread: bool = False) -> dict:
    """Stages a draft once a resolved contact and real content are known.
    Requires an unambiguous single email-channel match; anything else is
    reported back to the model instead of guessing."""
    if not body or not body.strip():
        return {"status": "invalid", "message": "Draft needs non-empty message text."}
    try:
        from lk_calendar import profile
        subject, body = profile.sign(subject or ""), profile.sign(body)       # never stage a draft that still says [Your Name]
    except Exception:
        pass

    resolved = resolve_email_contact(person_name)
    if resolved["status"] != "found":
        return resolved

    created_at = _utc_now()
    draft_id = secrets.token_hex(4)
    payload = {
        "type": "email_send",
        "draftId": draft_id,
        "person_id": resolved.get("person_id"),
        "person_name": resolved["person_name"],
        "to_email": resolved["email"],
        "reply_in_thread": bool(reply_in_thread),
        "subject": (subject or "").strip(),
        "body": body.strip(),
        "createdAt": created_at.isoformat(),
        "expiresAt": (created_at + timedelta(minutes=PENDING_SEND_TTL_MINUTES)).isoformat(),
    }
    _save_json(_pending_send_path(), payload)
    _remember_draft({k: payload[k] for k in ("person_id", "person_name", "to_email", "reply_in_thread", "subject", "body", "createdAt")} | {"id": draft_id})
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


def _reply_target(person_id: int, subject: str):
    """The last email thread with this person: (threadId, Message-ID to reply to, subject). Falls back to a new thread if anything is missing."""
    conn = crm_db.get_connection()
    try:
        row = conn.execute(
            "SELECT thread_id, external_message_id FROM messages WHERE person_id = ? AND channel = 'email' AND thread_id IS NOT NULL "
            "AND external_message_id IS NOT NULL ORDER BY message_id DESC LIMIT 1", (person_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None, None, subject
    try:
        meta = gmail_client.get_message(row["external_message_id"])
        headers = {h.get("name", "").lower(): h.get("value", "") for h in (meta.get("payload", {}).get("headers") or [])}
        message_id, original = headers.get("message-id"), headers.get("subject", "")
        if not message_id:
            return None, None, subject
        if not (subject or "").strip():
            subject = original if original.lower().startswith("re:") else f"Re: {original}"
        return row["thread_id"], message_id, subject
    except Exception:
        return None, None, subject


def _mark_email_sent(person_id: int) -> None:
    """A person in a funnel moves to "email sent" (which also schedules the follow-up task). Never lets a failure here affect the send."""
    try:
        from lk_calendar import contacts as lk_contacts
        lk_contacts.mark_email_sent(person_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not update the status after sending an email")


def update_pending_send(subject: str, body: str) -> dict | None:
    """The user edited the staged draft by hand: keep their text (it is what Send now sends and what the AI starts from next)."""
    payload = load_pending_send()
    if not payload:
        return None
    payload["subject"] = (subject or "").strip()
    payload["body"] = (body or "").rstrip()
    _save_json(_pending_send_path(), payload)
    if payload.get("draftId"):
        drafts = _load_drafts()
        for d in drafts:
            if d["id"] == payload["draftId"]:
                d["subject"], d["body"] = payload["subject"], payload["body"]
        _save_drafts(drafts)
    return payload


def send_email(person_id, person_name: str, to_email: str, subject: str, body: str, reply_in_thread: bool = False) -> dict:
    """Sends one real email through Gmail and records it: the message on the contact, then the "email sent" status and follow-up task.
    Returns {"status": "sent"/"failed", "message", "person_name"}. Shared by the single draft and the funnel batch."""
    account_id = get_active_account_id()
    if not account_id:
        return {"status": "failed", "message": "No Gmail account is connected.", "person_name": person_name}

    thread_id, in_reply_to = None, None
    if reply_in_thread and person_id:
        thread_id, in_reply_to, subject = _reply_target(person_id, subject)

    mime = MIMEText(body)
    mime["To"] = to_email
    mime["Subject"] = subject
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to

    try:
        result = gmail_client.send_message(mime.as_bytes(), thread_id=thread_id)
    except GmailSyncError as exc:
        # A real API error here means it genuinely did not send — unlike
        # LinkedIn's browser automation, Gmail's response is trustworthy,
        # so no separate re-check/verification pass is needed on top of it.
        return {"status": "failed", "message": f"Not sent to {person_name} — {exc}", "person_name": person_name}

    conn = crm_db.get_connection()
    try:
        person_id, crm_account_id = crm_db.get_or_create_person_for_channel(conn, "email", to_email, person_name)
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

    _mark_email_sent(person_id)
    return {"status": "sent", "message": f"Sent to {person_name}.", "person_name": person_name}


def confirm_pending_send() -> dict:
    payload = load_pending_send()
    if not payload:
        return {"status": "no_pending_send", "message": "There is no active draft to send."}

    to_email = payload.get("to_email")
    body = payload.get("body")
    if not to_email or not body:
        clear_pending_send()
        return {"status": "invalid_pending_send", "message": "The pending draft was invalid and was cleared."}

    result = send_email(payload.get("person_id"), payload.get("person_name"), to_email, payload.get("subject") or "", body,
                        bool(payload.get("reply_in_thread")))
    clear_pending_send()
    if result["status"] == "sent":
        discard_draft(payload.get("draftId"))           # sent: it no longer needs saving
    return result
