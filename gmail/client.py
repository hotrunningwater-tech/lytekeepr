"""
Thin Gmail REST API wrapper, stdlib only. No parsing, storage, or sync
orchestration lives here — just authenticated HTTP calls in and parsed
JSON out. Uses google_oauth.tokens.get_valid_access_token() for auth;
never touches the client secret directly.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from google_oauth.tokens import get_valid_access_token

from .errors import GmailHistoryGoneError, GmailSyncError

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _request(
    method: str,
    path: str,
    params: dict | None = None,
    timeout: int = 30,
    access_token: str | None = None,
    json_body: dict | None = None,
) -> dict:
    access_token = access_token or get_valid_access_token()
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    body = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    if json_body is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if exc.code == 410:
            raise GmailHistoryGoneError(body) from exc
        raise GmailSyncError(f"Gmail API error ({exc.code}): {body}") from exc
    except urllib.error.URLError as exc:
        raise GmailSyncError(f"Could not reach Gmail: {exc}") from exc


def get_profile(access_token: str | None = None) -> dict:
    return _request("GET", "/profile", access_token=access_token)


def get_sender_name(access_token: str | None = None) -> str:
    """The display name set on the connected Gmail address (the name recipients see in From). Empty if none is set."""
    data = _request("GET", "/settings/sendAs", access_token=access_token)
    entries = data.get("sendAs") or []
    pick = next((e for e in entries if e.get("isPrimary")), None) or next((e for e in entries if e.get("isDefault")), None) or (entries[0] if entries else {})
    name = (pick.get("displayName") or "").strip()
    if name:
        return name
    # no display name on the account: use the name on the emails the user has sent (the most common one in the latest few)
    import collections
    import email.utils
    seen = collections.Counter()
    try:
        for m in (_request("GET", "/messages", params={"q": "in:sent", "maxResults": 5}, access_token=access_token).get("messages") or [])[:5]:
            d = _request("GET", f"/messages/{m['id']}", params={"format": "metadata", "metadataHeaders": "From"}, access_token=access_token)
            headers = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
            display = email.utils.parseaddr(headers.get("from", ""))[0].strip()
            if display:
                seen[display] += 1
    except Exception:
        return ""
    return seen.most_common(1)[0][0] if seen else ""


def list_message_ids(query: str, page_token: str | None = None) -> dict:
    params = {"q": query, "maxResults": 500}
    if page_token:
        params["pageToken"] = page_token
    return _request("GET", "/messages", params=params)


def get_message(message_id: str) -> dict:
    return _request("GET", f"/messages/{message_id}", params={"format": "full"})


def get_attachment(message_id: str, attachment_id: str) -> dict:
    return _request("GET", f"/messages/{message_id}/attachments/{attachment_id}")


def download_attachment_bytes(message_id: str, attachment_id: str) -> bytes:
    result = get_attachment(message_id, attachment_id)
    data = result.get("data", "")
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def list_history(start_history_id: str, page_token: str | None = None) -> dict:
    params = {
        "startHistoryId": start_history_id,
        "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
    }
    if page_token:
        params["pageToken"] = page_token
    return _request("GET", "/history", params=params)


def send_message(raw_message: bytes, *, thread_id: str | None = None, access_token: str | None = None) -> dict:
    """Sends a real, live email via the Gmail API. raw_message is a full
    RFC 2822 message (e.g. built with email.mime.text.MIMEText), base64url-
    encoded here the same way import_message() does it. Passing thread_id
    keeps a reply in the same Gmail thread instead of starting a new one."""
    encoded = base64.urlsafe_b64encode(raw_message).decode().rstrip("=")
    body = {"raw": encoded}
    if thread_id:
        body["threadId"] = thread_id
    return _request("POST", "/messages/send", access_token=access_token, json_body=body)


def import_message(
    raw_message: bytes,
    *,
    deleted: bool = False,
    internal_date_source: str = "dateHeader",
    never_mark_spam: bool = True,
    process_for_calendar: bool = False,
    access_token: str | None = None,
) -> dict:
    params = {
        "deleted": str(deleted).lower(),
        "internalDateSource": internal_date_source,
        "neverMarkSpam": str(never_mark_spam).lower(),
        "processForCalendar": str(process_for_calendar).lower(),
    }
    encoded = base64.urlsafe_b64encode(raw_message).decode().rstrip("=")
    return _request(
        "POST",
        "/messages/import",
        params=params,
        access_token=access_token,
        json_body={"raw": encoded},
    )
