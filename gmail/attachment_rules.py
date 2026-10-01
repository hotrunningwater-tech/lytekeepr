"""Shared attachment-type rules used across Gmail, registry, and UI layers."""

PHOTO_REVIEW_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}


def is_photo_reviewable_attachment(attachment: dict) -> bool:
    return (attachment.get("mimeType") or "").lower() in PHOTO_REVIEW_IMAGE_MIME_TYPES
