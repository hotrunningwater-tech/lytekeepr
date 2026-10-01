"""Builds the cross-referenced document registry: approved email
attachments (gmail.storage) x locally OCR'd documents (ocr_reader),
resolved via matcher. No Gmail API calls, no OpenAI calls — pure
read-and-cross-reference over data that's already local.
"""

from datetime import datetime, timezone

from gmail.attachment_rules import is_photo_reviewable_attachment
from gmail.storage import load_all_messages
from gmail.sync import get_active_account_id

from .photo_review_state import load_state as load_photo_review_state
from . import storage
from .matcher import match_attachment
from .ocr_reader import list_ocr_documents

# The OCR pipeline only handles PDFs (see ocr_service), so that's the only
# attachment type worth cross-referencing here. Without this filter, inline
# signature images (logos, tracking pixels) and .ics calendar invites — which
# Gmail's MIME structure exposes as "attachments" too — flood the registry
# as false "needs scanning" noise.
DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
PHOTO_REVIEW_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}

STATUS_REASONS = {
    "confirmed": "Filename and file size match a completed OCR scan.",
    "probable": "Likely matches a completed OCR scan (filename matched; size unknown or differs).",
    "missing": "No matching OCR-processed document found locally yet.",
}

CONFIDENCE_TO_STATUS = {
    "confirmed": "matched_scanned",
    "probable": "matched_scanned",
    "missing": "pending_scan",
}


def _build_attachment_entry(email: dict, attachment: dict, doc: dict | None, confidence: str) -> dict:
    from_obj = email.get("from") or {}
    return {
        "id": f"email:{email['id']}:{attachment.get('attachmentId', '')}",
        "source": "email",
        "attachmentId": attachment.get("attachmentId", ""),
        "emailId": email["id"],
        "emailSubject": email.get("subject", ""),
        "emailSender": from_obj,
        "emailDate": email.get("receivedAt", ""),
        "filename": attachment.get("filename", ""),
        "mimeType": attachment.get("mimeType", ""),
        "size": attachment.get("size"),
        "localPath": doc.get("originalPath") if doc else None,
        "ocrJobId": doc.get("jobId") if doc else None,
        "ocrStatus": doc.get("status") if doc else None,
        "downloaded": doc is not None,
        "matchConfidence": confidence,
        "status": CONFIDENCE_TO_STATUS[confidence],
        "reason": STATUS_REASONS[confidence],
    }


def _build_local_entry(doc: dict) -> dict:
    return {
        "id": f"local:{doc['jobId']}",
        "source": "local",
        "attachmentId": None,
        "emailId": None,
        "emailSubject": None,
        "emailSender": None,
        "emailDate": None,
        "filename": doc.get("filename", ""),
        "mimeType": "application/pdf",
        "size": doc.get("size"),
        "localPath": doc.get("originalPath"),
        "ocrJobId": doc.get("jobId"),
        "ocrStatus": doc.get("status"),
        "downloaded": True,
        "matchConfidence": "unresolved",
        "status": "unresolved_local",
        "reason": "Locally OCR-processed document doesn't confidently match any approved email attachment.",
    }


def _build_photo_review_entry(email: dict, attachments: list[dict], states: list[dict], generated_doc: dict | None) -> dict:
    from_obj = email.get("from") or {}
    attachment_ids = [attachment.get("attachmentId", "") for attachment in attachments if attachment.get("attachmentId")]
    filenames = [attachment.get("filename", "") for attachment in attachments if attachment.get("filename")]
    generated_states = [state for state in states if state.get("status") == "generated_pdf"]
    pending_states = [state for state in states if state.get("status") == "pending_review"]
    review_job_id = (generated_states[:1] or pending_states[:1] or [{}])[0].get("reviewJobId")
    status = "pending_photo_review"
    reason = "Photographed document pages need review before they can be turned into a PDF."
    local_path = None
    ocr_job_id = None
    ocr_status = None
    downloaded = False

    if generated_states and len(generated_states) == len(attachments) and generated_doc:
        status = "matched_scanned"
        reason = "Photo pages were reviewed and converted into a generated PDF."
        local_path = generated_doc.get("originalPath")
        ocr_job_id = generated_doc.get("jobId")
        ocr_status = generated_doc.get("status")
        downloaded = True
    elif pending_states:
        reason = "Photo pages are waiting for photographed-page review."

    return {
        "id": f"email:{email['id']}:photo-review",
        "source": "email",
        "attachmentId": attachment_ids[0] if attachment_ids else "",
        "attachmentIds": attachment_ids,
        "attachmentCount": len(attachments),
        "emailId": email["id"],
        "emailSubject": email.get("subject", ""),
        "emailSender": from_obj,
        "emailDate": email.get("receivedAt", ""),
        "filename": filenames[0] if len(attachments) == 1 and filenames else f"{len(attachments)} photo pages",
        "filenames": filenames,
        "mimeType": "application/x-dlyte-photo-pages",
        "size": sum(int(attachment.get("size") or 0) for attachment in attachments),
        "localPath": local_path,
        "ocrJobId": ocr_job_id,
        "ocrStatus": ocr_status,
        "downloaded": downloaded,
        "matchConfidence": "photo_review",
        "status": status,
        "reason": reason,
        "reviewJobId": review_job_id,
    }


def build_registry() -> dict:
    account_id = get_active_account_id()
    emails = load_all_messages(account_id) if account_id else []
    ocr_docs = list_ocr_documents()
    ocr_docs_by_job_id = {doc["jobId"]: doc for doc in ocr_docs}
    photo_review_state = load_photo_review_state()

    attachment_pairs = [
        (email, attachment)
        for email in emails
        for attachment in (email.get("attachments") or [])
        if (attachment.get("mimeType") or "").lower() in DOCUMENT_MIME_TYPES
    ]

    entries: dict[str, dict] = {}
    claimed_job_ids: set[str] = set()

    # Confirmed matches claim their document before weaker ("probable")
    # matches get a chance at the same document, so the strongest available
    # evidence wins any contested match.
    for pass_confidence in ("confirmed", "probable"):
        for email, attachment in attachment_pairs:
            key = f"email:{email['id']}:{attachment.get('attachmentId', '')}"
            if key in entries:
                continue
            doc, confidence = match_attachment(attachment, ocr_docs, claimed_job_ids)
            if confidence != pass_confidence:
                continue
            if doc:
                claimed_job_ids.add(doc["jobId"])
            entries[key] = _build_attachment_entry(email, attachment, doc, confidence)

    for email, attachment in attachment_pairs:
        key = f"email:{email['id']}:{attachment.get('attachmentId', '')}"
        if key not in entries:
            entries[key] = _build_attachment_entry(email, attachment, None, "missing")

    for email in emails:
        photo_attachments = [
            attachment for attachment in (email.get("attachments") or []) if is_photo_reviewable_attachment(attachment)
        ]
        if not photo_attachments:
            continue

        states = []
        generated_job_ids: set[str] = set()
        for attachment in photo_attachments:
            key = f"email:{email['id']}:{attachment.get('attachmentId', '')}"
            state = photo_review_state.get(key)
            if not state:
                continue
            states.append(state)
            if state.get("status") == "generated_pdf" and state.get("generatedJobId"):
                generated_job_ids.add(state["generatedJobId"])

        generated_doc = None
        if len(generated_job_ids) == 1:
            generated_doc = ocr_docs_by_job_id.get(next(iter(generated_job_ids)))

        entries[f"email:{email['id']}:photo-review"] = _build_photo_review_entry(
            email,
            photo_attachments,
            states,
            generated_doc,
        )

    for doc in ocr_docs:
        if doc["jobId"] in claimed_job_ids:
            continue
        entries[f"local:{doc['jobId']}"] = _build_local_entry(doc)

    dismissed_ids = storage.load_dismissed_ids()
    if dismissed_ids:
        entries = {key: entry for key, entry in entries.items() if key not in dismissed_ids}

    now = datetime.now(timezone.utc).isoformat()
    for entry in entries.values():
        entry["lastCheckedAt"] = now

    storage.save_registry(entries)
    return entries


def dismiss_entries(ids: list[str]) -> dict:
    """Permanently excludes the given registry entry ids from future
    attention lists, then rebuilds (cheap — local-only, see module
    docstring) so the result reflects the removal immediately."""
    storage.add_dismissed_ids(ids)
    return build_registry()


def compute_counts(entries: dict) -> dict:
    matched = sum(1 for e in entries.values() if e["status"] == "matched_scanned")
    pending_scan = sum(1 for e in entries.values() if e["status"] == "pending_scan")
    pending_photo_review = sum(1 for e in entries.values() if e["status"] == "pending_photo_review")
    unresolved = sum(1 for e in entries.values() if e["status"] == "unresolved_local")
    return {"matched": matched, "pendingScan": pending_scan, "pendingPhotoReview": pending_photo_review, "unresolved": unresolved}
