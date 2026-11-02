"""Recently deleted contacts: deleting a contact first keeps a copy of everything tied to them (details, channels, messages, notes, statuses,
tasks), which can be restored for 30 days and is then removed for good. Saved email drafts are not kept.
The admin sees everything deleted; a team member sees what they deleted themselves."""

import json
import sqlite3
from datetime import timedelta

from . import store

KEEP_DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS recycle_bin (
    bin_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL,
    deleted_by INTEGER,
    data TEXT NOT NULL
);
"""


class RecycleError(ValueError):
    pass


def _crm():
    from . import contacts
    return contacts._crm()


def _now():
    return store.now_local().isoformat(timespec="minutes")


def _prune(conn) -> None:
    cutoff = (store.now_local() - timedelta(days=KEEP_DAYS)).isoformat(timespec="minutes")
    conn.execute("DELETE FROM recycle_bin WHERE deleted_at < ?", (cutoff,))


def _tables_with_person(conn) -> list:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    return [t for t in names if t != "recycle_bin" and "person_id" in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}]


def _rows(conn, sql, args=()) -> list:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def keep(person_id: int) -> None:
    """Copy everything tied to this person into the bin. Called before anything is deleted; an error here stops the delete."""
    from . import users
    pid = int(person_id)
    data: dict = {"crm": {}, "calendar": {}, "company": None}
    with store._conn() as cal:
        for t in _tables_with_person(cal):
            data["calendar"][t] = _rows(cal, f"SELECT * FROM {t} WHERE person_id = ?", (pid,))
        leads = [r["id"] for r in data["calendar"].get("leads", [])]
        refs = [f"lead:{i}" for i in leads] + [f"contact:{pid}"]
        marks = ",".join("?" for _ in refs)
        data["calendar"]["events"] = _rows(cal, f"SELECT * FROM events WHERE lead_ref IN ({marks})", refs)
        ids = [e["id"] for e in data["calendar"]["events"]]
        data["calendar"]["completions"] = _rows(cal, f"SELECT * FROM completions WHERE event_id IN ({','.join('?' for _ in ids)})", ids) if ids else []
        for t in ("lead_history", "lead_message_seen"):
            exists = cal.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone()
            data["calendar"][t] = _rows(cal, f"SELECT * FROM {t} WHERE lead_id IN ({','.join('?' for _ in leads)})", leads) if exists and leads else []
    with _crm() as conn:
        conn.executescript(SCHEMA)
        for t in _tables_with_person(conn):
            data["crm"][t] = _rows(conn, f"SELECT * FROM {t} WHERE person_id = ?", (pid,))
        person = (data["crm"].get("people") or [None])[0]
        if not person:
            return
        if person.get("company_id"):
            row = conn.execute("SELECT * FROM companies WHERE company_id = ?", (person["company_id"],)).fetchone()
            data["company"] = dict(row) if row else None
        detail = next((a["channel_identifier"] for a in data["crm"].get("channel_accounts", []) if a.get("channel") == "email"), "") \
            or next((a["channel_identifier"] or "" for a in data["crm"].get("channel_accounts", [])), "")
        _prune(conn)
        conn.execute("INSERT INTO recycle_bin (person_id, name, detail, deleted_at, deleted_by, data) VALUES (?, ?, ?, ?, ?, ?)",
                     (pid, person.get("name") or (data["company"] or {}).get("company_name") or "", detail, _now(), users.current_id(), json.dumps(data, default=str)))


def _mine_sql() -> tuple:
    from . import users
    return ("1 = 1", []) if users.is_admin() else ("deleted_by = ?", [users.current_id()])


def listing() -> list:
    cond, args = _mine_sql()
    with _crm() as conn:
        conn.executescript(SCHEMA)
        _prune(conn)
        rows = conn.execute(f"SELECT bin_id, person_id, name, detail, deleted_at FROM recycle_bin WHERE {cond} ORDER BY bin_id DESC", args).fetchall()
    return [{**dict(r), "days_left": max(0, KEEP_DAYS - (store.now_local().date() - store._parse_date(r["deleted_at"][:10], "date")).days)} for r in rows]


def _put_back(conn, table: str, rows: list) -> None:
    if not rows:
        return
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not have:
        return
    for row in rows:
        cols = [c for c in row if c in have]
        try:
            conn.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols])
        except sqlite3.IntegrityError:
            pass                                       # it pointed at something deleted since (a deal, a call): the rest still comes back


def restore(bin_id: int) -> dict:
    cond, args = _mine_sql()
    with _crm() as conn:
        conn.executescript(SCHEMA)
        row = conn.execute(f"SELECT * FROM recycle_bin WHERE bin_id = ? AND {cond}", (int(bin_id), *args)).fetchone()
        if not row:
            raise RecycleError("That contact is no longer in Recently deleted.")
        data = json.loads(row["data"])
        crm_rows = data["crm"]
        person = crm_rows["people"][0]
        if conn.execute("SELECT 1 FROM people WHERE person_id = ?", (person["person_id"],)).fetchone():
            raise RecycleError("That contact already exists.")
        for a in crm_rows.get("channel_accounts", []):
            if a.get("channel_identifier") and conn.execute("SELECT 1 FROM channel_accounts WHERE channel = ? AND channel_identifier = ?", (a["channel"], a["channel_identifier"])).fetchone():
                raise RecycleError(f"Another contact now uses {a['channel_identifier']}. Delete or change that one first.")
        if data.get("company") and not conn.execute("SELECT 1 FROM companies WHERE company_id = ?", (data["company"]["company_id"],)).fetchone():
            _put_back(conn, "companies", [data["company"]])
        person = dict(person)
        if person.get("funnel_id") and not conn.execute("SELECT 1 FROM funnels WHERE funnel_id = ?", (person["funnel_id"],)).fetchone():
            person["funnel_id"], person["funnel_stage_id"] = None, None    # its funnel was deleted since
        elif person.get("funnel_stage_id") and not conn.execute("SELECT 1 FROM funnel_stages WHERE stage_id = ?", (person["funnel_stage_id"],)).fetchone():
            person["funnel_stage_id"] = None
        _put_back(conn, "people", [person])
        first = ("channel_accounts", "messages")
        for t in first + tuple(t for t in crm_rows if t not in first + ("people",)):
            _put_back(conn, t, crm_rows.get(t, []))
        for rec in crm_rows.get("contact_records", []):      # deleting only marked their notes and conversations
            if not rec.get("deleted_at"):
                conn.execute("UPDATE contact_records SET deleted_at = '' WHERE record_id = ?", (rec["record_id"],))
        for a in crm_rows.get("channel_accounts", []):        # the next sync may find them again
            conn.execute("DELETE FROM excluded_contacts WHERE channel = ? AND channel_identifier = ?", (a.get("channel"), a.get("channel_identifier")))
        conn.execute("DELETE FROM recycle_bin WHERE bin_id = ?", (row["bin_id"],))
    with store._conn() as cal:
        order = ["leads", "events", "completions", "lead_history", "lead_message_seen"]
        for t in order + [t for t in data["calendar"] if t not in order]:
            _put_back(cal, t, data["calendar"].get(t, []))
    return {"person_id": person["person_id"], "name": row["name"]}


def register(app):
    from flask import jsonify

    @app.get("/api/crm/recycle")
    def recycle_list():
        return jsonify({"deleted": listing(), "keep_days": KEEP_DAYS})

    @app.post("/api/crm/recycle/<int:bin_id>/restore")
    def recycle_restore(bin_id):
        try:
            return jsonify(restore(bin_id))
        except RecycleError as exc:
            return jsonify({"error": str(exc)}), 400
