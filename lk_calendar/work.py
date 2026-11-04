"""Work pipelines (a test feature, off unless the owner switches it on for a workspace in the Owner console).

A work pipeline is a funnel of kind 'work': it holds ITEMS, never contacts. Each item is a row in the opportunities table (stage,
notes, history, tasks) with no people on it, named by the pipeline's own word ("Idea", "Order"; default "Item"). Inside an item are
SUB-ITEMS (name, done, note), stored only here. Documents stay under the funnel as for any funnel.
Kept apart from sales: lk_calendar/opportunities.py refuses to mix the two (sales routes never return a work item, nothing moves
between kinds, no contacts on an item, search skips work items), and crm/db.py never puts a contact in a work funnel.
"""

from datetime import datetime

from . import opportunities

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_subitems (
    subitem_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_subitems_item ON work_subitems(opportunity_id, position);
"""


class WorkError(ValueError):
    pass


def _crm():
    from . import contacts
    return contacts._crm()


def enabled() -> bool:
    from profiles.store import get_active_profile_id
    from . import directory
    return directory.work_enabled(get_active_profile_id())


def item_word(name: str) -> str:
    return (name or "").strip() or "Item"


def subitems(opp_id: int) -> list:
    with _crm() as conn:
        conn.executescript(SCHEMA)
        return [{"subitem_id": r["subitem_id"], "name": r["name"], "note": r["note"], "done": bool(r["done"])}
                for r in conn.execute("SELECT * FROM work_subitems WHERE opportunity_id = ? ORDER BY position, subitem_id", (opp_id,))]


def counts(opp_ids: list) -> dict:
    if not opp_ids:
        return {}
    with _crm() as conn:
        conn.executescript(SCHEMA)
        rows = conn.execute(f"""SELECT opportunity_id, COUNT(*) AS total, SUM(done) AS done FROM work_subitems
                                WHERE opportunity_id IN ({','.join('?' for _ in opp_ids)}) GROUP BY opportunity_id""", opp_ids).fetchall()
    return {r["opportunity_id"]: {"total": r["total"], "done": r["done"] or 0} for r in rows}


def _item(opp_id: int) -> dict:
    o = opportunities.get(opp_id)
    if not o or not o["work"]:
        raise WorkError("No such item.")
    return o


def _subitem(conn, subitem_id: int):
    row = conn.execute("SELECT * FROM work_subitems WHERE subitem_id = ?", (subitem_id,)).fetchone()
    if not row:
        raise WorkError("No such sub-item.")
    return row


def items(funnel_id: int) -> dict:
    """A work pipeline with its stages and every item (stage, notes, sub-items), for Claude."""
    with _crm() as conn:
        f = conn.execute("SELECT * FROM funnels WHERE funnel_id = ?", (funnel_id,)).fetchone()
        if not f or f["kind"] != "work":
            raise WorkError("That's not a work pipeline.")
        stages = {r["stage_id"]: r["name"] for r in conn.execute("SELECT stage_id, name FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (funnel_id,))}
        rows = conn.execute("SELECT opportunity_id, name, stage_id, notes FROM opportunities WHERE funnel_id = ? ORDER BY name COLLATE NOCASE", (funnel_id,)).fetchall()
    return {"pipeline": f["name"], "item_word": item_word(f["item_name"]), "stages": list(stages.values()),
            "items": [{"item_id": r["opportunity_id"], "name": r["name"], "stage": stages.get(r["stage_id"], ""), "notes": r["notes"],
                       "subitems": subitems(r["opportunity_id"])} for r in rows]}


def create_item(funnel_id: int, name: str, stage_id=None, notes: str = "", owner_id=None) -> dict:
    return opportunities.create(name, int(funnel_id), stage_id, None, notes or "", None, owner_id, work=True)


def update_item(opp_id: int, name=None, notes=None) -> dict:
    _item(opp_id)
    return opportunities.update(opp_id, name, notes, None)


def move_item(opp_id: int, stage_id: int) -> dict:
    _item(opp_id)
    return opportunities.move(opp_id, int(stage_id))


def delete_item(opp_id: int) -> None:
    _item(opp_id)
    opportunities.delete(opp_id)


def add_subitem(opp_id: int, name: str, note: str = "") -> dict:
    _item(opp_id)
    name = (name or "").strip()
    if not name:
        raise WorkError("Give the sub-item a name.")
    with _crm() as conn:
        conn.executescript(SCHEMA)
        pos = (conn.execute("SELECT MAX(position) AS m FROM work_subitems WHERE opportunity_id = ?", (opp_id,)).fetchone()["m"] or 0) + 1
        conn.execute("INSERT INTO work_subitems (opportunity_id, name, note, position, created_at) VALUES (?, ?, ?, ?, ?)",
                     (opp_id, name[:200], (note or "")[:2000], pos, datetime.now().isoformat(timespec="seconds")))
        opportunities._log(conn, opp_id, f"Added “{name[:200]}”")
    return _item(opp_id)


def update_subitem(subitem_id: int, name=None, note=None, done=None) -> dict:
    with _crm() as conn:
        conn.executescript(SCHEMA)
        row = _subitem(conn, subitem_id)
        sets = {}
        if name is not None and str(name).strip():
            sets["name"] = str(name).strip()[:200]
        if note is not None:
            sets["note"] = str(note)[:2000]
        if done is not None:
            sets["done"] = 1 if done else 0
        if sets:
            conn.execute(f"UPDATE work_subitems SET {', '.join(k + ' = :' + k for k in sets)} WHERE subitem_id = :id", {**sets, "id": subitem_id})
            if "done" in sets and sets["done"] != row["done"]:
                opportunities._log(conn, row["opportunity_id"], f"{'Done' if sets['done'] else 'Not done'}: “{sets.get('name', row['name'])}”")
    return _item(row["opportunity_id"])


def remove_subitem(subitem_id: int) -> dict:
    with _crm() as conn:
        conn.executescript(SCHEMA)
        row = _subitem(conn, subitem_id)
        conn.execute("DELETE FROM work_subitems WHERE subitem_id = ?", (subitem_id,))
        opportunities._log(conn, row["opportunity_id"], f"Removed “{row['name']}”")
    return _item(row["opportunity_id"])


def register(app):
    from functools import wraps
    from flask import jsonify, request
    from . import access, users

    def guarded(fn):
        @wraps(fn)
        def inner(*a, **k):
            if not enabled():
                return jsonify({"error": "Work pipelines are not switched on for this workspace."}), 403
            try:
                return fn(*a, **k)
            except (WorkError, opportunities.OpportunityError) as exc:
                return jsonify({"error": str(exc)}), 400
        return inner

    def body():
        return request.get_json(silent=True) or {}

    def visible(opp_id):
        o = _item(opp_id)
        if not access.can_opportunity(o):
            raise WorkError("No such item.")
        return o

    def visible_sub(subitem_id):
        with _crm() as conn:
            conn.executescript(SCHEMA)
            visible(_subitem(conn, subitem_id)["opportunity_id"])

    @app.get("/api/crm/work/funnels/<int:funnel_id>/items")
    @guarded
    def work_items(funnel_id):
        if not access.can_funnel(funnel_id):
            raise WorkError("That's not a work pipeline.")
        return jsonify(items(funnel_id))

    @app.post("/api/crm/work/items")
    @guarded
    def work_create():
        b = body()
        fid = int(b.get("funnel_id") or 0)
        if not access.can_funnel(fid):
            raise WorkError("That's not a work pipeline.")
        return jsonify(create_item(fid, b.get("name", ""), b.get("stage_id"), b.get("notes", ""), users.current_id())), 201

    @app.get("/api/crm/work/items/<int:opp_id>")
    @guarded
    def work_get(opp_id):
        return jsonify(visible(opp_id))

    @app.put("/api/crm/work/items/<int:opp_id>")
    @guarded
    def work_update(opp_id):
        visible(opp_id); b = body()
        return jsonify(update_item(opp_id, b.get("name"), b.get("notes")))

    @app.post("/api/crm/work/items/<int:opp_id>/move")
    @guarded
    def work_move(opp_id):
        visible(opp_id)
        return jsonify(move_item(opp_id, int(body().get("stage_id") or 0)))

    @app.post("/api/crm/work/items/<int:opp_id>/delete")
    @guarded
    def work_delete(opp_id):
        visible(opp_id); delete_item(opp_id)
        return jsonify({"ok": True})

    @app.post("/api/crm/work/items/<int:opp_id>/subitems")
    @guarded
    def work_add_sub(opp_id):
        visible(opp_id); b = body()
        return jsonify(add_subitem(opp_id, b.get("name", ""), b.get("note", "")))

    @app.put("/api/crm/work/subitems/<int:subitem_id>")
    @guarded
    def work_update_sub(subitem_id):
        visible_sub(subitem_id); b = body()
        return jsonify(update_subitem(subitem_id, b.get("name"), b.get("note"), b.get("done")))

    @app.post("/api/crm/work/subitems/<int:subitem_id>/delete")
    @guarded
    def work_remove_sub(subitem_id):
        visible_sub(subitem_id)
        return jsonify(remove_subitem(subitem_id))
