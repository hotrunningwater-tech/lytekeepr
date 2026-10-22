"""Goals on Home: a few priorities for today and broader ones for this week, ticked off when done.

- Per person: everyone has their own goals. Nobody assigns goals to others.
- Private by default; a goal marked "visible to the team" can be seen (not changed) by the others in the workspace.
- A daily goal belongs to one day; a weekly goal to one week (weeks start on Monday). Yesterday's unfinished daily goals are offered
  again ("move to today"); nothing moves by itself.
"""

from datetime import date, timedelta

from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    who TEXT NOT NULL,                 -- 'admin' or 'user:<id>'
    period TEXT NOT NULL,              -- 'day' | 'week'
    period_start TEXT NOT NULL,        -- the day, or the Monday of the week
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    shared INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_goals_who ON goals(who, period, period_start);
"""


class GoalError(ValueError):
    pass


def _me() -> str:
    from . import users
    return "admin" if users.is_admin() else f"user:{users.current_id()}"


def _today() -> date:
    return store.now_local().date()


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _conn():
    conn = store._conn()
    conn.executescript(SCHEMA)
    return conn


def _row(r) -> dict:
    return {"goal_id": r["goal_id"], "period": r["period"], "period_start": r["period_start"], "text": r["text"],
            "done": bool(r["done"]), "shared": bool(r["shared"]), "done_at": r["done_at"]}


def _names() -> dict:
    with store._conn() as conn:
        rows = conn.execute("SELECT id, name, email, role FROM users").fetchall()
    out = {}
    for r in rows:
        key = "admin" if r["role"] == "admin" else f"user:{r['id']}"
        out[key] = r["name"] or (r["email"] or "").split("@")[0]
    return out


def overview() -> dict:
    """Today's goals, this week's, yesterday's unfinished ones, and teammates' goals they chose to show."""
    me, today = _me(), _today()
    week = _monday(today)
    with _conn() as conn:
        def mine(period, start):
            return [_row(r) for r in conn.execute("""SELECT * FROM goals WHERE who = ? AND period = ? AND period_start = ? AND deleted_at = ''
                                                     ORDER BY position, goal_id""", (me, period, start.isoformat()))]
        today_goals, week_goals = mine("day", today), mine("week", week)
        open_before = [_row(r) for r in conn.execute("""SELECT * FROM goals WHERE who = ? AND period = 'day' AND period_start < ? AND period_start >= ?
                                                        AND done = 0 AND deleted_at = '' ORDER BY period_start DESC, goal_id""",
                                                     (me, today.isoformat(), (today - timedelta(days=7)).isoformat()))]
        shared = conn.execute("""SELECT * FROM goals WHERE who != ? AND shared = 1 AND deleted_at = '' AND
                                 ((period = 'day' AND period_start = ?) OR (period = 'week' AND period_start = ?)) ORDER BY who, period, goal_id""",
                              (me, today.isoformat(), week.isoformat())).fetchall()
    names, team = _names(), {}
    for r in shared:
        t = team.setdefault(r["who"], {"who": names.get(r["who"], "Teammate"), "today": [], "week": []})
        t["today" if r["period"] == "day" else "week"].append(_row(r))
    return {"today": today.isoformat(), "week_start": week.isoformat(), "today_goals": today_goals, "week_goals": week_goals,
            "earlier_open": open_before, "team": list(team.values())}


def add(text: str, period: str = "day", shared: bool = False) -> dict:
    text = (text or "").strip()[:300]
    if not text:
        raise GoalError("Write the goal first.")
    if period not in ("day", "week"):
        raise GoalError("A goal is for today (day) or this week (week).")
    start = _today() if period == "day" else _monday(_today())
    with _conn() as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM goals WHERE who = ? AND period = ? AND period_start = ?",
                           (_me(), period, start.isoformat())).fetchone()[0]
        cur = conn.execute("INSERT INTO goals (who, period, period_start, text, shared, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (_me(), period, start.isoformat(), text, 1 if shared else 0, pos, store.now_local().isoformat(timespec="minutes")))
        return _row(conn.execute("SELECT * FROM goals WHERE goal_id = ?", (cur.lastrowid,)).fetchone())


def _mine(conn, goal_id):
    r = conn.execute("SELECT * FROM goals WHERE goal_id = ? AND deleted_at = ''", (int(goal_id),)).fetchone()
    if not r or r["who"] != _me():
        raise GoalError("No such goal.")              # someone else's goal can be seen when shared, never changed
    return r


def update(goal_id: int, text=None, done=None, shared=None) -> dict:
    with _conn() as conn:
        r = _mine(conn, goal_id)
        sets = {}
        if text is not None and text.strip():
            sets["text"] = text.strip()[:300]
        if done is not None:
            sets["done"] = 1 if done else 0
            sets["done_at"] = store.now_local().isoformat(timespec="minutes") if done else ""
        if shared is not None:
            sets["shared"] = 1 if shared else 0
        if sets:
            conn.execute(f"UPDATE goals SET {', '.join(k + ' = :' + k for k in sets)} WHERE goal_id = :id", {**sets, "id": r["goal_id"]})
        return _row(conn.execute("SELECT * FROM goals WHERE goal_id = ?", (r["goal_id"],)).fetchone())


def to_today(goal_id: int) -> dict:
    """An unfinished goal from an earlier day becomes one of today's."""
    with _conn() as conn:
        r = _mine(conn, goal_id)
        if r["period"] != "day":
            raise GoalError("Only daily goals move to today.")
        conn.execute("UPDATE goals SET period_start = ? WHERE goal_id = ?", (_today().isoformat(), r["goal_id"]))
        return _row(conn.execute("SELECT * FROM goals WHERE goal_id = ?", (r["goal_id"],)).fetchone())


def delete(goal_id: int) -> None:
    with _conn() as conn:
        r = _mine(conn, goal_id)
        conn.execute("UPDATE goals SET deleted_at = ? WHERE goal_id = ?", (store.now_local().isoformat(timespec="minutes"), r["goal_id"]))


def register(app):
    from flask import jsonify, request

    def run(fn, *a):
        try:
            out = fn(*a)
            return jsonify(out if out is not None else {"ok": True})
        except GoalError as exc:
            return jsonify({"error": str(exc)}), 400

    def b():
        return request.get_json(silent=True) or {}

    @app.get("/api/calendar/goals")
    def goals_overview():
        return run(overview)

    @app.post("/api/calendar/goals")
    def goals_add():
        x = b()
        return run(add, x.get("text", ""), x.get("period") or "day", bool(x.get("shared")))

    @app.put("/api/calendar/goals/<int:goal_id>")
    def goals_update(goal_id):
        x = b()
        return run(update, goal_id, x.get("text"), x.get("done") if "done" in x else None, x.get("shared") if "shared" in x else None)

    @app.post("/api/calendar/goals/<int:goal_id>/to-today")
    def goals_to_today(goal_id):
        return run(to_today, goal_id)

    @app.post("/api/calendar/goals/<int:goal_id>/delete")
    def goals_delete(goal_id):
        return run(delete, goal_id)
