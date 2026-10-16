"""The front desk: which email belongs to which workspace, and whether that workspace may be used.

This is the only place shared by all workspaces, and it holds nothing but emails, roles and account status (never contacts, emails
or notes). One email belongs to exactly one workspace. A workspace (an account, the paying or waived customer) is "waiting" after
sign-up until the super admin approves it, then "active"; "suspended" blocks everyone in it. Billing is "waived" by default.
"""

import os
import sqlite3
import threading
from datetime import datetime

from profiles import store as profiles

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    owner_email TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    status TEXT NOT NULL DEFAULT 'waiting',
    billing TEXT NOT NULL DEFAULT 'waived',
    created_at TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS members (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    role TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);
"""
STATUSES = ("waiting", "active", "suspended")
MAX_SIGNUPS_PER_HOUR = 20                 # a brake against someone creating workspaces in bulk
_lock = threading.Lock()


class DirectoryError(Exception):
    pass


def _path():
    return profiles.PROFILE_ROOT / "directory.db"


def _conn():
    profiles.PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def super_admins() -> set:
    """Who may open the owner's dashboard (LYTEKEEPR_SUPER_ADMINS, comma separated)."""
    raw = os.getenv("LYTEKEEPR_SUPER_ADMINS", "hotrunningwater@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_super_admin(email: str) -> bool:
    return bool(email) and email.strip().lower() in super_admins()


# ------------------------------------------------------------------ lookups
def account_for_profile(profile_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE profile_id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None


def lookup(email: str):
    """(account, role) for this email, or (None, None) if it is not known anywhere."""
    with _conn() as conn:
        row = conn.execute("SELECT m.role, a.* FROM members m JOIN accounts a ON a.id = m.account_id WHERE m.email = ?",
                           ((email or "").strip().lower(),)).fetchone()
    if not row:
        return None, None
    data = dict(row)
    return {k: v for k, v in data.items() if k != "role"}, data["role"]


def list_accounts() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""SELECT a.*, (SELECT COUNT(*) FROM members m WHERE m.account_id = a.id) AS people
                               FROM accounts a ORDER BY a.status = 'waiting' DESC, a.created_at DESC""").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ changes
def ensure_home_account(owner_email: str) -> dict:
    """The workspace the app was set up with is account 1: active, owned by the admin. Its team list is copied in once."""
    home = profiles.home_profile_id()
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE profile_id = ?", (home,)).fetchone()
        if not row:
            conn.execute("INSERT INTO accounts (profile_id, name, owner_email, status, created_at, approved_at) VALUES (?, ?, ?, 'active', ?, ?)",
                         (home, "My workspace", (owner_email or "").lower(), _now(), _now()))
            row = conn.execute("SELECT * FROM accounts WHERE profile_id = ?", (home,)).fetchone()
        elif owner_email and not row["owner_email"]:
            conn.execute("UPDATE accounts SET owner_email = ? WHERE id = ?", (owner_email.lower(), row["id"]))
        if owner_email:
            conn.execute("INSERT OR IGNORE INTO members (email, account_id, role, created_at) VALUES (?, ?, 'owner', ?)", (owner_email.lower(), row["id"], _now()))
        return dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone())


def add_member(email: str, profile_id: str, role: str = "internal") -> None:
    """Put an email on a workspace's list. Refused if that email already belongs to another workspace (one email, one account)."""
    email = (email or "").strip().lower()
    with _lock, _conn() as conn:
        acct = conn.execute("SELECT id FROM accounts WHERE profile_id = ?", (profile_id,)).fetchone()
        if not acct:
            raise DirectoryError("this workspace is not registered")
        have = conn.execute("SELECT account_id FROM members WHERE email = ?", (email,)).fetchone()
        if have and have["account_id"] != acct["id"]:
            raise DirectoryError("that email already belongs to another LyteKeepr account")
        if not have:
            conn.execute("INSERT INTO members (email, account_id, role, created_at) VALUES (?, ?, ?, ?)", (email, acct["id"], role, _now()))


def remove_member(email: str, profile_id: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM members WHERE email = ? AND role != 'owner' AND account_id = (SELECT id FROM accounts WHERE profile_id = ?)",
                     ((email or "").strip().lower(), profile_id))


def sign_up(email: str, name: str = "") -> dict:
    """A new person signed in with Google: they get their own empty workspace, waiting for approval."""
    email = (email or "").strip().lower()
    with _lock:
        with _conn() as conn:
            if conn.execute("SELECT 1 FROM members WHERE email = ?", (email,)).fetchone():
                raise DirectoryError("that email already belongs to an account")
            recent = conn.execute("SELECT COUNT(*) FROM accounts WHERE created_at >= ?",
                                  (datetime.now().replace(microsecond=0).isoformat(timespec="seconds")[:13],)).fetchone()[0]
            if recent >= MAX_SIGNUPS_PER_HOUR:
                raise DirectoryError("too many sign-ups right now, try again later")
        label = (name or email.split("@")[0]).strip()[:60] or "Workspace"
        import re
        slug = re.sub(r"[^a-z0-9]", "", email.split("@")[0])[:12] or "user"
        profile = profiles.create_profile(label, profile_id=f"ws-{datetime.now():%Y%m%d%H%M%S}-{slug}", is_demo_profile=False)
        _set_up_workspace(profile["profile_id"], email, name)
        with _conn() as conn:
            conn.execute("INSERT INTO accounts (profile_id, name, owner_email, status, created_at) VALUES (?, ?, ?, 'waiting', ?)",
                         (profile["profile_id"], label, email, _now()))
            acct = conn.execute("SELECT * FROM accounts WHERE profile_id = ?", (profile["profile_id"],)).fetchone()
            conn.execute("INSERT INTO members (email, account_id, role, created_at) VALUES (?, ?, 'owner', ?)", (email, acct["id"], _now()))
            return dict(acct)


def _set_up_workspace(profile_id: str, owner_email: str, name: str) -> None:
    """Create the new workspace's empty databases from the same template every workspace uses, with the owner as its admin."""
    from crm import db as crm_db
    from . import store
    with profiles.use_profile(profile_id):
        crm_db.init_db()
        with store._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO users (email, name, role, active, access, share_emails, created_at) VALUES (?, ?, 'admin', 1, 'all', 1, ?)",
                         (owner_email, (name or "")[:100], store.now_local().isoformat()))


def set_status(account_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise DirectoryError("status must be waiting, active or suspended")
    with _lock, _conn() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            raise DirectoryError("no such account")
        conn.execute("UPDATE accounts SET status = ?, approved_at = CASE WHEN ? = 'active' AND approved_at = '' THEN ? ELSE approved_at END WHERE id = ?",
                     (status, status, _now(), account_id))
        return dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())


# ------------------------------------------------------------------ background jobs
def active_workspaces() -> list[str]:
    """Every workspace a background job should visit: the first workspace and every approved account (never waiting or suspended)."""
    home = profiles.home_profile_id()
    with _conn() as conn:
        rows = [r["profile_id"] for r in conn.execute("SELECT profile_id FROM accounts WHERE status = 'active' ORDER BY id")]
    known = {p["profile_id"] for p in profiles.list_profiles()}
    return [home] + [p for p in rows if p != home and p in known]


def run_in_each_workspace(job, label: str) -> dict:
    """Run job() once inside each approved workspace, one after the other. A failure in one workspace is logged and never stops the rest."""
    import logging
    results = {}
    for profile_id in active_workspaces():
        try:
            with profiles.use_profile(profile_id):
                results[profile_id] = job()
        except Exception:
            logging.getLogger(__name__).exception("%s failed in workspace %s", label, profile_id)
            results[profile_id] = "failed"
    return results
