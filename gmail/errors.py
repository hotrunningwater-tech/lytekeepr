class GmailSyncError(Exception):
    """Raised for Gmail API or sync failures, with a human-readable message."""


class GmailHistoryGoneError(GmailSyncError):
    """Raised when Gmail reports the stored history cursor (historyId) is no
    longer valid (HTTP 410) — the caller should fall back to a full sync."""
