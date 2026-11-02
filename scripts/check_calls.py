#!/usr/bin/env python3
"""Checks how a Fathom recording finds its meeting and contact, on a fresh installation in a temporary folder (never the real data):
an "Impromptu" recording listing only the host takes the meeting's name and guests; a call is never "with" our own side, even when our
address is also a contact; a call far from a meeting, or on another meeting link, stays off the guest's contact.
Prints CALLS CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_calls.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-calls-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({"LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy.db"), "LYTEKEEPR_PASSWORD": "calls-test",
                   "LYTEKEEPR_SECRET_KEY": "calls-test", "LYTEKEEPR_ADMIN_EMAIL": "owner@example.com", "LK_GMAIL_DAILY": "0",
                   "LK_GOOGLE_AUTOSYNC": "0", "LK_FATHOM_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": ""})
sys.path.insert(0, str(APP_DIR))
import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print("CALLS CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
    sys.exit(2)
import google_oauth.store as gstore  # noqa: E402
gstore.LEGACY_DIR = TMP / "legacy-google"
import server  # noqa: E402
from lk_calendar import calls, contacts, store  # noqa: E402

problems = []


def check(ok, what):
    if not ok:
        problems.append(what)


def meeting(day, hhmm, guest, title="Katie / Nexy Pilot", url="https://meet.google.com/aaa-bbbb-ccc"):
    with store._conn() as conn:
        return conn.execute("""INSERT INTO events (title, project, kind, start_date, start_time, duration_min, source, created_at, has_call, attendees, join_url, external_id)
                               VALUES (?, 'nexy', 'meeting', ?, ?, 30, 'google', '2026-01-01', 1, ?, ?, ?)""",
                            (title, day, hhmm, json.dumps([{"email": guest, "name": ""}]), url, f"google:primary:{title}{hhmm}")).lastrowid


def recording(rid, day, hhmm, url="https://meet.google.com/aaa-bbbb-ccc", title="Impromptu Google Meet Meeting"):
    calls._store_call({"recording_id": rid, "title": title, "recording_start_time": f"{day}T{hhmm}:00", "recording_end_time": f"{day}T{hhmm[:2]}:59:00", "meeting_url": url,
                       "calendar_invitees": [{"name": "Owner", "email": "owner@example.com", "is_external": False}],
                       "transcript": [{"speaker": {"display_name": "Owner"}, "timestamp": "00:01", "text": "hello"}]})
    with calls._crm() as conn:
        row = conn.execute("SELECT call_id, title FROM calls WHERE recording_id = ?", (rid,)).fetchone()
        return row["call_id"], row["title"], {r["person_id"] for r in conn.execute("SELECT person_id FROM call_people WHERE call_id = ?", (row["call_id"],))}


def main():
    with server.app.test_request_context():
        day = (date.today() - timedelta(days=1)).isoformat()
        katie = contacts.create_contact({"name": "Katie", "email": "katie@t.test", "brand": "nexy"})["id"]
        me = contacts.create_contact({"name": "Peter A", "email": "owner@example.com", "brand": "nexy"})["id"]
        other = contacts.create_contact({"name": "Other", "email": "other@t.test", "brand": "nexy"})["id"]
        with store._conn() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE email = 'owner@example.com'").fetchone():
                conn.execute("INSERT INTO users (email, name, role, created_at) VALUES ('owner@example.com', 'Owner', 'admin', '2026-01-01')")
        meeting(day, "11:00", "katie@t.test")
        cid, title, people = recording("r1", day, "10:56")
        check(title == "Katie / Nexy Pilot", f"an Impromptu recording takes the meeting's name ({title})")
        check(katie in people, "the guest's contact gets the call")
        check(me not in people, "a call is never with our own side, even when our address is a contact")
        meeting(day, "14:00", "other@t.test", title="Other call", url="https://meet.google.com/xxx-yyyy-zzz")
        _, title, people = recording("r2", day, "14:02", url="https://meet.google.com/ddd-eeee-fff")
        check(other not in people and title.startswith("Impromptu"), "another meeting link stays off the guest's contact")
        meeting(day, "17:00", "other@t.test", title="Later call", url="")
        _, title, people = recording("r3", day, "16:30", url="")
        check(other not in people, "a recording 30 minutes from a meeting stays off the guest's contact")
        with calls._crm() as conn:                 # an old link to our own side goes away
            conn.execute("INSERT OR IGNORE INTO call_people (call_id, person_id) VALUES (?, ?)", (cid, me))
        out = calls.attention()
        names = [p["name"] for r in out["ready"] for p in r["contacts"]]
        check("Peter A" not in names and "Katie" in names, f"Home says the call is with the guest, not us ({names})")


try:
    main()
except Exception as exc:
    import traceback
    traceback.print_exc()
    problems.append(f"crashed: {exc}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
for p in problems:
    print("FAILED:", p)
print("CALLS CHECK PASSED" if not problems else f"CALLS CHECK FAILED: {len(problems)} problem(s)")
sys.exit(0 if not problems else 1)
