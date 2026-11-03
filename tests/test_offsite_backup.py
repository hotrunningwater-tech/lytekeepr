"""The off-site backup: what goes in a copy, that a copy survives the round trip only with the right password, which old copies go,
and when the owner is told. Nothing reaches Backblaze."""

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("offsite_backup", ROOT / "scripts" / "offsite_backup.py")
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)

from lk_calendar import backup_watch  # noqa: E402


def db(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (text,))
    conn.commit()
    conn.close()


class CopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        profiles = self.tmp / "app" / "profiles"
        db(profiles / "data" / "0001" / "crm" / "lytekeepr.db", "contacts")
        db(profiles / "directory.db", "dir")
        (profiles / "data" / "0001" / "email-storage").mkdir(parents=True)
        (profiles / "data" / "0001" / "email-storage" / "m.json").write_text("mail")
        (profiles / "data" / "0001" / "crm" / "lytekeepr.db-wal").write_text("in progress")
        db(profiles / "deleted" / "0002-closed" / "crm" / "lytekeepr.db", "closed customer")
        (profiles / "registry.json").write_text("{}")
        (profiles / "store.py").write_text("# code")
        (self.tmp / "lytekeepr.env").write_text("LK_TOKEN_KEY=x")
        (self.tmp / "backblaze.env").write_text("B2_APP_KEY=secret")

    def test_what_goes_in(self):
        stage = self.tmp / "s"
        counts = offsite.stage(self.tmp, stage)
        names = {str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file()}
        self.assertEqual(names, {"profiles/data/0001/crm/lytekeepr.db", "profiles/directory.db", "profiles/data/0001/email-storage/m.json", "profiles/registry.json", "lytekeepr.env"})
        self.assertEqual(counts["databases"], 2)

    def test_round_trip_needs_the_password(self):
        stage = self.tmp / "s" / "lytekeepr"
        offsite.stage(self.tmp, stage)
        archive = self.tmp / "c.enc"
        offsite.pack(stage.parent / "lytekeepr", archive, "right password")
        self.assertNotIn(b"contacts", archive.read_bytes())
        self.assertNotIn(b"LK_TOKEN_KEY", archive.read_bytes())
        out = self.tmp / "out"
        out.mkdir()
        offsite.unpack(archive, out, "right password")
        self.assertEqual(offsite.check_copy(out)["databases"], 2)
        bad = self.tmp / "bad"
        bad.mkdir()
        with self.assertRaises(Exception):
            offsite.unpack(archive, bad, "wrong password")

    def test_password_is_not_on_the_command_line(self):
        source = (ROOT / "scripts" / "offsite_backup.py").read_text()
        self.assertNotIn("pass:", source)

    def test_a_broken_database_is_caught(self):
        stage = self.tmp / "s" / "lytekeepr"
        offsite.stage(self.tmp, stage)
        with open(stage / "profiles" / "directory.db", "r+b") as f:
            f.seek(100)
            f.write(b"\xff" * 4000)
        with self.assertRaises(Exception):
            offsite.check_copy(self.tmp / "s")

    def test_nothing_to_copy_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            offsite.stage(self.tmp / "empty", self.tmp / "s2")

    def test_which_old_copies_go(self):
        now = datetime(2026, 11, 20, 8)
        name = lambda days: f"lytekeepr-{now - timedelta(days=days):%Y%m%d-%H%M%S}.tar.gz.enc"
        names = [name(1), name(13), name(15), name(40), "something-else.txt", "lytekeepr-notadate.tar.gz.enc"]
        self.assertEqual(sorted(offsite.to_remove(names, now)), sorted([name(15), name(40)]))
        self.assertEqual(offsite.to_remove([name(40)], now), [], "the only copy must stay, however old")


class WatchTest(unittest.TestCase):
    def test_when_the_owner_is_told(self):
        now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        recent, old = (now - timedelta(hours=5)).isoformat(), (now - timedelta(hours=40)).isoformat()
        checked, stale = (now - timedelta(days=3)).isoformat(), (now - timedelta(days=12)).isoformat()
        good = {"ok": True, "at": recent, "verified_ok": True, "verified_at": checked}
        self.assertEqual(backup_watch.problem(good, now), "")
        self.assertIn("failed", backup_watch.problem(dict(good, ok=False), now))
        self.assertIn("has not run", backup_watch.problem(dict(good, at=old), now))
        self.assertIn("has not run", backup_watch.problem({}, now))
        self.assertIn("weekly check", backup_watch.problem(dict(good, verified_ok=False), now))
        self.assertIn("weekly check", backup_watch.problem(dict(good, verified_at=stale), now))

    def test_nothing_happens_without_a_status_file(self):
        with unittest.mock.patch.dict(os.environ, {"LK_BACKUP_STATUS": "/nonexistent/x.json"}):
            self.assertEqual(backup_watch.check(), "")


if __name__ == "__main__":
    unittest.main()
