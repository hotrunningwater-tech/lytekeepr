"""
Daily-check gate for the document/email cross-reference, plus a manual
recheck path.

This is a locally-run dev server, not an always-on service — there's no
existing cron/task-queue infrastructure in this project to hook into,
so "runs once per day" is implemented the way that's actually honest for
this runtime: opportunistically, the first time the app is used after 24h
have elapsed since the last check (checked via /api/gmail/status, which
the drawer already calls on load). run_check_now() is the same underlying
operation, exposed directly for the manual "Recheck now" action.
"""

from datetime import datetime, timezone

from . import storage
from .config import DAILY_CHECK_INTERVAL_SECONDS
from .registry import build_registry


def maybe_run_daily_check() -> dict:
    state = storage.load_check_state()
    last_run = state.get("lastRunAt")

    if last_run:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run)).total_seconds()
            if elapsed < DAILY_CHECK_INTERVAL_SECONDS:
                return storage.load_registry()
        except ValueError:
            pass

    return run_check_now()


def run_check_now() -> dict:
    entries = build_registry()
    storage.save_check_state({"lastRunAt": datetime.now(timezone.utc).isoformat()})
    return entries
