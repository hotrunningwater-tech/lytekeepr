"""Normalizes a raw Gmail API message resource into DLYTE's internal email
record shape. Pure data transformation — no HTTP calls, no storage."""

import base64
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime


def _decode_body(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _header(headers: list[dict], name: str) -> str:
    name = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _parse_addresses(value: str) -> list[dict]:
    if not value:
        return []
    return [{"name": name, "email": email} for name, email in getaddresses([value]) if email]


def _walk_parts(part: dict, text_parts: list[str], html_parts: list[str], attachments: list[dict]) -> None:
    mime_type = part.get("mimeType", "")
    filename = part.get("filename") or ""
    body = part.get("body", {}) or {}

    if filename and body.get("attachmentId"):
        attachments.append(
            {
                "attachmentId": body["attachmentId"],
                "filename": filename,
                "mimeType": mime_type,
                "size": body.get("size", 0),
            }
        )
    elif mime_type == "text/plain" and body.get("data"):
        text_parts.append(_decode_body(body["data"]))
    elif mime_type == "text/html" and body.get("data"):
        html_parts.append(_decode_body(body["data"]))

    for sub_part in part.get("parts", []) or []:
        _walk_parts(sub_part, text_parts, html_parts, attachments)


def normalize_message(raw: dict, account_id: str) -> dict:
    payload = raw.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []
    _walk_parts(payload, text_parts, html_parts, attachments)

    from_list = _parse_addresses(_header(headers, "From"))
    from_obj = from_list[0] if from_list else {"name": "", "email": ""}

    subject = _header(headers, "Subject")
    date_header = _header(headers, "Date")

    internal_date_ms = int(raw.get("internalDate") or "0")
    received_at = (
        datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc).isoformat()
        if internal_date_ms
        else ""
    )

    sent_at = ""
    if date_header:
        try:
            sent_at = parsedate_to_datetime(date_header).isoformat()
        except (TypeError, ValueError):
            sent_at = ""

    label_ids = raw.get("labelIds", []) or []

    return {
        "id": raw.get("id", ""),
        "threadId": raw.get("threadId", ""),
        "accountId": account_id,
        "historyId": raw.get("historyId", ""),
        "from": from_obj,
        "to": _parse_addresses(_header(headers, "To")),
        "cc": _parse_addresses(_header(headers, "Cc")),
        "bcc": _parse_addresses(_header(headers, "Bcc")),
        "subject": subject,
        "receivedAt": received_at,
        "sentAt": sent_at,
        "snippet": raw.get("snippet", ""),
        "textBody": "\n".join(text_parts).strip(),
        "htmlBody": "\n".join(html_parts).strip(),
        "labelIds": label_ids,
        "isUnread": "UNREAD" in label_ids,
        "isStarred": "STARRED" in label_ids,
        "hasAttachments": bool(attachments),
        "attachments": attachments,
        "syncedAt": datetime.now(timezone.utc).isoformat(),
    }
