#!/usr/bin/env python3
"""Nightly backup of the live databases, for every workspace: its calendar (events, leads, team) and its CRM (people, companies,
funnels) (runs on the server, see /etc/cron.d/lytekeepr-calendar-backup). Each workspace's copies are kept and pruned on their own.

Uses SQLite's own backup API, so it is safe while the app is running. Keeps the newest KEEP copies.
Restore: stop the service, copy a backup over /opt/lytekeepr-calendar/data/calendar.db, start it again.
"""
import datetime
import os
import sqlite3
import sys
from pathlib import Path

SRC = Path("/opt/lytekeepr-calendar/data/calendar.db")              # the old shared calendar, until a workspace has adopted it
PROFILES_DATA = Path(os.getenv("LK_PROFILES_ROOT", "/opt/lytekeepr-calendar/app/profiles")) / "data"     # <workspace>/crm, <workspace>/calendar
DEST = Path("/opt/lytekeepr-calendar/backups")
KEEP = 14

DEST.mkdir(mode=0o700, parents=True, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(src_path: Path, prefix: str, count_sql: dict) -> bool:
    target = DEST / f"{prefix}-{stamp}.db"
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    check = sqlite3.connect(target)
    ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    counts = ", ".join(f"{n} {check.execute(q).fetchone()[0]}" for n, q in count_sql.items())
    check.close()
    if not ok:
        target.unlink()
        print(f"{datetime.datetime.now():%F %T} FAILED integrity check for {prefix}, backup discarded")
        return False
    os.chmod(target, 0o600)
    old = sorted(DEST.glob(f"{prefix}-*.db"))[:-KEEP]
    for f in old:
        f.unlink()
    print(f"{datetime.datetime.now():%F %T} OK {target.name} ({counts}); {len(list(DEST.glob(prefix + '-*.db')))} kept, {len(old)} pruned")
    return True


CAL_COUNTS = {"events": "SELECT COUNT(*) FROM events", "leads": "SELECT COUNT(*) FROM leads"}
CRM_COUNTS = {"people": "SELECT COUNT(*) FROM people", "companies": "SELECT COUNT(*) FROM companies"}
results = []
adopted = any(PROFILES_DATA.glob("*/calendar/adopted-from.txt"))
if SRC.exists() and not adopted:
    results.append(backup(SRC, "calendar", CAL_COUNTS))
for ws in sorted(p for p in PROFILES_DATA.glob("*") if p.is_dir()):
    if (ws / "calendar" / "calendar.db").exists():
        results.append(backup(ws / "calendar" / "calendar.db", f"ws{ws.name}-calendar", CAL_COUNTS))
    if (ws / "crm" / "lytekeepr.db").exists():
        results.append(backup(ws / "crm" / "lytekeepr.db", f"ws{ws.name}-crm", CRM_COUNTS))
sys.exit(0 if all(results) else 1)
