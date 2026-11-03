"""Tells the owner when the nightly off-site backup (scripts/offsite_backup.py) fails or stops running: a task on the first workspace's
Home and an email, at most once a day. On a machine with no status file (a laptop, a check) it does nothing."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
MAX_AGE_HOURS = 36                  # nightly, plus room for one slow night
VERIFY_MAX_AGE_DAYS = 9             # weekly


def _status_path() -> Path:
    return Path(os.getenv("LK_BACKUP_STATUS", "/opt/lytekeepr-calendar/offsite-status.json"))


def problem(status: dict, now: datetime) -> str:
    """Plain words for what is wrong, or '' when all is well."""
    def age(key):
        try:
            return now - datetime.fromisoformat(status.get(key) or "")
        except ValueError:
            return None
    if status.get("ok") is False:
        return "Last night's backup to Backblaze failed. Your data is not safely copied off the server until this is fixed."
    backup_age = age("at")
    if backup_age is None or backup_age > timedelta(hours=MAX_AGE_HOURS):
        return "The nightly backup to Backblaze has not run for over a day. Your data is not safely copied off the server until this is fixed."
    if status.get("verified_ok") is False:
        return "The weekly check of the Backblaze backup failed: a copy may not be restorable."
    verify_age = age("verified_at")
    if verify_age is None or verify_age > timedelta(days=VERIFY_MAX_AGE_DAYS):
        return "The weekly check of the Backblaze backup has not run for over a week."
    return ""


def check() -> str:
    """Once an hour from the background loop. The state key keeps it to one task and one email a day."""
    try:
        status = json.loads(_status_path().read_text())
    except OSError:
        return ""
    except ValueError:
        status = {"ok": False}
    text = problem(status, datetime.now(timezone.utc))
    if not text:
        return ""
    from profiles.store import home_profile_id, use_profile
    from . import directory, store, superadmin
    with use_profile(home_profile_id()):
        today = store.now_local().date().isoformat()
        if store.get_state("backup_alert_day") == today:
            return text
        store.set_state("backup_alert_day", today)
        store.create_event({"title": "Fix backups: tell Claude Code \"backups failed\"", "project": "lytekeepr", "kind": "task", "start_date": today, "notes": text})
    directory.record_error("Backups", text, home_profile_id())
    if directory.console_settings().get("alert_errors") == "1":
        superadmin._alert("Backups", text, home_profile_id())
    log.warning(text)
    return text
