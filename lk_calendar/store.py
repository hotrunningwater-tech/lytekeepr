"""SQLite storage + recurrence expansion for the calendar.

Times are stored as naive local Montreal time (date + HH:MM), so
recurring items stay at the same wall-clock time across daylight saving.
"Now" is always computed in TZ below, never from the machine's clock zone.
"""

import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
# Before workspaces existed there was one calendar database for the whole app (LK_CALENDAR_DB on the server). It is copied once
# into the first workspace and then left untouched as a backup; every workspace now has its own <workspace>/calendar/calendar.db.
LEGACY_DB_PATH = Path(os.getenv("LK_CALENDAR_DB") or Path(__file__).resolve().parent / "calendar.db")
_adopt_lock = __import__("threading").Lock()


def db_path() -> Path:
    """This workspace's calendar database."""
    from profiles.store import get_active_profile_id, home_profile_id, profile_component_dir
    path = profile_component_dir("calendar") / "calendar.db"
    if not path.exists() and LEGACY_DB_PATH.exists() and get_active_profile_id() == home_profile_id():
        _adopt_legacy(path)
    return path


def _adopt_legacy(path: Path) -> None:
    """Copy the old shared calendar database into the first workspace (SQLite's own backup, safe while it is in use)."""
    with _adopt_lock:
        if path.exists():
            return
        tmp = path.with_suffix(".adopting")
        src = sqlite3.connect(f"file:{LEGACY_DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        tmp.replace(path)
        (path.parent / "adopted-from.txt").write_text(f"{LEGACY_DB_PATH}\n{datetime.now().isoformat()}\n")

# Which funnel stage each status belongs to (by stage name). Changing a status moves the person to that stage if they are in a funnel.
DEFAULT_STAGES = {
    ("linkedin", "new"): "New lead", ("linkedin", "sent"): "Contacted", ("linkedin", "accepted"): "Contacted",
    ("linkedin", "message_sent"): "Contacted", ("linkedin", "replied"): "In conversation", ("linkedin", "non customer"): "Lost", ("linkedin", "done"): "Lost",
    ("instagram", "new"): "New lead", ("instagram", "sent"): "Contacted", ("instagram", "replied"): "In conversation", ("instagram", "done"): "Lost",
    ("email", "new"): "New lead", ("email", "sent"): "Contacted", ("email", "replied"): "In conversation", ("email", "bounced"): "Lost", ("email", "done"): "Lost",
}
# The later funnel stages: status -> (stage, task rule, task title). The same on every channel; each matches its stage until we need to split one.
LATER_STATUSES = {
    "call booked": ("In conversation", "none", ""),
    "call done": ("In conversation", "task", "Follow up after the call"),
    "confirmed": ("Confirmed", "none", ""),
    "onboarding": ("Onboarding", "none", ""),
    "completed": ("Completed", "closed", ""),
}
# Brands (the businesses this account runs) live in the brands table and can be edited; these are the first ones.
# "google" is not a brand: it marks read-only events synced from Google Calendar (a calendar source).
DEFAULT_BRANDS = (("lytekeepr", "LyteKeepr", "#7fa8b8"), ("aspan", "Aspan", "#e98b74"), ("nexy", "Nexy", "#21493f"))
CALENDAR_SOURCES = ("google",)
# "personal" is not a brand either: a task or reminder only the person who made it sees (not even the admin sees a member's).
PERSONAL = "personal"
MEETING_STATUSES = {"scheduled": "", "completed": "completed", "no_show": "no_show", "cancelled": "cancelled"}   # stored value; "" = scheduled
MEETING_ALIASES = {"scheduled": "scheduled", "upcoming": "scheduled", "": "scheduled", "completed": "completed", "complete": "completed",
                   "done": "completed", "held": "completed", "no show": "no_show", "noshow": "no_show", "no-show": "no_show",
                   "cancelled": "cancelled", "canceled": "cancelled"}
FOLLOW_UP_AFTER_CALL_HOURS = 24
UNTIMED_TIME = "08:00"          # items with no specific time are stored at 08:00 and flagged untimed (they live in the top strip)
BUCKETS = ("cold", "warm", "closed", "none", "task")      # task = its own task per person with a custom title (status_tasks)
# How a lead's status is treated: cold = reached out, no reply (grouped follow-up); warm = they replied (own task);
# closed = nothing more to do (reminder closed); none = no task (new leads, and custom labels unless set otherwise).
DEFAULT_BUCKETS = {
    ("linkedin", "new"): "none", ("linkedin", "sent"): "cold", ("linkedin", "accepted"): "cold",
    ("linkedin", "message_sent"): "cold", ("linkedin", "replied"): "warm", ("linkedin", "non customer"): "closed", ("linkedin", "done"): "closed",
    ("instagram", "new"): "none", ("instagram", "sent"): "cold", ("instagram", "replied"): "warm", ("instagram", "done"): "closed",
    ("email", "new"): "none", ("email", "sent"): "cold", ("email", "replied"): "warm", ("email", "bounced"): "closed", ("email", "done"): "closed",
}
KINDS = ("task", "meeting", "reminder")
REPEATS = ("none", "daily", "weekdays", "weekly")
DURATIONS = {"task": 30, "meeting": 60, "reminder": 15}
MAX_RANGE_DAYS = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,
  start_date TEXT NOT NULL,
  start_time TEXT NOT NULL,
  duration_min INTEGER NOT NULL,
  repeat TEXT NOT NULL DEFAULT 'none',
  repeat_days TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  lead_ref TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  external_id TEXT NOT NULL DEFAULT '',
  my_notes TEXT NOT NULL DEFAULT '',
  meeting_status TEXT NOT NULL DEFAULT '',
  has_call INTEGER NOT NULL DEFAULT 0,
  untimed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  signature TEXT NOT NULL UNIQUE,
  mapping TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT '',
  used INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status_tasks (
  channel TEXT NOT NULL,
  status_key TEXT NOT NULL,
  title TEXT NOT NULL,
  PRIMARY KEY (channel, status_key)
);
CREATE TABLE IF NOT EXISTS status_stages (
  channel TEXT NOT NULL,
  status_key TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  PRIMARY KEY (channel, status_key)
);
CREATE TABLE IF NOT EXISTS status_buckets (
  channel TEXT NOT NULL,
  status_key TEXT NOT NULL,
  bucket TEXT NOT NULL,
  PRIMARY KEY (channel, status_key)
);
CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  project TEXT NOT NULL,
  contact TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  status_date TEXT NOT NULL,
  follow_up_date TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE (channel, contact)
);
CREATE TABLE IF NOT EXISTS custom_statuses (
  channel TEXT NOT NULL,
  label TEXT NOT NULL,
  PRIMARY KEY (channel, label)
);
CREATE TABLE IF NOT EXISTS lead_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch TEXT NOT NULL,
  lead_id INTEGER NOT NULL,
  old_status TEXT NOT NULL,
  old_status_date TEXT NOT NULL,
  old_follow_up TEXT NOT NULL DEFAULT '',
  new_status TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'status',
  old_event_date TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    access TEXT NOT NULL DEFAULT 'funnels',
    share_emails INTEGER NOT NULL DEFAULT 0,
    last_login_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS funnel_shares (
    user_id INTEGER NOT NULL,
    funnel_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, funnel_id)
);

CREATE TABLE IF NOT EXISTS brand_access (
  user_id INTEGER NOT NULL,
  brand TEXT NOT NULL,
  scope TEXT NOT NULL,           -- 'all' = the whole brand; 'funnels' = only the funnels of that brand shared with them
  PRIMARY KEY (user_id, brand)
);

CREATE TABLE IF NOT EXISTS brands (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS completions (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  occ_date TEXT NOT NULL,
  PRIMARY KEY (event_id, occ_date)
);
"""


class ValidationError(ValueError):
    pass


GOOGLE_READ_ONLY = "This event is synced from Google Calendar and is read-only. You can add your own notes and set its meeting status."


class _ClosingConnection(sqlite3.Connection):
    """`with _conn() as conn:` commits/rolls back AND closes. (The stock sqlite3 context manager never
    closes, so every call leaked open files until the server ran out of them.)"""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def _is_home_workspace() -> bool:
    """The first workspace (the owner's): it keeps the owner's starting brands. Every other workspace starts with none."""
    try:
        from profiles.store import get_active_profile_id, home_profile_id
        return get_active_profile_id() == home_profile_id()
    except Exception:
        return True


def _conn():
    conn = sqlite3.connect(db_path(), timeout=10, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    for table, column, ddl in (
        ("leads", "follow_up_date", "TEXT NOT NULL DEFAULT ''"),
        ("leads", "person_id", "INTEGER"),
        ("lead_history", "kind", "TEXT NOT NULL DEFAULT 'status'"),
        ("lead_history", "old_event_date", "TEXT NOT NULL DEFAULT ''"),
        ("events", "external_id", "TEXT NOT NULL DEFAULT ''"),
        ("events", "my_notes", "TEXT NOT NULL DEFAULT ''"),
        ("events", "owner_id", "INTEGER"),
        ("events", "ical_uid", "TEXT NOT NULL DEFAULT ''"),          # Google's id for one meeting across everyone's calendars (no duplicates)
        ("events", "google_calendar", "TEXT NOT NULL DEFAULT ''"),   # which of the person's Google calendars it is in ('' = their main one)
        ("events", "expect_recording", "INTEGER NOT NULL DEFAULT 0"),
        ("events", "attendees", "TEXT NOT NULL DEFAULT '[]'"),
        ("events", "join_url", "TEXT NOT NULL DEFAULT ''"),
        ("users", "access", "TEXT NOT NULL DEFAULT 'funnels'"),
        ("users", "share_emails", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "last_login_at", "TEXT NOT NULL DEFAULT ''"),
        ("events", "meeting_status", "TEXT NOT NULL DEFAULT ''"),
        ("events", "has_call", "INTEGER NOT NULL DEFAULT 0"),
        ("events", "untimed", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:      # two requests upgrading at the same moment: the other one won, which is fine
                if "duplicate column" not in str(exc).lower():
                    raise
    if conn.execute("SELECT COUNT(*) FROM status_tasks").fetchone()[0] == 0:
        conn.execute("INSERT INTO status_tasks (channel, status_key, title) VALUES ('instagram', 'ai replied', 'AI gatekeeper issue')")
    if conn.execute("SELECT COUNT(*) FROM status_stages").fetchone()[0] == 0:
        conn.executemany("INSERT INTO status_stages (channel, status_key, stage_name) VALUES (?, ?, ?)",
                         [(ch, key, st) for (ch, key), st in DEFAULT_STAGES.items()])
    if conn.execute("SELECT COUNT(*) FROM status_buckets").fetchone()[0] == 0:
        conn.executemany("INSERT OR IGNORE INTO status_buckets (channel, status_key, bucket) VALUES (?, ?, ?)",
                         [(ch, key, b) for (ch, key), b in DEFAULT_BUCKETS.items()])
    # LinkedIn's "not interested" came later: existing workspaces get its bucket and stage once (a choice already made stays)
    conn.execute("INSERT OR IGNORE INTO status_buckets (channel, status_key, bucket) VALUES ('linkedin', 'done', 'closed')")
    conn.execute("INSERT OR IGNORE INTO status_stages (channel, status_key, stage_name) VALUES ('linkedin', 'done', ?)",
                 (DEFAULT_STAGES[("linkedin", "done")],))
    if conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0] == 0 and _is_home_workspace():    # a new USER makes their own brands
        conn.executemany("INSERT OR IGNORE INTO brands (key, name, color, position) VALUES (?, ?, ?, ?)",
                         [(k, n, c, i) for i, (k, n, c) in enumerate(DEFAULT_BRANDS)])
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_events_external ON events(external_id) WHERE external_id != ''")
    return conn


def brand_keys() -> list:
    """The brand keys, in order. A plain read (no schema work), safe to call while another connection is writing."""
    try:
        raw = sqlite3.connect(db_path(), timeout=10)
        try:
            return [r[0] for r in raw.execute("SELECT key FROM brands ORDER BY position, created_at, key")]
        finally:
            raw.close()
    except sqlite3.Error:
        return [k for k, _, _ in DEFAULT_BRANDS]


def default_project(preferred: str = "") -> str:
    """preferred if it is still a brand, else LyteKeepr, else the first brand."""
    keys = brand_keys()
    for k in (preferred, "lytekeepr"):
        if k and k in keys:
            return k
    return keys[0] if keys else "lytekeepr"


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def check_not_past(start_date: str, start_time: str = "", untimed: bool = False) -> None:
    """New items can't start in the past (a past date, or a time earlier today that has gone by)."""
    now = now_local()
    day = _parse_date(start_date, "start_date")
    if day < now.date():
        raise ValidationError("That date has passed. Pick today or a later date.")
    if not untimed and day == now.date() and start_time and start_time[:5] < now.strftime("%H:%M"):
        raise ValidationError("That time has passed. Pick a later time.")


def _parse_date(value, field="date") -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError(f"{field} must be YYYY-MM-DD")


def _parse_time(value) -> str:
    try:
        return datetime.strptime(str(value), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValidationError("time must be HH:MM")


def clean(data: dict, partial_from: dict | None = None) -> dict:
    """Validate/normalise an event payload. partial_from fills missing keys."""
    base = dict(partial_from or {})
    base.update({k: v for k, v in data.items() if v is not None})

    title = str(base.get("title", "")).strip()
    if not title:
        raise ValidationError("title is required")
    project = base.get("project") or default_project()
    if project == PERSONAL:
        if base.get("kind", "task") == "meeting":
            raise ValidationError("A meeting can't be personal: personal is for tasks and reminders.")
        if str(base.get("lead_ref") or ""):
            raise ValidationError("A personal task can't be linked to a contact or deal.")
    elif project not in CALENDAR_SOURCES:
        keys = brand_keys()
        if project not in keys:
            raise ValidationError(f"project must be one of the brands: {', '.join(keys)}")
    kind = base.get("kind", "task")
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
    start_date = _parse_date(base.get("start_date"), "start_date")
    start_time = _parse_time(base.get("start_time", "09:00"))
    try:
        duration = int(base.get("duration_min") or DURATIONS[kind])
    except (TypeError, ValueError):
        raise ValidationError("duration_min must be a number")
    if not 5 <= duration <= 24 * 60:
        raise ValidationError("duration_min must be between 5 and 1440")
    repeat = base.get("repeat", "none")
    if repeat not in REPEATS:
        raise ValidationError(f"repeat must be one of {', '.join(REPEATS)}")
    days = base.get("repeat_days", "")
    if isinstance(days, (list, tuple)):
        days = ",".join(str(d) for d in days)
    day_list = sorted({int(d) for d in str(days).split(",") if d.strip() != ""}) if days != "" else []
    if any(d < 0 or d > 6 for d in day_list):
        raise ValidationError("repeat_days must be 0 (Mon) to 6 (Sun)")
    if repeat == "weekly" and not day_list:
        day_list = [start_date.weekday()]
    if repeat != "weekly":
        day_list = []
    return {
        "title": title[:200],
        "project": project,
        "kind": kind,
        "start_date": start_date.isoformat(),
        "start_time": start_time,
        "duration_min": duration,
        "repeat": repeat,
        "repeat_days": ",".join(str(d) for d in day_list),
        "notes": str(base.get("notes", ""))[:2000],
        "untimed": 1 if base.get("untimed") else 0,
        "source": str(base.get("source", "manual"))[:40],
        "lead_ref": str(base.get("lead_ref", ""))[:200],
    }


def _viewer():
    """Who is looking: a team member's id, or None for the admin (also scripts and the scheduled run, which work for the admin)."""
    from . import users
    return None if users.is_admin() else users.current_id()


def _admin_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def is_mine(ev: dict, conn=None) -> bool:
    """A personal item belongs to whoever made it (no owner = the admin's); everything else isn't private."""
    if (ev or {}).get("project") != PERSONAL:
        return True
    me, owner = _viewer(), ev.get("owner_id")
    if me is not None:
        return owner == me
    if owner is None:
        return True
    own = conn is None
    conn = conn or _conn()
    try:
        return owner == _admin_id(conn)
    finally:
        if own:
            conn.close()


def _touchable(event_id: int, conn) -> dict | None:
    ev = get_event(event_id, conn)
    return ev if ev and is_mine(ev, conn) else None


def create_event(data: dict) -> dict:
    row = clean(data)
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO events (title, project, kind, start_date, start_time, duration_min,
               repeat, repeat_days, notes, source, lead_ref, created_at, untimed)
               VALUES (:title, :project, :kind, :start_date, :start_time, :duration_min,
               :repeat, :repeat_days, :notes, :source, :lead_ref, :created_at, :untimed)""",
            {**row, "created_at": now_local().isoformat()},
        )
        return get_event(cur.lastrowid, conn)


def get_event(event_id: int, conn=None) -> dict | None:
    own = conn is None
    conn = conn or _conn()
    try:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def update_event(event_id: int, data: dict) -> dict | None:
    with _conn() as conn:
        current = _touchable(event_id, conn)
        if not current:
            return None
        if current["source"] == "google":
            raise ValidationError(GOOGLE_READ_ONLY)
        row = clean(data, partial_from=current)
        row["source"] = current["source"]
        row["lead_ref"] = current["lead_ref"]
        conn.execute(
            """UPDATE events SET title=:title, project=:project, kind=:kind, start_date=:start_date,
               start_time=:start_time, duration_min=:duration_min, repeat=:repeat,
               repeat_days=:repeat_days, notes=:notes, untimed=:untimed WHERE id=:id""",
            {**row, "id": event_id},
        )
        return get_event(event_id, conn)


def delete_event(event_id: int) -> bool:
    with _conn() as conn:
        current = _touchable(event_id, conn)
        if not current:
            return False
        if current["source"] == "google":
            raise ValidationError(GOOGLE_READ_ONLY)
        return conn.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0


def set_done(event_id: int, occ_date: str, done: bool) -> bool:
    _parse_date(occ_date, "date")
    with _conn() as conn:
        if not _touchable(event_id, conn):
            return False
        if done:
            conn.execute("INSERT OR IGNORE INTO completions VALUES (?, ?)", (event_id, occ_date))
        else:
            conn.execute("DELETE FROM completions WHERE event_id=? AND occ_date=?", (event_id, occ_date))
        return True


def _occurs_on(ev: dict, day: date) -> bool:
    if day < date.fromisoformat(ev["start_date"]):
        return False
    repeat = ev["repeat"]
    if repeat == "none":
        return day.isoformat() == ev["start_date"]
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return day.weekday() < 5
    if repeat == "weekly":
        days = [int(d) for d in ev["repeat_days"].split(",") if d != ""]
        return day.weekday() in days
    return False


def occurrences(start: str, end: str) -> list[dict]:
    """All occurrences with date in [start, end] inclusive, sorted by time."""
    d0, d1 = _parse_date(start, "start"), _parse_date(end, "end")
    if d1 < d0:
        raise ValidationError("end must not be before start")
    if (d1 - d0).days > MAX_RANGE_DAYS:
        raise ValidationError(f"range too large (max {MAX_RANGE_DAYS} days)")
    with _conn() as conn:
        admin_id = _admin_id(conn)
        me = _viewer()
        events = [dict(r) for r in conn.execute("SELECT * FROM events")
                  if r["project"] != PERSONAL or r["owner_id"] == me or (me is None and r["owner_id"] in (None, admin_id))]
        done = {(r["event_id"], r["occ_date"]) for r in conn.execute(
            "SELECT event_id, occ_date FROM completions WHERE occ_date BETWEEN ? AND ?", (start, end))}
        leads = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM leads")}
        buckets = bucket_map(conn)
        for l in leads.values():
            l["bucket"] = buckets.get((l["channel"], l["status"]), "none")
    out = []
    day = d0
    while day <= d1:
        iso = day.isoformat()
        for ev in events:
            if _occurs_on(ev, day):
                lead = None
                if ev["lead_ref"].startswith("lead:"):
                    lead = leads.get(int(ev["lead_ref"][5:] or 0))
                out.append({**ev, "date": iso, "done": (ev["id"], iso) in done, "lead": lead})
        day += timedelta(days=1)
    out.sort(key=lambda o: (o["date"], o["start_time"], o["id"]))
    return out


def agenda() -> dict:
    now = now_local()
    today = now.date()
    past = occurrences((today - timedelta(days=30)).isoformat(), (today - timedelta(days=1)).isoformat())
    # Recurring items you skipped aren't "overdue" — that would pile up daily.
    overdue = [o for o in past if o["repeat"] == "none" and not o["done"] and o["source"] != "google"]
    todays = occurrences(today.isoformat(), today.isoformat())
    upcoming = occurrences((today + timedelta(days=1)).isoformat(), (today + timedelta(days=7)).isoformat())
    return {
        "now": now.strftime("%Y-%m-%dT%H:%M"),
        "overdue": overdue,
        "today": todays,
        "upcoming": upcoming,
        "badge": len(overdue) + sum(1 for o in todays if not o["done"]),
    }


def move_overdue(to: str = "today", allowed=None) -> dict:
    """Move every overdue one-off item to today (or tomorrow), keeping its time of day. allowed(item): only the ones the person may change."""
    if to not in ("today", "tomorrow"):
        raise ValidationError("to must be 'today' or 'tomorrow'")
    target = now_local().date() + timedelta(days=1 if to == "tomorrow" else 0)
    ids = [o["id"] for o in agenda()["overdue"] if allowed is None or allowed(o)]
    with _conn() as conn:
        conn.executemany("UPDATE events SET start_date = ? WHERE id = ? AND repeat = 'none'",
                         [(target.isoformat(), i) for i in ids])
    return {"moved": len(ids), "to": target.isoformat()}


# ---------------- assistant chat history (kept so a refresh or sign-in doesn't lose the conversation) ----------------
CHAT_KEEP = 100


def chat_append(role: str, content: str) -> None:
    if role not in ("user", "assistant") or not str(content or "").strip():
        return
    with _conn() as conn:
        conn.execute("INSERT INTO chat_messages (role, content, created_at) VALUES (?, ?, ?)",
                     (role, str(content)[:20000], now_local().isoformat()))
        conn.execute("DELETE FROM chat_messages WHERE id NOT IN (SELECT id FROM chat_messages ORDER BY id DESC LIMIT ?)", (CHAT_KEEP,))


def chat_history() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT role, content, created_at FROM chat_messages ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def chat_clear() -> int:
    with _conn() as conn:
        return conn.execute("DELETE FROM chat_messages").rowcount


# ---------------- status buckets (cold / warm / closed) ----------------
def bucket_for(conn, channel: str, status_key: str) -> str:
    row = conn.execute("SELECT bucket FROM status_buckets WHERE channel = ? AND status_key = ?", (channel, status_key)).fetchone()
    return row["bucket"] if row else "none"


def bucket_map(conn) -> dict:
    return {(r["channel"], r["status_key"]): r["bucket"] for r in conn.execute("SELECT * FROM status_buckets")}


def set_bucket(channel: str, status_key: str, bucket: str) -> None:
    if bucket not in BUCKETS:
        raise ValidationError("bucket must be cold, warm, closed or none")
    with _conn() as conn:
        conn.execute("INSERT INTO status_buckets (channel, status_key, bucket) VALUES (?, ?, ?) "
                     "ON CONFLICT(channel, status_key) DO UPDATE SET bucket = excluded.bucket", (channel, status_key, bucket))


# ---------------- meetings: my notes, status, and the follow-up after a completed call ----------------
def get_state(key: str) -> str | None:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))


def meeting_follow_up(conn, ev: dict) -> str | None:
    """A completed conference call gets one reminder 24 hours after it ended; any other state removes it."""
    ref = f"meeting:{ev['id']}"
    existing = conn.execute("SELECT * FROM events WHERE lead_ref = ? AND source = 'auto'", (ref,)).fetchone()
    if ev["has_call"] and ev["meeting_status"] == "completed":
        end = datetime.fromisoformat(f"{ev['start_date']}T{ev['start_time']}") + timedelta(minutes=ev["duration_min"])
        due = end + timedelta(hours=FOLLOW_UP_AFTER_CALL_HOURS)          # the day after the call ends; no clock time
        fields = clean({"title": f"Follow up: {ev['title']}", "project": ev["project"], "kind": "reminder",
                        "start_date": due.date().isoformat(), "start_time": UNTIMED_TIME, "duration_min": 15,
                        "source": "auto", "lead_ref": ref, "untimed": 1})
        if not existing:
            conn.execute("""INSERT INTO events (title, project, kind, start_date, start_time, duration_min, repeat, repeat_days,
                            notes, source, lead_ref, created_at, untimed) VALUES (:title, :project, :kind, :start_date, :start_time,
                            :duration_min, :repeat, :repeat_days, :notes, :source, :lead_ref, :created_at, :untimed)""",
                         {**fields, "created_at": now_local().isoformat()})
            return "created"
        if existing["start_date"] != fields["start_date"] or existing["untimed"] != 1:
            conn.execute("UPDATE events SET start_date = ?, start_time = ?, untimed = 1, title = ? WHERE id = ?",
                         (fields["start_date"], fields["start_time"], fields["title"], existing["id"]))
            conn.execute("DELETE FROM completions WHERE event_id = ?", (existing["id"],))
            return "moved"
        return None
    if existing:
        conn.execute("DELETE FROM events WHERE id = ?", (existing["id"],))
        return "removed"
    return None


def set_meeting(event_id: int, meeting_status: str | None = None, my_notes: str | None = None, expect_recording: bool | None = None) -> dict | None:
    """Your own notes and meeting status. Works on every event, including read-only Google ones; Google syncs never touch these."""
    with _conn() as conn:
        ev = get_event(event_id, conn)
        if not ev:
            return None
        if meeting_status is not None:
            key = MEETING_ALIASES.get(re.sub(r"[\s_\-]+", " ", str(meeting_status).strip().lower()).replace("no show", "no show"))
            if key is None:
                raise ValidationError("meeting status must be one of: scheduled, completed, no show, cancelled")
            conn.execute("UPDATE events SET meeting_status = ? WHERE id = ?", (MEETING_STATUSES[key], event_id))
        if my_notes is not None:
            conn.execute("UPDATE events SET my_notes = ? WHERE id = ?", (str(my_notes)[:4000], event_id))
        if expect_recording is not None:                 # whether Fathom should record it (a LyteKeepr setting; Google is not touched)
            conn.execute("UPDATE events SET expect_recording = ? WHERE id = ?", (1 if expect_recording else 0, event_id))
        ev = get_event(event_id, conn)
        meeting_follow_up(conn, ev)
        return ev


# ---------------- small saved settings (key -> text), e.g. the name the user signs emails with
def get_setting(key: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (f"setting:{key}",)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", (f"setting:{key}", value))
