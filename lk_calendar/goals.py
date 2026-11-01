"""Goals: a few priorities for today and this week (on Home), and longer ones kept out of sight until asked for.

- Levels: day, week, then short term (month, about 1 month), mid term (mid, 2-4 months) and long term (year, 1 year or more).
  A daily goal belongs to one day and a weekly goal to one week (weeks start on Monday); for the longer levels period_start is
  the target date.
- Each goal is for a brand or Personal (brand ''). Daily and weekly goals made on Home are Personal, as before.
- Per person: everyone has their own goals. Nobody assigns goals to others. Private by default; a goal marked "visible to the
  team" can be seen (not changed) by the others: a brand's goal only by those who may see that brand. Personal longer-term
  goals are never shown to anyone else.
- Sub-goals (steps): one level deep, under any goal; they share its brand. Deleting a goal deletes its steps.
- done_at is when it was ticked off; unticking clears it.
- Yesterday's unfinished daily goals are offered again ("move to today"); nothing moves by itself.
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
LEVELS = {"day": "Today", "week": "This week", "month": "Short term", "mid": "Mid term", "year": "Long term"}
DEFAULT_DAYS = {"month": 30, "mid": 90, "year": 365}           # a target date when none is given
LEVEL_WORDS = {"daily": "day", "today": "day", "weekly": "week", "short": "month", "short term": "month", "monthly": "month",
               "mid term": "mid", "medium": "mid", "quarter": "mid", "long": "year", "long term": "year", "yearly": "year"}


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
    have = {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}
    for column, ddl in (("brand", "TEXT NOT NULL DEFAULT ''"), ("parent_id", "INTEGER")):
        if column not in have:
            try:
                conn.execute(f"ALTER TABLE goals ADD COLUMN {column} {ddl}")
            except Exception as exc:                       # two requests upgrading at the same moment: the other one won
                if "duplicate column" not in str(exc).lower():
                    raise
    return conn


def _row(r) -> dict:
    out = {"goal_id": r["goal_id"], "period": r["period"], "level": LEVELS.get(r["period"], r["period"]), "period_start": r["period_start"],
           "text": r["text"], "done": bool(r["done"]), "shared": bool(r["shared"]), "done_at": r["done_at"],
           "brand": r["brand"] or "personal", "parent_id": r["parent_id"]}
    if r["period"] in DEFAULT_DAYS:
        out["target_date"] = r["period_start"]
    return out


def _level(period) -> str:
    p = str(period or "day").strip().lower()
    p = LEVEL_WORDS.get(p, p)
    if p not in LEVELS:
        raise GoalError("The level is one of: day, week, month (short term), mid (2-4 months), year (long term).")
    return p


def _brand(brand) -> str:
    """'' for Personal, else a brand key this person may see."""
    from . import access, brands
    b = str(brand or "").strip()
    if not b or b.lower() == "personal":
        return ""
    try:
        key = brands.resolve(b)
    except store.ValidationError as exc:
        raise GoalError(str(exc))
    if not access.brand_allowed(key):
        raise GoalError("You don't have access to that brand.")
    return key


def _start(level: str, target_date=None) -> str:
    today = _today()
    if level == "day":
        return today.isoformat()
    if level == "week":
        return _monday(today).isoformat()
    if target_date:
        try:
            return date.fromisoformat(str(target_date)[:10]).isoformat()
        except ValueError:
            raise GoalError("The target date is YYYY-MM-DD.")
    return (today + timedelta(days=DEFAULT_DAYS[level])).isoformat()


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
    from . import access
    names, team = _names(), {}
    for r in shared:
        if r["brand"] and not access.brand_allowed(r["brand"]):
            continue
        t = team.setdefault(r["who"], {"who": names.get(r["who"], "Teammate"), "today": [], "week": []})
        t["today" if r["period"] == "day" else "week"].append(_row(r))
    return {"today": today.isoformat(), "week_start": week.isoformat(), "today_goals": today_goals, "week_goals": week_goals,
            "earlier_open": open_before, "team": list(team.values())}


def plan(brand=None, level=None, include_done: bool = True) -> dict:
    """All of my goals at every level (or one level), for one brand or Personal or all, each with its steps.
    Today's and this week's are the current ones; the longer levels are every open one plus those done in the last 30 days."""
    me, today = _me(), _today()
    key = None if brand in (None, "", "all") else _brand(brand)
    only = _level(level) if level else None
    with _conn() as conn:
        rows = [r for r in conn.execute("SELECT * FROM goals WHERE who = ? AND deleted_at = '' ORDER BY period_start, position, goal_id", (me,))]
    def current(r):
        if r["period"] == "day":
            return r["period_start"] == today.isoformat()
        if r["period"] == "week":
            return r["period_start"] == _monday(today).isoformat()
        return include_done or not r["done"]
    recent = (today - timedelta(days=30)).isoformat()
    picked = [r for r in rows if (key is None or r["brand"] == key) and current(r)
              and not (r["period"] in DEFAULT_DAYS and r["done"] and (r["done_at"] or "")[:10] < recent)]
    steps: dict = {}
    for r in rows:
        if r["parent_id"]:
            steps.setdefault(r["parent_id"], []).append(_row(r))
    out = {lv: [] for lv in ("year", "mid", "month", "week", "day")}
    tops = {r["goal_id"] for r in picked if not r["parent_id"]}
    for r in picked:
        if r["parent_id"] in tops:
            continue                                               # shown under its goal
        g = _row(r)
        if r["parent_id"]:
            g["part_of"] = next((x["text"] for x in rows if x["goal_id"] == r["parent_id"]), "")
        mine_steps = steps.get(r["goal_id"], [])
        if mine_steps:
            g["steps"] = mine_steps
            g["steps_done"] = sum(1 for s in mine_steps if s["done"])
        out[r["period"]].append(g)
    if only:
        out = {only: out[only]}
    from . import access, brands as brand_mod
    return {"brand": "all" if key is None else (key or "personal"), "levels": LEVELS, "goals": out,
            "brands": [{"key": "personal", "name": "Personal"}] + [{"key": b["key"], "name": b["name"]} for b in brand_mod.all_brands()
                                                                   if access.brand_allowed(b["key"])]}


def add(text: str, period: str = "day", shared: bool = False, brand=None, target_date=None, parent_id=None) -> dict:
    text = (text or "").strip()[:300]
    if not text:
        raise GoalError("Write the goal first.")
    level = _level(period)
    with _conn() as conn:
        if parent_id:
            parent = _mine(conn, parent_id)
            if parent["parent_id"]:
                raise GoalError("A step can't have steps of its own: add it under the main goal.")
            key = parent["brand"]                                  # a step shares its goal's brand
        else:
            key = _brand(brand)
        start = _start(level, target_date)
        if level in DEFAULT_DAYS and start < _today().isoformat():
            raise GoalError("That target date has passed.")
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM goals WHERE who = ? AND period = ? AND period_start = ?",
                           (_me(), level, start)).fetchone()[0]
        cur = conn.execute("""INSERT INTO goals (who, period, period_start, text, shared, position, created_at, brand, parent_id)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (_me(), level, start, text, 1 if shared and (key or level in ("day", "week")) else 0, pos,
                            store.now_local().isoformat(timespec="minutes"), key, int(parent_id) if parent_id else None))
        return _row(conn.execute("SELECT * FROM goals WHERE goal_id = ?", (cur.lastrowid,)).fetchone())


def _mine(conn, goal_id):
    r = conn.execute("SELECT * FROM goals WHERE goal_id = ? AND deleted_at = ''", (int(goal_id),)).fetchone()
    if not r or r["who"] != _me():
        raise GoalError("No such goal.")              # someone else's goal can be seen when shared, never changed
    return r


def update(goal_id: int, text=None, done=None, shared=None, period=None, target_date=None, brand=None) -> dict:
    with _conn() as conn:
        r = _mine(conn, goal_id)
        sets = {}
        if text is not None and text.strip():
            sets["text"] = text.strip()[:300]
        if period is not None or target_date is not None:
            level = _level(period) if period is not None else r["period"]
            keep = target_date is None and level == r["period"] and level in DEFAULT_DAYS
            sets["period"], sets["period_start"] = level, r["period_start"] if keep else _start(level, target_date)
        if brand is not None:
            if r["parent_id"]:
                raise GoalError("A step has its goal's brand: change the main goal.")
            sets["brand"] = _brand(brand)
            conn.execute("UPDATE goals SET brand = ? WHERE parent_id = ?", (sets["brand"], r["goal_id"]))      # its steps go along
        if done is not None:
            sets["done"] = 1 if done else 0
            sets["done_at"] = store.now_local().isoformat(timespec="minutes") if done else ""
        if shared is not None:
            sets["shared"] = 1 if shared else 0
        if sets.get("shared") and not sets.get("brand", r["brand"]) and sets.get("period", r["period"]) in DEFAULT_DAYS:
            raise GoalError("A personal goal of that level stays private.")
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
        conn.execute("UPDATE goals SET deleted_at = ? WHERE goal_id = ? OR parent_id = ?",
                     (store.now_local().isoformat(timespec="minutes"), r["goal_id"], r["goal_id"]))          # its steps go too


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
        return run(add, x.get("text", ""), x.get("period") or "day", bool(x.get("shared")), x.get("brand"), x.get("target_date"), x.get("parent_id"))

    @app.put("/api/calendar/goals/<int:goal_id>")
    def goals_update(goal_id):
        x = b()
        return run(update, goal_id, x.get("text"), x.get("done") if "done" in x else None, x.get("shared") if "shared" in x else None,
                   x.get("period"), x.get("target_date"), x.get("brand"))

    @app.get("/api/calendar/goals/plan")
    def goals_plan():
        a = request.args
        return run(plan, a.get("brand"), a.get("level"), a.get("include_done", "1") != "0")

    @app.post("/api/calendar/goals/<int:goal_id>/to-today")
    def goals_to_today(goal_id):
        return run(to_today, goal_id)

    @app.post("/api/calendar/goals/<int:goal_id>/delete")
    def goals_delete(goal_id):
        return run(delete, goal_id)
