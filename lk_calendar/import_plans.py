"""A dropped spreadsheet waiting to be imported, and the plan Claude makes for it. USER checks the plan on CRM → Import (the same
Match fields page as a manual import, filled in) and clicks Import; nothing is created before that click.

- Every scanned file is kept here, not only in memory, so it survives a restart and waits while USER talks to Claude (14 days).
- The plan: which column is which field, the brand, and either contacts only or a funnel. The funnel is an existing one, or a new one:
  'cold' = cold outreach, three stages before the conversation, then the business's main funnel from its first stage on;
  'regular' = a copy of the main funnel's stages. Each person's stage: one default, or by the value of one column.
- The main funnel: the oldest sales funnel that isn't a cold outreach one (the one Claude set up for the business).
"""

import json
import secrets
import time
from datetime import datetime

from . import store

KEEP_SECONDS = 14 * 24 * 3600
COLD_START = [("Prospect found", "new"), ("First message sent", "contacted"), ("Follow-up sent", "")]
SCHEMA = """
CREATE TABLE IF NOT EXISTS import_uploads (
    token TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    headers TEXT NOT NULL,
    rows TEXT NOT NULL,
    created REAL NOT NULL,
    plan TEXT NOT NULL DEFAULT '',          -- JSON, saved by Claude (save)
    imported_at TEXT NOT NULL DEFAULT ''
);
"""


def _conn():
    conn = store._conn()
    conn.executescript(SCHEMA)
    return conn


def put(filename: str, headers: list, rows: list) -> str:
    token = secrets.token_urlsafe(12)
    with _conn() as conn:
        conn.execute("DELETE FROM import_uploads WHERE created < ?", (time.time() - KEEP_SECONDS,))
        conn.execute("INSERT INTO import_uploads (token, filename, headers, rows, created) VALUES (?, ?, ?, ?, ?)",
                     (token, filename, json.dumps(headers, ensure_ascii=False), json.dumps(rows, ensure_ascii=False), time.time()))
    return token


def _row(r) -> dict:
    return {"token": r["token"], "name": r["filename"], "headers": json.loads(r["headers"]), "rows": json.loads(r["rows"]),
            "plan": json.loads(r["plan"]) if r["plan"] else None, "imported": bool(r["imported_at"])}


def get(token: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM import_uploads WHERE token = ? AND created >= ?", (token or "", time.time() - KEEP_SECONDS)).fetchone()
    return _row(r) if r else None


def waiting() -> dict | None:
    """The most recently dropped file that isn't imported yet."""
    with _conn() as conn:
        r = conn.execute("SELECT * FROM import_uploads WHERE imported_at = '' AND created >= ? ORDER BY created DESC LIMIT 1",
                         (time.time() - KEEP_SECONDS,)).fetchone()
    return _row(r) if r else None


def any_planned_import() -> bool:
    with _conn() as conn:
        return bool(conn.execute("SELECT 1 FROM import_uploads WHERE imported_at != '' AND plan != '' LIMIT 1").fetchone())


def mark_imported(token: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE import_uploads SET imported_at = ? WHERE token = ?", (datetime.now().isoformat(timespec="seconds"), token))


# ----------------------------------------------------------------- funnels
def _crm():
    from crm import db as crm_db
    crm_db.init_db()
    return crm_db.get_connection()


def funnels() -> list:
    """The sales funnels with their stages; the main one and the cold outreach ones marked."""
    conn = _crm()
    try:
        rows = [dict(r) for r in conn.execute("SELECT funnel_id, name, brand, variant FROM funnels WHERE kind = 'sales' ORDER BY created_at, funnel_id")]
        for f in rows:
            f["stages"] = [r["name"] for r in conn.execute("SELECT name FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (f["funnel_id"],))]
    finally:
        conn.close()
    main = next((f for f in rows if f["variant"] != "cold"), None)
    for f in rows:
        f["main"] = f is main
        f["cold_outreach"] = f.pop("variant") == "cold"
    return rows


def main_funnel() -> dict | None:
    return next((f for f in funnels() if f["main"]), None)


def new_stages(kind: str) -> list:
    """The stages (name, mark) of a new funnel: a copy of the main funnel, or cold outreach in front of it."""
    main = main_funnel()
    if not main:
        raise store.ValidationError("There is no main funnel yet: set up the business's funnel first.")
    conn = _crm()
    try:
        stages = [(r["name"], r["mark"]) for r in conn.execute("SELECT name, mark FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (main["funnel_id"],))]
    finally:
        conn.close()
    if kind != "cold":
        return stages
    taken = {n.lower() for n, _ in COLD_START}
    return COLD_START + [(n, "conversation" if m == "new" else m) for n, m in stages if n.lower() not in taken]   # their reply: the inquiry


# ----------------------------------------------------------------- the plan
def _stage_for(row: dict, placement: dict) -> str:
    col = placement.get("column") or ""
    value = (row.get(col) or "").strip().lower() if col else ""
    by_value = {str(k).strip().lower(): v for k, v in (placement.get("values") or {}).items()}
    return by_value.get(value) or placement.get("stage") or ""


def check(up: dict, plan: dict) -> dict:
    """A plan made sound: known fields, a brand, and a funnel whose stages exist. Raises with what to fix."""
    from . import brands, contacts
    mapping = {h: f for h, f in (plan.get("mapping") or {}).items() if h in up["headers"] and (f in contacts.FIELD_KEYS or f in ("extra", "ignore"))}
    if not plan.get("brand"):
        raise store.ValidationError("Choose the brand these people are for (list_brands).")
    out = {"mapping": mapping, "brand": brands.resolve(plan["brand"]), "funnel": None}
    f = plan.get("funnel") or None
    if not f:
        return out                                    # contacts only
    if f.get("funnel_id"):
        have = next((x for x in funnels() if x["funnel_id"] == int(f["funnel_id"])), None)
        if not have:
            raise store.ValidationError("There is no sales funnel with that id: see list_funnels.")
        stages, name, new = have["stages"], have["name"], ""
    else:
        new = "cold" if f.get("new") == "cold" else "regular"
        stages = [n for n, _ in new_stages(new)]
        name = (f.get("name") or "").strip()[:80]
        if not name:
            raise store.ValidationError("Give the new funnel a name.")
    placement = {"stage": (f.get("stage") or stages[0]), "column": f.get("column") or "", "values": f.get("values") or {}}
    lower = {s.lower(): s for s in stages}
    if placement["column"] and placement["column"] not in up["headers"]:
        raise store.ValidationError(f"The file has no column {placement['column']!r}.")
    if placement["stage"].lower() not in lower:
        raise store.ValidationError("Stage not found. The stages are: " + ", ".join(stages))
    placement["stage"] = lower[placement["stage"].lower()]
    for value, stage in list(placement["values"].items()):
        if str(stage).lower() not in lower:
            raise store.ValidationError(f"Stage {stage!r} not found. The stages are: " + ", ".join(stages))
        placement["values"][value] = lower[str(stage).lower()]
    out["funnel"] = {"funnel_id": None if new else int(f["funnel_id"]), "new": new, "name": name, "stages": stages, **placement}
    return out


def split_counts(up: dict, funnel: dict | None) -> dict:
    """For the review: how many go to a stage by their column value, and how many take the default stage (USER may change it)."""
    if not funnel:
        return {"by_value": {}, "default": 0}
    by_value, default = {}, 0
    for row in up["rows"]:
        st = _stage_for(row, {**funnel, "stage": ""})
        if st:
            by_value[st] = by_value.get(st, 0) + 1
        else:
            default += 1
    return {"by_value": by_value, "default": default}


def counts(up: dict, funnel: dict | None) -> dict:
    """How many people go in each stage (in the funnel's order, empty stages left out)."""
    if not funnel:
        return {}
    n = {}
    for row in up["rows"]:
        st = _stage_for(row, funnel)
        n[st] = n.get(st, 0) + 1
    return {s: n[s] for s in funnel["stages"] if n.get(s)}


def save(token: str, plan: dict) -> dict:
    up = get(token) if token else waiting()
    if not up:
        raise store.ValidationError("No file is waiting: ask USER to drop the file on Home (Import a list) first.")
    if up["imported"]:
        raise store.ValidationError("That file is already imported.")
    sound = check(up, plan)
    with _conn() as conn:
        conn.execute("UPDATE import_uploads SET plan = ? WHERE token = ?", (json.dumps(sound, ensure_ascii=False), up["token"]))
    return {"token": up["token"], "file": up["name"], "people": len(up["rows"]), **sound, "counts": counts(up, sound["funnel"])}


def apply(token: str, person_rows: list, funnel: dict | None, brand: str) -> dict:
    """After the import: the people of each row into the funnel (made now if it's new), each in their stage. Someone already in a
    funnel stays where they are."""
    if not funnel:
        return {}
    from crm import db as crm_db
    from . import brands, contacts
    up = get(token)
    conn = _crm()
    try:
        fid = funnel.get("funnel_id")
        if not fid:
            main = main_funnel() or {}
            template = {"pattern": _pattern(conn, main.get("funnel_id")), "stages": [{"name": n, "mark": m} for n, m in new_stages(funnel["new"])]}
            fid = crm_db.create_funnel(conn, funnel["name"], template)
            if funnel["new"] == "cold":
                conn.execute("UPDATE funnels SET variant = 'cold' WHERE funnel_id = ?", (fid,))
        stage_ids = {r["name"]: r["stage_id"] for r in conn.execute("SELECT stage_id, name FROM funnel_stages WHERE funnel_id = ?", (fid,))}
        busy = {r["person_id"] for r in conn.execute("SELECT person_id FROM people WHERE funnel_id IS NOT NULL")}
        place = {}
        for i, pid in person_rows:
            if pid in busy or pid in place:
                continue
            place[pid] = _stage_for(up["rows"][i], funnel) if up else funnel["stage"]
        if place:
            crm_db.move_people_to_funnel(conn, list(place), fid)
            for pid, st in place.items():
                if st in stage_ids:
                    crm_db.move_person_to_stage(conn, pid, stage_ids[st])
        conn.commit()
    finally:
        conn.close()
    if brand and not funnel.get("funnel_id"):          # a new funnel takes the import's brand
        brands.set_funnel_brand(fid, brand)
    ids = list(place)
    if ids:
        contacts.ensure_leads(ids)
        brands.joined_funnel(ids, fid)
        for pid in ids:
            contacts.status_on_stage(pid)
    n = {}
    for st in place.values():
        n[st] = n.get(st, 0) + 1
    return {"funnel_id": fid, "funnel": funnel["name"], "placed": n}


def _pattern(conn, funnel_id) -> str:
    row = conn.execute("SELECT pattern FROM funnels WHERE funnel_id = ?", (funnel_id or 0,)).fetchone()
    return row["pattern"] if row else ""
