"""Maps stored Gmail message records to the drawer's EmailRecord shape
(see email/types.js) — the seam between synced Gmail data and the
existing, unmodified drawer UI.

The Inbox view shows every scanned message except ones explicitly marked
"not relevant" (status == dismissed/not_relevant), which are excluded
outright. Unreviewed messages show lightweight info (from scan-state)
with bodyLoaded=False; opening one triggers a live, non-persisted Gmail
fetch for the full text.
"""

from . import storage
from .attachment_rules import PHOTO_REVIEW_IMAGE_MIME_TYPES
from .errors import GmailSyncError
from .message_access import load_full_message
from .sync import get_active_account_id


def _map_attachments(attachments: list[dict]) -> list[dict]:
    out = []
    for attachment in attachments or []:
        mime_type = (attachment.get("mimeType") or "").lower()
        out.append(
            {
                "name": attachment.get("filename", ""),
                "attachmentId": attachment.get("attachmentId", ""),
                "mimeType": mime_type,
                "size": attachment.get("size", 0),
                "isPhotoReviewable": mime_type in PHOTO_REVIEW_IMAGE_MIME_TYPES,
            }
        )
    return out


def _map_folder(label_ids: list[str]) -> str:
    # Sync already excludes spam/trash, so this only needs to distinguish
    # "still in the Gmail inbox" from "everything else" (sent, archived).
    return "inbox" if "INBOX" in label_ids else "archived"


def to_email_record(message: dict, *, is_dlyte: bool = False) -> dict:
    """Full record -> EmailRecord, for messages whose complete body/attachments
    we actually have (saved records, or a live on-demand fetch)."""
    label_ids = message.get("labelIds", [])
    from_obj = message.get("from") or {}
    return {
        "id": message["id"],
        "senderName": from_obj.get("name") or from_obj.get("email") or "Unknown sender",
        "senderEmail": from_obj.get("email", ""),
        "recipients": [t.get("email", "") for t in message.get("to", []) if t.get("email")],
        "subject": message.get("subject") or "(no subject)",
        "preview": message.get("snippet", ""),
        "body": message.get("textBody") or message.get("snippet", ""),
        "receivedAt": message.get("receivedAt", ""),
        "unread": message.get("isUnread", False),
        "attachments": _map_attachments(message.get("attachments", [])),
        "folder": _map_folder(label_ids),
        # Every message that made it into messages/ was approved via the
        # candidate-review flow specifically because it looked case-relevant.
        "labels": ["dlyte"] if is_dlyte else [],
        "bodyLoaded": True,
    }


def _lightweight_record(message_id: str, entry: dict) -> dict:
    """scan-state entry -> EmailRecord, for messages we've only ever seen
    the metadata of (never fetched full content, or fetched-but-discarded
    it after classifying as not relevant)."""
    label_ids = entry.get("labelIds", [])
    return {
        "id": message_id,
        "senderName": entry.get("senderName") or entry.get("senderEmail") or "Unknown sender",
        "senderEmail": entry.get("senderEmail", ""),
        "recipients": [],
        "subject": entry.get("subject") or "(no subject)",
        "preview": entry.get("snippet", ""),
        "body": entry.get("snippet", ""),
        "receivedAt": entry.get("receivedAt", ""),
        "unread": entry.get("unread", False),
        "attachments": [{"name": ""}] if entry.get("hasAttachments") else [],
        "folder": _map_folder(label_ids),
        "labels": [],
        "bodyLoaded": False,
    }


def list_drawer_records(limit: int = 300) -> list[dict]:
    account_id = get_active_account_id()
    if not account_id:
        return []

    scan_state = storage.load_scan_state(account_id)
    # Dismissed ("not relevant") messages are excluded, not just tagged —
    # the drawer is the CRM's inbox view now, and marking something not
    # relevant is expected to actually remove it, not leave it sitting
    # there forever.
    all_entries = sorted(
        ((mid, e) for mid, e in scan_state.items() if e.get("status") not in ("dismissed", "not_relevant")),
        key=lambda kv: kv[1].get("receivedAt", ""),
        reverse=True,
    )

    # Saved/DLYTE messages must never be dropped by the recency cap below —
    # they're the case-relevant mail this whole app exists to surface, and
    # they trend older than the recent, mostly-irrelevant inbox noise that
    # dominates a plain "most recent N" cut. The limit only trims how much
    # *unreviewed* clutter rides along with them.
    saved_entries = [(mid, e) for mid, e in all_entries if e.get("status") == "saved"]
    other_entries = [(mid, e) for mid, e in all_entries if e.get("status") != "saved"]
    remaining = max(0, limit - len(saved_entries))
    entries = sorted(
        saved_entries + other_entries[:remaining],
        key=lambda kv: kv[1].get("receivedAt", ""),
        reverse=True,
    )

    records = []
    for message_id, entry in entries:
        if entry.get("status") == "saved":
            full = storage.load_message(account_id, message_id)
            records.append(to_email_record(full, is_dlyte=True) if full else _lightweight_record(message_id, entry))
        else:
            records.append(_lightweight_record(message_id, entry))
    return records


def get_message_detail(message_id: str) -> dict | None:
    """On-demand full detail for one message — used when the user opens an
    Inbox row that was never saved, so no full body is stored locally. Live
    Gmail fetch for display only; never persisted unless later approved
    through the normal candidate-review flow."""
    account_id = get_active_account_id()
    if not account_id:
        return None

    stored = storage.load_message(account_id, message_id)
    if stored:
        return to_email_record(stored, is_dlyte=True)

    try:
        full = load_full_message(message_id)
    except GmailSyncError:
        return None
    if full:
        storage.save_cached_message(account_id, full)
    return to_email_record(full, is_dlyte=False) if full else None
