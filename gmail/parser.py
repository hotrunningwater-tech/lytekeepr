"""Normalizes a raw Gmail API message resource into DLYTE's internal email
record shape. Pure data transformation — no HTTP calls, no storage."""

import base64
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser


def _decode_body(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


class _HtmlText(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.links, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAKS:
            self.out.append("\n")
        if tag == "a":
            self.links.append((dict(attrs).get("href") or "", len(self.out)))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            label = "".join(self.out[start:]).strip()
            if href.startswith(("http://", "https://")) and href not in label:
                self.out.append(f" ({href})")
        elif tag in self.BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    """Plain text of an HTML email, keeping each link's address next to its words ("video (https://...)")."""
    p = _HtmlText()
    p.feed(re.sub(r"<!--.*?-->", "", html or "", flags=re.S))
    text = "".join(p.out).replace("\xa0", " ").replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
        "textBody": "\n".join(text_parts).strip() or html_to_text("\n".join(html_parts)),
        "htmlBody": "\n".join(html_parts).strip(),
        "labelIds": label_ids,
        "isUnread": "UNREAD" in label_ids,
        "isStarred": "STARRED" in label_ids,
        "isBulk": bool(_header(headers, "List-Unsubscribe") or _header(headers, "List-Id")              # mailing lists and mass mail
                       or _header(headers, "Precedence").strip().lower() in ("bulk", "list", "junk")
                       or _header(headers, "Auto-Submitted").strip().lower() not in ("", "no")),
        "hasAttachments": bool(attachments),
        "attachments": attachments,
        "syncedAt": datetime.now(timezone.utc).isoformat(),
    }
