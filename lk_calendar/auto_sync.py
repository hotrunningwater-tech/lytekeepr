"""Hourly background Google Calendar sync, running inside the server process.

A small daemon thread wakes every TICK seconds and runs a sync when the last attempt (manual, chat or automatic) is
at least INTERVAL seconds old. The last-attempt time is saved in the database, so a restart neither skips an hour nor
causes a burst. It does nothing on a machine where Google was never connected. If Google authorization has expired the
failure is recorded and shown as a "Reconnect Google" prompt; existing events, notes and statuses are never touched.
Set LK_GOOGLE_AUTOSYNC=0 to switch it off.
"""

import logging
import os
import threading
import time

from . import google_sync, store

log = logging.getLogger(__name__)
_started = False


def _settings() -> tuple[int, int, int]:
    return (int(os.getenv("LK_GOOGLE_AUTOSYNC_INTERVAL_SEC", "3600")),
            int(os.getenv("LK_GOOGLE_AUTOSYNC_TICK_SEC", "60")),
            int(os.getenv("LK_GOOGLE_AUTOSYNC_STARTUP_DELAY_SEC", "20")))


def run_if_due() -> str:
    """One scheduler tick in this workspace: the admin's Google Calendar, then each team member's own. Returns what happened to the
    admin's (handy for tests): 'skipped', 'not due', 'synced' or 'failed'."""
    outcome = _run_one()
    try:
        from . import member_google
        for user_id in member_google.connected_members():
            with google_sync.as_member(user_id):
                _run_one()
    except Exception:
        log.exception("Team members' Google Calendar sync failed")
    return outcome


def _run_one() -> str:
    interval = _settings()[0]
    if not google_sync.is_connected():
        return "skipped"
    last = float(store.get_state(google_sync._key("google_last_attempt")) or 0)
    if time.time() - last < interval:
        return "not due"
    try:
        google_sync.sync(trigger="auto")      # records its own success/failure and the attempt time
        return "synced"
    except google_sync.GoogleSyncError as exc:
        log.warning("Hourly Google Calendar sync failed: %s", exc)
        return "failed"


def _loop() -> None:
    _, tick, delay = _settings()
    time.sleep(delay)
    while True:
        try:
            from . import directory
            directory.run_in_each_workspace(run_if_due, "Google Calendar sync")       # each workspace's own Google account
        except Exception:                      # a scheduler must never die
            log.exception("Google auto-sync tick crashed")
        time.sleep(tick)


def start() -> None:
    global _started
    if _started or os.getenv("LK_GOOGLE_AUTOSYNC", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, name="google-calendar-autosync", daemon=True).start()
