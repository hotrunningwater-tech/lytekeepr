"""
Weekly LinkedIn-leads CSV import: scans linkedin-leads/weekly/ for files not
yet recorded in linkedin_leads_imports, upserts each row into the
linkedin_leads table (deduped by profile_url), and makes sure exactly one
unresolved "provide this week's CSV" reminder exists whenever a check finds
no new file since the last import. The CSV stays the record of truth —
status-changing actions elsewhere write back into source_csv, this module
only ever reads.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from crm import db as crm_db

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
WEEKLY_DIR = APP_DIR / "linkedin-leads" / "weekly"
MISSING_CSV_REMINDER_SOURCE = "system_leads_csv_missing"
DAYS_BETWEEN_IMPORTS = 7
_CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # file-only check, cheap — a few times a day is plenty


def _row_to_lead(row: dict) -> dict | None:
    profile_url = (row.get("profile_url") or "").strip()
    name = (row.get("name") or "").strip()
    if not profile_url or not name:
        return None
    return {
        "profile_url": profile_url,
        "name": name,
        "company": (row.get("company") or "").strip() or None,
        "connection_note": (row.get("connection_note") or "").strip() or None,
        "dm_after_accept": (row.get("dm_after_accept") or "").strip() or None,
        "sent_date": (row.get("sent_date") or "").strip() or None,
        "accepted": (row.get("accepted") or "").strip() or None,
        "replied": (row.get("replied") or "").strip() or None,
        "notes": (row.get("notes") or "").strip() or None,
        "follow_up_date": (row.get("follow_up_date") or "").strip() or None,
    }


def import_file(path: Path) -> dict:
    """Upserts every row in one CSV into linkedin_leads. Safe to re-run on
    an already-imported file — upsert just refreshes existing rows."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    crm_db.init_db()
    conn = crm_db.get_connection()
    new_count = updated_count = skipped = 0
    try:
        for raw_row in rows:
            lead = _row_to_lead(raw_row)
            if lead is None:
                skipped += 1
                continue
            _, was_new = crm_db.upsert_linkedin_lead(conn, lead, source_csv=path.name)
            if was_new:
                new_count += 1
            else:
                updated_count += 1
        crm_db.record_leads_import(conn, path.name, len(rows))
        conn.commit()
    finally:
        conn.close()

    result = {"filename": path.name, "rows": len(rows), "new": new_count, "updated": updated_count, "skipped": skipped}
    logger.info("linkedin_leads import: %s", result)
    return result


def check_for_new_files() -> list[dict]:
    """Imports any weekly CSV not already recorded. Returns the list of
    import results (empty if nothing new)."""
    if not WEEKLY_DIR.is_dir():
        return []
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        already_imported = crm_db.list_imported_lead_filenames(conn)
    finally:
        conn.close()

    results = []
    for path in sorted(WEEKLY_DIR.glob("*.csv")):
        if path.name in already_imported:
            continue
        results.append(import_file(path))
    return results


def ensure_missing_csv_reminder() -> None:
    """If it's been a week or more since the last import and no new file
    has shown up, make sure there's exactly one unresolved reminder
    nudging the user to drop this week's CSV in — never more than one at
    once (see crm_db.has_pending_reminder_from)."""
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        latest = crm_db.latest_leads_import(conn)
        if latest is None:
            return  # never imported anything yet — nothing to be "overdue" relative to
        last_imported_at = datetime.fromisoformat(latest["imported_at"])
        if datetime.now() - last_imported_at < timedelta(days=DAYS_BETWEEN_IMPORTS):
            return
        if crm_db.has_pending_reminder_from(conn, MISSING_CSV_REMINDER_SOURCE):
            return
        today = datetime.now().date().isoformat()
        crm_db.create_reminder(
            conn,
            text="Drop this week's LinkedIn leads CSV into linkedin-leads/weekly/",
            due_date=today,
            source=MISSING_CSV_REMINDER_SOURCE,
        )
        conn.commit()
    finally:
        conn.close()


def run_check() -> None:
    imported = check_for_new_files()
    if not imported:
        ensure_missing_csv_reminder()


def _scheduler_loop() -> None:
    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)
        try:
            run_check()
        except Exception:  # noqa: BLE001 - a scheduler tick failing should never kill the loop
            logger.exception("linkedin_leads scheduler tick failed")
            continue


def start_scheduler() -> None:
    """Safe to call once at app startup. Runs one check immediately (not
    just after the first interval), then ticks every
    _CHECK_INTERVAL_SECONDS."""
    try:
        run_check()
    except Exception:
        logger.exception("linkedin_leads initial check failed")
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
