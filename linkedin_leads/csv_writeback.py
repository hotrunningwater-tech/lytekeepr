"""
Writes a single lead's current DB state back into its source CSV row,
matched by profile_url — keeps the spreadsheet the record of truth, per
the explicit requirement that the app never silently drifts away from it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .importer import WEEKLY_DIR

UPDATABLE_COLUMNS = ("accepted", "replied", "notes", "follow_up_date")


def write_lead_to_csv(lead: dict) -> bool:
    """lead is a linkedin_leads row (dict) — must include profile_url and
    source_csv. Returns False (without raising) if the source file is
    missing, since a CSV being moved/renamed by hand shouldn't break the
    app's own status updates."""
    path = WEEKLY_DIR / lead["source_csv"]
    if not path.is_file():
        return False

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)

    changed = False
    for row in rows:
        if (row.get("profile_url") or "").strip() == lead["profile_url"]:
            for col in UPDATABLE_COLUMNS:
                if col in row:
                    row[col] = lead.get(col) or ""
            changed = True
            break

    if not changed:
        return False

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return True
