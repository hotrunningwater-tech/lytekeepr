"""LinkedIn messages waiting to go out. The USER approves the exact text (in Claude, shown first, or with Send in LyteKeepr); it waits
here, in their own workspace, until the LyteKeepr LinkedIn add-on on their computer sends it with their LinkedIn login and reports back.

A message is sent once at most: the add-on claims it before sending, and one claimed but never reported back is marked "may have gone
out" instead of being tried again. Kept in the workspace's CRM database."""

import re
from datetime import datetime, timedelta

from crm import db as crm_db

STALE_MINUTES = 15            # claimed this long ago and never reported: the add-on stopped halfway
CLAIM_AT_ONCE = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    person_name TEXT NOT NULL DEFAULT '',
    profile_slug TEXT,
    conversation_ref TEXT,
    message_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);
"""
SLUG = re.compile(r"linkedin\.com/in/([A-Za-z0-9][A-Za-z0-9-]{2,199})", re.I)


def _conn():
    conn = crm_db.get_connection()
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def profile_slug(conn, person_id: int):
    """Their LinkedIn profile, from what LyteKeepr has: a synced profile name or a saved profile link."""
    for row in conn.execute("SELECT channel_identifier, url FROM channel_accounts WHERE person_id = ? AND channel = 'linkedin'", (person_id,)):
        ident = row["channel_identifier"] or ""
        if ident and not ident.startswith("name:") and "/" not in ident and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{2,199}", ident):
            return ident
        for text in (ident, row["url"] or ""):
            m = SLUG.search(text)
            if m:
                return m.group(1)
    return None


def queue(person_id: int, person_name: str, text: str, conversation_ref=None) -> int:
    conn = _conn()
    try:
        slug = profile_slug(conn, person_id)
        cur = conn.execute("INSERT INTO linkedin_outbox (person_id, person_name, profile_slug, conversation_ref, message_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (person_id, person_name, slug, conversation_ref, text, _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def claim() -> list:
    conn = _conn()
    try:
        stale = (datetime.now() - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
        conn.execute("UPDATE linkedin_outbox SET status = 'uncertain', detail = 'The add-on stopped while sending: check LinkedIn whether it went out.', finished_at = ? "
                     "WHERE status = 'sending' AND claimed_at < ?", (_now(), stale))
        rows = conn.execute("SELECT * FROM linkedin_outbox WHERE status = 'queued' ORDER BY outbox_id LIMIT ?", (CLAIM_AT_ONCE,)).fetchall()
        for r in rows:
            conn.execute("UPDATE linkedin_outbox SET status = 'sending', claimed_at = ? WHERE outbox_id = ? AND status = 'queued'", (_now(), r["outbox_id"]))
        conn.commit()
        return [{"id": r["outbox_id"], "name": r["person_name"], "text": r["message_text"], "profile_slug": r["profile_slug"],
                 "conversation_ref": r["conversation_ref"]} for r in rows]
    finally:
        conn.close()


def finish(outbox_id: int, outcome: str, detail: str = "") -> bool:
    """outcome: verified (sent), failed, uncertain (LinkedIn could not confirm), retry (not tried: e.g. logged out; back in the queue)."""
    status = {"verified": "sent", "failed": "failed", "uncertain": "uncertain", "retry": "queued"}.get(outcome)
    if not status:
        return False
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM linkedin_outbox WHERE outbox_id = ? AND status = 'sending'", (int(outbox_id),)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE linkedin_outbox SET status = ?, detail = ?, finished_at = ?, claimed_at = CASE WHEN ? = 'queued' THEN '' ELSE claimed_at END WHERE outbox_id = ?",
                     (status, str(detail)[:500], "" if status == "queued" else _now(), status, int(outbox_id)))
        conn.commit()
    finally:
        conn.close()
    if status == "sent":
        try:
            from assistant import followup_engine
            followup_engine.record_outreach(row["person_id"], "linkedin", row["conversation_ref"])
        except Exception:
            pass
    return True


def counts() -> dict:
    conn = _conn()
    try:
        waiting = conn.execute("SELECT COUNT(*) FROM linkedin_outbox WHERE status IN ('queued', 'sending')").fetchone()[0]
        week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
        problems = [dict(r) for r in conn.execute("SELECT person_name, status, detail, finished_at FROM linkedin_outbox WHERE status IN ('failed', 'uncertain') "
                                                  "AND finished_at > ? ORDER BY outbox_id DESC LIMIT 5", (week,))]
        return {"waiting": waiting, "problems": problems}
    finally:
        conn.close()
