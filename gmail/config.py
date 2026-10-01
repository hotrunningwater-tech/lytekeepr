import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from profiles.store import profile_component_dir

MODULE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = MODULE_DIR / ".env"
STORAGE_ROOT = None
_ENV_LOADED = False


def get_storage_root() -> Path:
    if STORAGE_ROOT is not None:
        return STORAGE_ROOT
    return profile_component_dir("email-storage")


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(ENV_PATH)
        _ENV_LOADED = True


@dataclass
class GmailSyncConfig:
    # Optional initial backfill window. Empty/0 means "scan the full mailbox
    # slice allowed by the rest of the query filters" instead of truncating
    # historical imported/demo mail.
    sync_window_days: int | None = None
    # Optional fixed start date (e.g. "2026-04-27"). Preferred over
    # sync_window_days: a rolling day-count keeps drifting relative to
    # "today," so a case-relevant cutoff (like a filing date) silently
    # creeps forward every day the app isn't opened. A fixed date doesn't.
    # Takes priority over sync_window_days when both are set.
    sync_since_date: str | None = None

    @classmethod
    def from_env(cls) -> "GmailSyncConfig":
        _ensure_env_loaded()

        since_raw = (os.getenv("GMAIL_SYNC_SINCE_DATE", "") or "").strip()
        sync_since_date = None
        if since_raw:
            # Validates eagerly so a typo'd date fails at startup, not
            # silently inside a Gmail search query.
            date.fromisoformat(since_raw)
            sync_since_date = since_raw

        raw = (os.getenv("GMAIL_SYNC_WINDOW_DAYS", "") or "").strip()
        sync_window_days = None
        if raw and raw not in {"0", "all", "ALL"}:
            sync_window_days = int(raw)

        return cls(sync_window_days=sync_window_days, sync_since_date=sync_since_date)
