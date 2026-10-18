"""Opportunities: funnel entries that are one shared deal, project or partnership with several people on it.

They live next to the existing contact entries (people.funnel_id / funnel_stage_id), which are not changed in any way:
- An opportunity has its own funnel and stage and moves only when someone moves it (never because a participant replied,
  joined or left), so the outreach automation of contact entries never touches it.
- Participants are existing contacts. Each has an optional role, an optional primary flag (never set automatically; at most
  one, or none), active/inactive with joined/left dates, and optionally who introduced them. The same person can also be
  a contact entry in the same funnel, and can be in any number of opportunities.
- "Mark inactive" keeps the person on the opportunity's history; "remove" deletes only the link (added by mistake). A
  contact record is never deleted from here.
- Everything that happens is written to the opportunity's log.
Tasks for an opportunity are ordinary calendar tasks linked with lead_ref "opp:<id>".
"""

from contextlib import contextmanager
from datetime import datetime

from . import store

ROLES = ("primary", "decision-maker", "influencer", "advisor", "introducer", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funnel_id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    company_id INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    owner_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunities_funnel ON opportunities(funnel_id, stage_id);
CREATE TABLE IF NOT EXISTS opportunity_people (
    opportunity_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    introduced_by INTEGER,
    joined_at TEXT NOT NULL,
    left_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (opportunity_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_opportunity_people_person ON opportunity_people(person_id);
CREATE TABLE IF NOT EXISTS opportunity_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    who TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL
);
"""


class OpportunityError(ValueError):
    pass


@contextmanager
def _crm():
    from . import contacts
    with contacts._crm() as conn:
        conn.executescript(SCHEMA)
        yield conn


def _now() -> str:
    return store.now_local().isoformat(timespec="minutes")


def _actor() -> str:
    try:
        from flask import g
        user = getattr(g, "user", None) or {}
        return user.get("name") or user.get("email") or ""
    except RuntimeError:
        return ""


def _log(conn, opp_id: int, text: str) -> None:
    conn.execute("INSERT INTO opportunity_log (opportunity_id, at, who, text) VALUES (?, ?, ?, ?)", (opp_id, _now(), _actor(), text[:500]))


def _person_name(conn, person_id) -> str:
    row = conn.execute("SELECT name FROM people WHERE person_id = ?", (person_id,)).fetchone()
    return (row["name"] if row else "") or f"contact #{person_id}"


def _stage(conn, stage_id) -> dict | None:
    row = conn.execute("SELECT stage_id, funnel_id, name FROM funnel_stages WHERE stage_id = ?", (stage_id,)).fetchone()
    return dict(row) if row else None


def _company_id(conn, company: str | None):
    if not company:
        return None
    from crm import db as crm_db
    return crm_db.get_or_create_company(conn, company.strip()[:200])


# ------------------------------------------------------------------ reading
def _participants(conn, opp_ids: list) -> dict:
    if not opp_ids:
        return {}
    marks = ",".join("?" for _ in opp_ids)
    out: dict = {}
    for r in conn.execute(f"""SELECT op.*, p.name, c.company_name, ib.name AS introduced_by_name
                              FROM opportunity_people op JOIN people p ON p.person_id = op.person_id
                              LEFT JOIN companies c ON c.company_id = p.company_id
                              LEFT JOIN people ib ON ib.person_id = op.introduced_by
                              WHERE op.opportunity_id IN ({marks})
                              ORDER BY op.active DESC, op.is_primary DESC, op.joined_at""", opp_ids):
        out.setdefault(r["opportunity_id"], []).append({
            "contact_id": r["person_id"], "name": r["name"], "company": r["company_name"] or "", "role": r["role"],
            "primary": bool(r["is_primary"]), "active": bool(r["active"]), "introduced_by": r["introduced_by"],
            "introduced_by_name": r["introduced_by_name"] or "", "joined_at": r["joined_at"], "left_at": r["left_at"]})
    from . import access
    if access.person_ids() is not None:            # a team member: people they may not see (another brand) are left out entirely
        for k in out:
            out[k] = [p for p in out[k] if access.can_person(p["contact_id"])]
            for p in out[k]:
                if p["introduced_by"] and not access.can_person(p["introduced_by"]):
                    p["introduced_by"], p["introduced_by_name"] = None, ""
    return out


def summary_line(people: list) -> str:
    """'Anna Weber (primary) · Michael Ross, Sara Lind +2' — active participants, the primary first."""
    active = [p for p in people if p["active"]]
    if not active:
        return "No active participants"
    first = [p for p in active if p["primary"]] or []
    rest = [p for p in active if not p["primary"]]
    parts = [first[0]["name"] + " (lead)"] if first else []
    shown = rest[: (2 if first else 3)]
    tail = ", ".join(p["name"] for p in shown) + (f" +{len(rest) - len(shown)}" if len(rest) > len(shown) else "")
    return " · ".join(x for x in (parts[0] if parts else "", tail) if x)


def _card(row, people: list) -> dict:
    active = [p for p in people if p["active"]]
    companies = []
    for p in active:
        if p["company"] and p["company"] not in companies:
            companies.append(p["company"])
    return {"opportunity_id": row["opportunity_id"], "kind": "opportunity", "name": row["name"], "funnel_id": row["funnel_id"],
            "stage_id": row["stage_id"], "company": row["company_name"] if "company_name" in row.keys() else "",
            "participants": len(active), "summary": summary_line(people), "companies": companies,
            "single_name": active[0]["name"] if len(active) == 1 else "",
            "people_text": " ".join(p["name"] or "" for p in people)}       # every participant, for the funnel search


def cards_for_funnel(funnel_id: int) -> dict:
    """{stage_id: [opportunity card, ...]} for the funnel board."""
    with _crm() as conn:
        rows = conn.execute("""SELECT o.*, c.company_name FROM opportunities o LEFT JOIN companies c ON c.company_id = o.company_id
                               WHERE o.funnel_id = ? ORDER BY o.name COLLATE NOCASE""", (funnel_id,)).fetchall()
        people = _participants(conn, [r["opportunity_id"] for r in rows])
    out: dict = {}
    for r in rows:
        out.setdefault(r["stage_id"], []).append(_card(r, people.get(r["opportunity_id"], [])))
    return out


def counts_by_stage(conn, funnel_id: int) -> dict:
    conn.executescript(SCHEMA)
    return {r["stage_id"]: r["n"] for r in conn.execute("SELECT stage_id, COUNT(*) AS n FROM opportunities WHERE funnel_id = ? GROUP BY stage_id", (funnel_id,))}


def get(opp_id: int) -> dict | None:
    with _crm() as conn:
        row = conn.execute("""SELECT o.*, c.company_name, f.name AS funnel_name, f.brand AS brand, s.name AS stage_name FROM opportunities o
                              LEFT JOIN companies c ON c.company_id = o.company_id LEFT JOIN funnels f ON f.funnel_id = o.funnel_id
                              LEFT JOIN funnel_stages s ON s.stage_id = o.stage_id WHERE o.opportunity_id = ?""", (opp_id,)).fetchone()
        if not row:
            return None
        people = _participants(conn, [opp_id]).get(opp_id, [])
        log = [dict(r) for r in conn.execute("SELECT at, who, text FROM opportunity_log WHERE opportunity_id = ? ORDER BY log_id DESC LIMIT 200", (opp_id,))]
        stages = [dict(r) for r in conn.execute("SELECT stage_id, name FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (row["funnel_id"],))]
    tasks = []
    with store._conn() as cal:
        for e in cal.execute("SELECT id, title, start_date, start_time, untimed, kind FROM events WHERE lead_ref = ? ORDER BY start_date, start_time", (f"opp:{opp_id}",)):
            done = cal.execute("SELECT 1 FROM completions WHERE event_id = ? AND occ_date = ?", (e["id"], e["start_date"])).fetchone()
            tasks.append({**dict(e), "done": bool(done)})
    return {"opportunity_id": row["opportunity_id"], "name": row["name"], "funnel_id": row["funnel_id"], "funnel": row["funnel_name"] or "",
            "brand": row["brand"] or "",                 # an opportunity's brand is its funnel's
            "stage_id": row["stage_id"], "stage": row["stage_name"] or "", "stages": stages, "company": row["company_name"] or "",
            "notes": row["notes"], "created_at": row["created_at"], "updated_at": row["updated_at"], "owner_id": row["owner_id"],
            "participants": people, "summary": summary_line(people), "tasks": tasks, "history": log}


def for_person(person_id: int) -> list[dict]:
    """The opportunities a contact is (or was) on, with their role there."""
    with _crm() as conn:
        rows = conn.execute("""SELECT o.opportunity_id, o.name, o.funnel_id, f.name AS funnel, s.name AS stage, op.role, op.is_primary, op.active, op.left_at
                               FROM opportunity_people op JOIN opportunities o ON o.opportunity_id = op.opportunity_id
                               LEFT JOIN funnels f ON f.funnel_id = o.funnel_id LEFT JOIN funnel_stages s ON s.stage_id = o.stage_id
                               WHERE op.person_id = ? ORDER BY op.active DESC, o.updated_at DESC""", (person_id,)).fetchall()
    return [{"opportunity_id": r["opportunity_id"], "name": r["name"], "funnel_id": r["funnel_id"], "funnel": r["funnel"] or "", "stage": r["stage"] or "",
             "role": r["role"], "primary": bool(r["is_primary"]), "active": bool(r["active"]), "left_at": r["left_at"]} for r in rows]


def search(q: str, funnel_ids: set | None = None, limit: int = 20) -> list[dict]:
    like = f"%{(q or '').strip().lower()}%"
    with _crm() as conn:
        rows = conn.execute("""SELECT DISTINCT o.*, c.company_name FROM opportunities o LEFT JOIN companies c ON c.company_id = o.company_id
                               LEFT JOIN opportunity_people op ON op.opportunity_id = o.opportunity_id LEFT JOIN people p ON p.person_id = op.person_id
                               WHERE lower(o.name) LIKE ? OR lower(IFNULL(c.company_name,'')) LIKE ? OR lower(IFNULL(p.name,'')) LIKE ?
                               ORDER BY o.updated_at DESC LIMIT ?""", (like, like, like, int(limit) * 3)).fetchall()
        rows = [r for r in rows if funnel_ids is None or r["funnel_id"] in funnel_ids][:limit]
        people = _participants(conn, [r["opportunity_id"] for r in rows])
    return [_card(r, people.get(r["opportunity_id"], [])) for r in rows]


# ------------------------------------------------------------------ changes (each one logged)
def create(name: str, funnel_id: int, stage_id: int | None = None, company: str | None = None, notes: str = "",
           participants: list | None = None, owner_id: int | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise OpportunityError("Give the opportunity a name.")
    with _crm() as conn:
        if not conn.execute("SELECT 1 FROM funnels WHERE funnel_id = ?", (funnel_id,)).fetchone():
            raise OpportunityError("No such funnel.")
        if stage_id:
            st = _stage(conn, stage_id)
            if not st or st["funnel_id"] != int(funnel_id):
                raise OpportunityError("That stage is not in this funnel.")
        else:
            first = conn.execute("SELECT stage_id FROM funnel_stages WHERE funnel_id = ? ORDER BY position LIMIT 1", (funnel_id,)).fetchone()
            if not first:
                raise OpportunityError("This funnel has no stages yet.")
            stage_id = first["stage_id"]
        now = _now()
        opp_id = conn.execute("""INSERT INTO opportunities (funnel_id, stage_id, name, company_id, notes, owner_id, created_at, updated_at)
                                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (int(funnel_id), int(stage_id), name[:200], _company_id(conn, company),
                                                                      (notes or "")[:5000], owner_id, now, now)).lastrowid
        _log(conn, opp_id, f"Created in stage “{_stage(conn, stage_id)['name']}”")
    for p in participants or []:
        add_participant(opp_id, int(p.get("contact_id")), p.get("role") or "", bool(p.get("primary")), p.get("introduced_by"))
    return get(opp_id)


def update(opp_id: int, name: str | None = None, notes: str | None = None, company: str | None = None) -> dict:
    with _crm() as conn:
        row = conn.execute("SELECT * FROM opportunities WHERE opportunity_id = ?", (opp_id,)).fetchone()
        if not row:
            raise OpportunityError("No such opportunity.")
        sets, changed = {}, []
        if name is not None and name.strip() and name.strip() != row["name"]:
            sets["name"] = name.strip()[:200]; changed.append(f"Renamed to “{sets['name']}”")
        if notes is not None and notes != row["notes"]:
            sets["notes"] = notes[:5000]; changed.append("Notes updated")
        if company is not None:
            cid = _company_id(conn, company) if company.strip() else None
            if cid != row["company_id"]:
                sets["company_id"] = cid; changed.append(f"Main company set to “{company.strip()}”" if cid else "Main company cleared")
        if sets:
            sets["updated_at"] = _now()
            conn.execute(f"UPDATE opportunities SET {', '.join(k + ' = :' + k for k in sets)} WHERE opportunity_id = :id", {**sets, "id": opp_id})
            for c in changed:
                _log(conn, opp_id, c)
    return get(opp_id)


def move(opp_id: int, stage_id: int) -> dict:
    """Move to another stage of its funnel, or to a stage of another funnel. Only ever done on purpose."""
    with _crm() as conn:
        row = conn.execute("SELECT * FROM opportunities WHERE opportunity_id = ?", (opp_id,)).fetchone()
        st = _stage(conn, stage_id)
        if not row:
            raise OpportunityError("No such opportunity.")
        if not st:
            raise OpportunityError("No such stage.")
        if st["stage_id"] != row["stage_id"]:
            old = _stage(conn, row["stage_id"]) or {"name": "?"}
            conn.execute("UPDATE opportunities SET funnel_id = ?, stage_id = ?, updated_at = ? WHERE opportunity_id = ?", (st["funnel_id"], st["stage_id"], _now(), opp_id))
            _log(conn, opp_id, f"Moved from “{old['name']}” to “{st['name']}”")
    return get(opp_id)


def delete(opp_id: int) -> None:
    """Deletes the opportunity and its participant links and history. Contacts, companies and messages are untouched."""
    with _crm() as conn:
        conn.execute("DELETE FROM opportunity_people WHERE opportunity_id = ?", (opp_id,))
        conn.execute("DELETE FROM opportunity_log WHERE opportunity_id = ?", (opp_id,))
        n = conn.execute("DELETE FROM opportunities WHERE opportunity_id = ?", (opp_id,)).rowcount
    if not n:
        raise OpportunityError("No such opportunity.")
    with store._conn() as cal:                       # its own tasks go too (contacts' tasks are separate and stay)
        cal.execute("DELETE FROM events WHERE lead_ref = ?", (f"opp:{opp_id}",))


def _role(role) -> str:
    r = (role or "").strip().lower().replace("decision maker", "decision-maker")
    return r[:40]


def add_participant(opp_id: int, person_id: int, role: str = "", primary: bool = False, introduced_by: int | None = None) -> dict:
    with _crm() as conn:
        if not conn.execute("SELECT 1 FROM opportunities WHERE opportunity_id = ?", (opp_id,)).fetchone():
            raise OpportunityError("No such opportunity.")
        if not conn.execute("SELECT 1 FROM people WHERE person_id = ?", (person_id,)).fetchone():
            raise OpportunityError("No such contact.")
        if introduced_by and not conn.execute("SELECT 1 FROM people WHERE person_id = ?", (introduced_by,)).fetchone():
            raise OpportunityError("The contact who introduced them was not found.")
        have = conn.execute("SELECT * FROM opportunity_people WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id)).fetchone()
        name = _person_name(conn, person_id)
        if have and have["active"]:
            raise OpportunityError(f"{name} is already on this opportunity.")
        if have:                                     # was inactive: back on, history kept
            conn.execute("UPDATE opportunity_people SET active = 1, left_at = '' WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id))
            _log(conn, opp_id, f"{name} is involved again")
        else:
            conn.execute("""INSERT INTO opportunity_people (opportunity_id, person_id, role, is_primary, active, introduced_by, joined_at)
                            VALUES (?, ?, ?, 0, 1, ?, ?)""", (opp_id, person_id, _role(role), introduced_by, _now()))
            _log(conn, opp_id, f"{name} added" + (f" as {_role(role)}" if _role(role) else "")
                 + (f", introduced by {_person_name(conn, introduced_by)}" if introduced_by else ""))
        conn.execute("UPDATE opportunities SET updated_at = ? WHERE opportunity_id = ?", (_now(), opp_id))
    if primary:
        update_participant(opp_id, person_id, primary=True)
    return get(opp_id)


def update_participant(opp_id: int, person_id: int, role: str | None = None, primary: bool | None = None,
                       active: bool | None = None, introduced_by: int | None = None) -> dict:
    with _crm() as conn:
        have = conn.execute("SELECT * FROM opportunity_people WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id)).fetchone()
        if not have:
            raise OpportunityError("That contact is not on this opportunity.")
        name = _person_name(conn, person_id)
        if role is not None and _role(role) != have["role"]:
            conn.execute("UPDATE opportunity_people SET role = ? WHERE opportunity_id = ? AND person_id = ?", (_role(role), opp_id, person_id))
            _log(conn, opp_id, f"{name}'s role: {_role(role) or 'none'}")
        if primary is True and not have["is_primary"]:      # one primary at most: the previous one stops being primary
            for r in conn.execute("SELECT person_id FROM opportunity_people WHERE opportunity_id = ? AND is_primary = 1", (opp_id,)).fetchall():
                _log(conn, opp_id, f"{_person_name(conn, r['person_id'])} is no longer the primary contact")
            conn.execute("UPDATE opportunity_people SET is_primary = 0 WHERE opportunity_id = ?", (opp_id,))
            conn.execute("UPDATE opportunity_people SET is_primary = 1 WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id))
            _log(conn, opp_id, f"{name} is the primary contact")
        if primary is False and have["is_primary"]:
            conn.execute("UPDATE opportunity_people SET is_primary = 0 WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id))
            _log(conn, opp_id, f"{name} is no longer the primary contact")
        if active is False and have["active"]:
            conn.execute("UPDATE opportunity_people SET active = 0, is_primary = 0, left_at = ? WHERE opportunity_id = ? AND person_id = ?", (_now(), opp_id, person_id))
            _log(conn, opp_id, f"{name} is no longer involved" + (" (was the primary contact)" if have["is_primary"] else ""))
        if active is True and not have["active"]:
            conn.execute("UPDATE opportunity_people SET active = 1, left_at = '' WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id))
            _log(conn, opp_id, f"{name} is involved again")
        if introduced_by is not None and (introduced_by or None) != have["introduced_by"]:
            conn.execute("UPDATE opportunity_people SET introduced_by = ? WHERE opportunity_id = ? AND person_id = ?", (introduced_by or None, opp_id, person_id))
            _log(conn, opp_id, f"{name} was introduced by {_person_name(conn, introduced_by)}" if introduced_by else f"{name}: introducer cleared")
        conn.execute("UPDATE opportunities SET updated_at = ? WHERE opportunity_id = ?", (_now(), opp_id))
    return get(opp_id)


def clear_primary(opp_id: int) -> dict:
    with _crm() as conn:
        for r in conn.execute("SELECT person_id FROM opportunity_people WHERE opportunity_id = ? AND is_primary = 1", (opp_id,)).fetchall():
            _log(conn, opp_id, f"{_person_name(conn, r['person_id'])} is no longer the primary contact")
        conn.execute("UPDATE opportunity_people SET is_primary = 0 WHERE opportunity_id = ?", (opp_id,))
    return get(opp_id)


def remove_participant(opp_id: int, person_id: int) -> dict:
    """Undo a link added by mistake. The contact itself is not touched. (Someone who left: use update_participant(active=False).)"""
    with _crm() as conn:
        n = conn.execute("DELETE FROM opportunity_people WHERE opportunity_id = ? AND person_id = ?", (opp_id, person_id)).rowcount
        if not n:
            raise OpportunityError("That contact is not on this opportunity.")
        conn.execute("UPDATE opportunity_people SET introduced_by = NULL WHERE opportunity_id = ? AND introduced_by = ?", (opp_id, person_id))
        _log(conn, opp_id, f"{_person_name(conn, person_id)} removed (added by mistake)")
    return get(opp_id)


def add_task(opp_id: int, title: str, day: str, time: str | None = None, owner_id: int | None = None) -> dict:
    o = get(opp_id)
    if not o:
        raise OpportunityError("No such opportunity.")
    store.check_not_past(day, time or "", untimed=not time)
    ev = store.create_event({"title": (title or "").strip() or f"Next step: {o['name']}", "project": store.default_project(o.get("brand", "")), "kind": "task", "start_date": day,
                             "start_time": time or "08:00", "untimed": not time, "notes": f"Opportunity: {o['name']}"})
    with store._conn() as cal:
        cal.execute("UPDATE events SET lead_ref = ?, owner_id = ? WHERE id = ?", (f"opp:{opp_id}", owner_id, ev["id"]))
    with _crm() as conn:
        _log(conn, opp_id, f"Task added: {ev['title']} ({day})")
    return get(opp_id)


# ------------------------------------------------------------------ who may see what (funnel sharing)
def visible_person_ids(conn, funnel_ids: set) -> set:
    """Participants of opportunities in the funnels shared with a team member: they may open those contacts."""
    if not funnel_ids:
        return set()
    conn.executescript(SCHEMA)
    marks = ",".join("?" for _ in funnel_ids)
    return {r["person_id"] for r in conn.execute(f"""SELECT op.person_id FROM opportunity_people op JOIN opportunities o ON o.opportunity_id = op.opportunity_id
                                                     WHERE o.funnel_id IN ({marks})""", sorted(funnel_ids))}


def blockers_for_stage(conn, stage_id: int) -> int:
    conn.executescript(SCHEMA)
    return conn.execute("SELECT COUNT(*) FROM opportunities WHERE stage_id = ?", (stage_id,)).fetchone()[0]


def blockers_for_funnel(conn, funnel_id: int) -> int:
    conn.executescript(SCHEMA)
    return conn.execute("SELECT COUNT(*) FROM opportunities WHERE funnel_id = ?", (funnel_id,)).fetchone()[0]


# ------------------------------------------------------------------ routes
def register(app):
    from functools import wraps
    from flask import jsonify, request
    from . import access, users

    def guarded(fn):
        @wraps(fn)
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except (OpportunityError, store.ValidationError) as exc:
                return jsonify({"error": str(exc)}), 400
        return inner

    def body():
        return request.get_json(silent=True) or {}

    def visible(opp_id):
        o = get(opp_id)
        if not o or not access.can_opportunity(o):
            raise OpportunityError("No such opportunity.")
        return o

    def check_person(pid):
        if pid is not None and not access.can_person(pid):
            raise OpportunityError("You don't have access to that contact.")

    @app.get("/api/crm/opportunities")
    @guarded
    def opps_search():
        fid = request.args.get("funnel_id")
        allowed = access.funnel_ids()
        found = search(request.args.get("q", ""), allowed, int(request.args.get("limit") or 20))
        if fid:
            found = [o for o in found if o["funnel_id"] == int(fid)]
        return jsonify({"opportunities": found})

    @app.post("/api/crm/opportunities")
    @guarded
    def opps_create():
        b = body()
        fid = int(b.get("funnel_id") or 0)
        if not access.can_funnel(fid):
            raise OpportunityError("You don't have access to that funnel.")
        for p in b.get("participants") or []:
            check_person(p.get("contact_id"))
        return jsonify(create(b.get("name", ""), fid, b.get("stage_id"), b.get("company"), b.get("notes", ""), b.get("participants"),
                              users.current_id())), 201

    @app.get("/api/crm/opportunities/<int:opp_id>")
    @guarded
    def opps_get(opp_id):
        return jsonify(visible(opp_id))

    @app.put("/api/crm/opportunities/<int:opp_id>")
    @guarded
    def opps_update(opp_id):
        visible(opp_id); b = body()
        return jsonify(update(opp_id, b.get("name"), b.get("notes"), b.get("company")))

    @app.post("/api/crm/opportunities/<int:opp_id>/move")
    @guarded
    def opps_move(opp_id):
        visible(opp_id)
        with _crm() as conn:
            st = _stage(conn, body().get("stage_id"))
        if not st or not access.can_funnel(st["funnel_id"]):
            raise OpportunityError("No such stage.")
        return jsonify(move(opp_id, st["stage_id"]))

    @app.post("/api/crm/opportunities/<int:opp_id>/delete")
    @guarded
    def opps_delete(opp_id):
        visible(opp_id); delete(opp_id)
        return jsonify({"ok": True})

    @app.post("/api/crm/opportunities/<int:opp_id>/participants")
    @guarded
    def opps_add(opp_id):
        visible(opp_id); b = body()
        check_person(b.get("contact_id")); check_person(b.get("introduced_by"))
        return jsonify(add_participant(opp_id, int(b.get("contact_id") or 0), b.get("role") or "", bool(b.get("primary")), b.get("introduced_by")))

    @app.put("/api/crm/opportunities/<int:opp_id>/participants/<int:person_id>")
    @guarded
    def opps_change(opp_id, person_id):
        visible(opp_id); b = body(); check_person(b.get("introduced_by"))
        return jsonify(update_participant(opp_id, person_id, b.get("role"), b.get("primary"), b.get("active"),
                                          b.get("introduced_by") if "introduced_by" in b else None))

    @app.post("/api/crm/opportunities/<int:opp_id>/participants/<int:person_id>/remove")
    @guarded
    def opps_remove(opp_id, person_id):
        visible(opp_id)
        return jsonify(remove_participant(opp_id, person_id))

    @app.post("/api/crm/opportunities/<int:opp_id>/clear-primary")
    @guarded
    def opps_clear_primary(opp_id):
        visible(opp_id)
        return jsonify(clear_primary(opp_id))

    @app.post("/api/crm/opportunities/<int:opp_id>/tasks")
    @guarded
    def opps_task(opp_id):
        visible(opp_id); b = body()
        return jsonify(add_task(opp_id, b.get("title", ""), str(b.get("date", "")), b.get("time"), users.current_id())), 201

    @app.get("/api/crm/people/<int:person_id>/opportunities")
    @guarded
    def opps_for_person(person_id):
        check_person(person_id)
        allowed = access.funnel_ids()
        return jsonify({"opportunities": [o for o in for_person(person_id) if allowed is None or o["funnel_id"] in allowed]})
