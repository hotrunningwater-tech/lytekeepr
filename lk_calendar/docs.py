"""Documents: folders, documents and tabs kept in the CRM database (not Google Drive), mainly for Claude to write and find things.

Brand → Funnel → Folders (nested) → Documents → Tabs
- Every folder and document belongs to a brand, and optionally to one of that brand's funnels (the funnel must have that brand).
- Documents sit at the top of a brand or funnel, or in a folder. A document has its own text and any number of named tabs.
- Three tables hold everything (doc_folders, docs, doc_tabs); moving or renaming never touches the content.
- Every overwrite first saves the old text in doc_revisions, and deleting only marks things deleted, so nothing is lost by mistake.
- Who may see what follows the brand and funnel access (see can_scope).
"""

import re
from contextlib import contextmanager

from . import store

MAX_TEXT = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_folders (
    folder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    funnel_id INTEGER,
    parent_id INTEGER,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS docs (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    funnel_id INTEGER,
    folder_id INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS doc_tabs (
    tab_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS doc_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    tab_id INTEGER,
    body TEXT NOT NULL,
    at TEXT NOT NULL,
    who TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_docs_scope ON docs(brand, funnel_id, folder_id);
CREATE INDEX IF NOT EXISTS idx_doc_folders_scope ON doc_folders(brand, funnel_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_doc_tabs_doc ON doc_tabs(doc_id);
"""


class DocError(ValueError):
    pass


@contextmanager
def _crm():
    from .contacts import _crm as crm
    with crm() as conn:
        conn.executescript(SCHEMA)
        yield conn


def _now() -> str:
    return store.now_local().isoformat(timespec="minutes")


def _who() -> str:
    try:
        from flask import g
        user = getattr(g, "user", None) or {}
        return user.get("name") or user.get("email") or ""
    except RuntimeError:
        return ""


# ------------------------------------------------------------------ where things live, and who may see them
def scope(brand=None, funnel_id=None) -> tuple:
    """(brand key, funnel_id or None) from a brand name/key and/or a funnel. A funnel brings its own brand, and must have one."""
    from . import brands
    key = brands.resolve(brand) if brand else ""
    fid = int(funnel_id) if funnel_id else None
    if fid:
        fb = brands.funnel_brand(fid)
        with _crm() as conn:
            row = conn.execute("SELECT name FROM funnels WHERE funnel_id = ?", (fid,)).fetchone()
        if not row:
            raise DocError("No such funnel.")
        if not fb:
            raise DocError(f"The funnel “{row['name']}” has no brand yet. Give it a brand first, then its documents can be created.")
        if key and key != fb:
            raise DocError(f"The funnel “{row['name']}” belongs to {brands.names().get(fb, fb)}, not {brands.names().get(key, key)}.")
        key = fb
    if not key:
        raise DocError("Say which brand (or funnel) this belongs to.")
    return key, fid


def can_scope(brand: str, funnel_id) -> bool:
    """Funnel documents: whoever may see that funnel (and its brand). Brand documents: the admin, "Everything", or a member given the whole brand."""
    from . import access
    if access.is_full():
        return True
    scopes = access.brand_scopes()
    if funnel_id:
        return access.can_funnel(funnel_id) and (scopes is None or brand in scopes)
    return bool(scopes) and scopes.get(brand) == "all"


def _need(brand, funnel_id):
    if not can_scope(brand, funnel_id):
        raise DocError("You don't have access to that.")


def _folder(conn, folder_id) -> dict:
    row = conn.execute("SELECT * FROM doc_folders WHERE folder_id = ? AND deleted_at = ''", (int(folder_id),)).fetchone()
    if not row:
        raise DocError("No such folder.")
    _need(row["brand"], row["funnel_id"])
    return dict(row)


def _doc(conn, doc_id) -> dict:
    row = conn.execute("SELECT * FROM docs WHERE doc_id = ? AND deleted_at = ''", (int(doc_id),)).fetchone()
    if not row:
        raise DocError("No such document.")
    _need(row["brand"], row["funnel_id"])
    return dict(row)


def _tab(conn, doc_id, tab=None) -> dict | None:
    """A tab by id or by name (any case); None = the document's own text."""
    if tab in (None, "", 0):
        return None
    if isinstance(tab, int) or str(tab).isdigit():
        row = conn.execute("SELECT * FROM doc_tabs WHERE tab_id = ? AND doc_id = ? AND deleted_at = ''", (int(tab), doc_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM doc_tabs WHERE doc_id = ? AND lower(name) = lower(?) AND deleted_at = ''", (doc_id, str(tab).strip())).fetchone()
    if not row:
        raise DocError(f"This document has no tab “{tab}”.")
    return dict(row)


def _path(conn, folder_id) -> list:
    names, seen = [], set()
    while folder_id and folder_id not in seen:
        seen.add(folder_id)
        row = conn.execute("SELECT name, parent_id FROM doc_folders WHERE folder_id = ?", (folder_id,)).fetchone()
        if not row:
            break
        names.insert(0, row["name"])
        folder_id = row["parent_id"]
    return names


def _where(conn, brand, funnel_id, folder_id) -> str:
    from . import brands
    parts = [brands.names().get(brand, brand)]
    if funnel_id:
        row = conn.execute("SELECT name FROM funnels WHERE funnel_id = ?", (funnel_id,)).fetchone()
        parts.append(row["name"] if row else f"funnel {funnel_id}")
    return " / ".join(parts + _path(conn, folder_id))


# ------------------------------------------------------------------ reading
def overview() -> list:
    """Every brand and funnel this person may see that has documents, with how many."""
    from . import brands
    names = brands.names()
    out = []
    with _crm() as conn:
        rows = conn.execute("""SELECT brand, funnel_id, COUNT(*) AS n, MAX(updated_at) AS last FROM docs WHERE deleted_at = '' GROUP BY brand, funnel_id""").fetchall()
        funnels = {r["funnel_id"]: r["name"] for r in conn.execute("SELECT funnel_id, name FROM funnels")}
    for r in rows:
        if can_scope(r["brand"], r["funnel_id"]):
            out.append({"brand": r["brand"], "brand_name": names.get(r["brand"], r["brand"]), "funnel_id": r["funnel_id"],
                        "funnel": funnels.get(r["funnel_id"], "") if r["funnel_id"] else "", "documents": r["n"], "last_updated": r["last"]})
    return sorted(out, key=lambda x: (x["brand_name"], x["funnel"]))


def tree(brand=None, funnel_id=None) -> dict:
    """Folders (nested), documents and their tab names in one brand (its own documents, not its funnels') or one funnel."""
    key, fid = scope(brand, funnel_id)
    _need(key, fid)
    with _crm() as conn:
        cond, args = ("funnel_id = ?", [fid]) if fid else ("funnel_id IS NULL AND brand = ?", [key])
        folders = [dict(r) for r in conn.execute(f"SELECT folder_id, parent_id, name, updated_at FROM doc_folders WHERE {cond} AND deleted_at = '' ORDER BY lower(name)", args)]
        docs = [dict(r) for r in conn.execute(f"""SELECT doc_id, folder_id, title, updated_at, length(body) AS chars FROM docs WHERE {cond} AND deleted_at = ''
                                                  ORDER BY lower(title)""", args)]
        tabs: dict = {}
        if docs:
            marks = ",".join("?" for _ in docs)
            for t in conn.execute(f"SELECT tab_id, doc_id, name, length(body) AS chars FROM doc_tabs WHERE doc_id IN ({marks}) AND deleted_at = '' ORDER BY position, tab_id",
                                  [d["doc_id"] for d in docs]):
                tabs.setdefault(t["doc_id"], []).append({"tab_id": t["tab_id"], "name": t["name"], "chars": t["chars"]})
        where = _where(conn, key, fid, None)
    for d in docs:
        d["tabs"] = tabs.get(d["doc_id"], [])
    def build(parent):
        return [{**f, "folders": build(f["folder_id"]), "documents": [d for d in docs if d["folder_id"] == f["folder_id"]]}
                for f in folders if f["parent_id"] == parent]
    known = {f["folder_id"] for f in folders}
    return {"brand": key, "funnel_id": fid, "where": where, "folders": build(None),
            "documents": [d for d in docs if not d["folder_id"] or d["folder_id"] not in known]}


def _section(text: str, section) -> str | None:
    """The part of the text under a heading: by its words or its number (1 = the first heading)."""
    lines = text.split("\n")
    heads = [(i, len(m.group(1)), m.group(2).strip()) for i, l in enumerate(lines) for m in [re.match(r"^(#{1,6})\s+(.*)$", l)] if m]
    pick = None
    if str(section).strip().isdigit():
        n = int(section)
        pick = heads[n - 1] if 0 < n <= len(heads) else None
    else:
        want = str(section).strip().lower()
        pick = next((h for h in heads if h[2].lower() == want), None) or next((h for h in heads if want in h[2].lower()), None)
    if not pick:
        return None
    end = next((i for i, lvl, _ in heads if i > pick[0] and lvl <= pick[1]), len(lines))
    return pick[0], end


def read(doc_id, tab=None, section=None) -> dict:
    with _crm() as conn:
        d = _doc(conn, doc_id)
        tabs = [dict(r) for r in conn.execute("SELECT tab_id, name, body, updated_at FROM doc_tabs WHERE doc_id = ? AND deleted_at = '' ORDER BY position, tab_id", (d["doc_id"],))]
        where = _where(conn, d["brand"], d["funnel_id"], d["folder_id"])
        t = _tab(conn, d["doc_id"], tab)
    out = {"doc_id": d["doc_id"], "title": d["title"], "where": where, "brand": d["brand"], "funnel_id": d["funnel_id"], "folder_id": d["folder_id"],
           "updated_at": d["updated_at"], "updated_by": d["updated_by"]}
    if t or section:
        text = t["body"] if t else d["body"]
        if section:
            span = _section(text, section)
            if span is None:
                raise DocError(f"No section “{section}” (sections are the # headings).")
            text = "\n".join(text.split("\n")[span[0]:span[1]])
        out.update({"tab": t["name"] if t else None, "tab_id": t["tab_id"] if t else None, "content": text})
        return out
    out.update({"content": d["body"], "tabs": tabs})
    return out


def search(q: str, brand=None, funnel_id=None, in_content: bool = True, limit: int = 20) -> list:
    """Names first (folders, documents, tabs), then the text; only where this person may look."""
    words = [w for w in (q or "").lower().split() if w]
    if not words:
        raise DocError("What should I look for?")
    key, fid = (scope(brand, funnel_id) if (brand or funnel_id) else ("", None))
    hits = []
    with _crm() as conn:
        def allowed(r):
            if fid and r["funnel_id"] != fid:
                return False
            if key and not fid and r["brand"] != key:
                return False
            return can_scope(r["brand"], r["funnel_id"])
        def match(text):
            t = (text or "").lower()
            return all(w in t for w in words)
        def snippet(text):
            t = text or ""
            i = t.lower().find(words[0])
            return ("…" if i > 80 else "") + t[max(0, i - 80): i + 220].replace("\n", " ") + ("…" if i + 220 < len(t) else "")
        for f in conn.execute("SELECT * FROM doc_folders WHERE deleted_at = ''"):
            if allowed(f) and match(f["name"]):
                hits.append({"kind": "folder", "folder_id": f["folder_id"], "name": f["name"], "where": _where(conn, f["brand"], f["funnel_id"], f["parent_id"]), "rank": 0})
        for d in conn.execute("SELECT * FROM docs WHERE deleted_at = ''"):
            if not allowed(d):
                continue
            where = _where(conn, d["brand"], d["funnel_id"], d["folder_id"])
            if match(d["title"]) or match(where + " " + d["title"]):
                hits.append({"kind": "document", "doc_id": d["doc_id"], "title": d["title"], "where": where, "rank": 1})
            elif in_content and match(d["body"]):
                hits.append({"kind": "text", "doc_id": d["doc_id"], "title": d["title"], "where": where, "snippet": snippet(d["body"]), "rank": 3})
            for t in conn.execute("SELECT * FROM doc_tabs WHERE doc_id = ? AND deleted_at = ''", (d["doc_id"],)):
                if match(t["name"]):
                    hits.append({"kind": "tab", "doc_id": d["doc_id"], "tab_id": t["tab_id"], "title": d["title"], "tab": t["name"], "where": where, "rank": 2})
                elif in_content and match(t["body"]):
                    hits.append({"kind": "text", "doc_id": d["doc_id"], "tab_id": t["tab_id"], "title": d["title"], "tab": t["name"], "where": where,
                                 "snippet": snippet(t["body"]), "rank": 3})
    hits.sort(key=lambda h: h["rank"])
    for h in hits:
        h.pop("rank")
    return hits[:limit]


# ------------------------------------------------------------------ folders
def create_folder(name: str, brand=None, funnel_id=None, parent_id=None) -> dict:
    name = (name or "").strip()[:200]
    if not name:
        raise DocError("A folder needs a name.")
    with _crm() as conn:
        if parent_id:
            p = _folder(conn, parent_id)
            key, fid = p["brand"], p["funnel_id"]
        else:
            key, fid = scope(brand, funnel_id)
            _need(key, fid)
        cur = conn.execute("INSERT INTO doc_folders (brand, funnel_id, parent_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (key, fid, int(parent_id) if parent_id else None, name, _now(), _now()))
        return {"folder_id": cur.lastrowid, "name": name, "where": _where(conn, key, fid, int(parent_id) if parent_id else None)}


def _descendants(conn, folder_id) -> list:
    out, todo = [], [folder_id]
    while todo:
        cur = todo.pop()
        kids = [r["folder_id"] for r in conn.execute("SELECT folder_id FROM doc_folders WHERE parent_id = ? AND deleted_at = ''", (cur,))]
        out += kids
        todo += kids
    return out


def update_folder(folder_id, name=None, parent_id=None, brand=None, funnel_id=None, to_top: bool = False) -> dict:
    """Rename, or move into another folder (parent_id) or to the top of a brand or funnel (to_top). Everything inside moves along."""
    with _crm() as conn:
        f = _folder(conn, folder_id)
        if name is not None and name.strip():
            conn.execute("UPDATE doc_folders SET name = ?, updated_at = ? WHERE folder_id = ?", (name.strip()[:200], _now(), f["folder_id"]))
        moving = parent_id or to_top or brand or funnel_id
        if moving:
            inner = _descendants(conn, f["folder_id"])
            if parent_id:
                p = _folder(conn, parent_id)
                if int(parent_id) == f["folder_id"] or int(parent_id) in inner:
                    raise DocError("A folder can't go inside itself.")
                key, fid, parent = p["brand"], p["funnel_id"], int(parent_id)
            else:
                key, fid = scope(brand, funnel_id) if (brand or funnel_id) else (f["brand"], f["funnel_id"])     # to_top alone: the top of where it is
                _need(key, fid)
                parent = None
            ids = [f["folder_id"]] + inner
            marks = ",".join("?" for _ in ids)
            conn.execute("UPDATE doc_folders SET parent_id = ?, updated_at = ? WHERE folder_id = ?", (parent, _now(), f["folder_id"]))
            conn.execute(f"UPDATE doc_folders SET brand = ?, funnel_id = ? WHERE folder_id IN ({marks})", [key, fid, *ids])
            conn.execute(f"UPDATE docs SET brand = ?, funnel_id = ? WHERE folder_id IN ({marks})", [key, fid, *ids])
        row = conn.execute("SELECT * FROM doc_folders WHERE folder_id = ?", (f["folder_id"],)).fetchone()
        return {"folder_id": row["folder_id"], "name": row["name"], "where": _where(conn, row["brand"], row["funnel_id"], row["parent_id"])}


def delete_folder(folder_id, with_contents: bool = False) -> dict:
    """An empty folder goes at once; one with things in it only with with_contents (everything inside goes too). Kept in the database, marked deleted."""
    with _crm() as conn:
        f = _folder(conn, folder_id)
        ids = [f["folder_id"]] + _descendants(conn, f["folder_id"])
        marks = ",".join("?" for _ in ids)
        n_docs = conn.execute(f"SELECT COUNT(*) FROM docs WHERE folder_id IN ({marks}) AND deleted_at = ''", ids).fetchone()[0]
        if (n_docs or len(ids) > 1) and not with_contents:
            raise DocError(f"“{f['name']}” has {len(ids) - 1} folder(s) and {n_docs} document(s) inside. Confirm deleting it with everything in it.")
        now = _now()
        conn.execute(f"UPDATE doc_folders SET deleted_at = ? WHERE folder_id IN ({marks})", [now, *ids])
        conn.execute(f"UPDATE docs SET deleted_at = ? WHERE folder_id IN ({marks}) AND deleted_at = ''", [now, *ids])
        return {"deleted_folders": len(ids), "deleted_documents": n_docs}


# ------------------------------------------------------------------ documents and tabs
def create_doc(title: str, brand=None, funnel_id=None, folder_id=None, content: str = "", tabs: list | None = None) -> dict:
    title = (title or "").strip()[:300]
    if not title:
        raise DocError("A document needs a title.")
    with _crm() as conn:
        if folder_id:
            f = _folder(conn, folder_id)
            key, fid = f["brand"], f["funnel_id"]
        else:
            key, fid = scope(brand, funnel_id)
            _need(key, fid)
        cur = conn.execute("INSERT INTO docs (brand, funnel_id, folder_id, title, body, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (key, fid, int(folder_id) if folder_id else None, title, (content or "")[:MAX_TEXT], _now(), _now(), _who()))
        doc_id = cur.lastrowid
        for i, t in enumerate(tabs or []):
            if (t.get("name") or "").strip():
                conn.execute("INSERT INTO doc_tabs (doc_id, name, body, position, updated_at) VALUES (?, ?, ?, ?, ?)",
                             (doc_id, t["name"].strip()[:200], (t.get("content") or "")[:MAX_TEXT], i, _now()))
        return {"doc_id": doc_id, "title": title, "where": _where(conn, key, fid, int(folder_id) if folder_id else None)}


def update_doc(doc_id, title=None, folder_id=None, brand=None, funnel_id=None, to_top: bool = False) -> dict:
    """Rename, or move into a folder, or to the top of a brand or funnel. The text and tabs stay as they are."""
    with _crm() as conn:
        d = _doc(conn, doc_id)
        if title is not None and title.strip():
            conn.execute("UPDATE docs SET title = ?, updated_at = ? WHERE doc_id = ?", (title.strip()[:300], _now(), d["doc_id"]))
        if folder_id:
            f = _folder(conn, folder_id)
            conn.execute("UPDATE docs SET folder_id = ?, brand = ?, funnel_id = ?, updated_at = ? WHERE doc_id = ?", (f["folder_id"], f["brand"], f["funnel_id"], _now(), d["doc_id"]))
        elif to_top or brand or funnel_id:
            key, fid = scope(brand, funnel_id) if (brand or funnel_id) else (d["brand"], d["funnel_id"])
            _need(key, fid)
            conn.execute("UPDATE docs SET folder_id = NULL, brand = ?, funnel_id = ?, updated_at = ? WHERE doc_id = ?", (key, fid, _now(), d["doc_id"]))
        row = conn.execute("SELECT * FROM docs WHERE doc_id = ?", (d["doc_id"],)).fetchone()
        return {"doc_id": row["doc_id"], "title": row["title"], "where": _where(conn, row["brand"], row["funnel_id"], row["folder_id"])}


def delete_doc(doc_id) -> dict:
    with _crm() as conn:
        d = _doc(conn, doc_id)
        conn.execute("UPDATE docs SET deleted_at = ? WHERE doc_id = ?", (_now(), d["doc_id"]))
        return {"deleted": d["title"]}


def write(doc_id, text: str, mode: str = "append", tab=None, section=None, find: str | None = None) -> dict:
    """Change the text of a document (or one tab) without touching anything else:
    append (add at the end), replace (the whole text), replace_section (one # heading's part), find_replace (one exact passage)."""
    text = text or ""
    with _crm() as conn:
        d = _doc(conn, doc_id)
        t = _tab(conn, d["doc_id"], tab)
        old = t["body"] if t else d["body"]
        if mode == "append":
            new = (old.rstrip("\n") + "\n\n" + text.strip("\n")) if old.strip() else text
        elif mode == "replace":
            new = text
        elif mode == "replace_section":
            span = _section(old, section) if section not in (None, "") else None
            if span is None:
                raise DocError(f"No section “{section}” (sections are the # headings).")
            lines = old.split("\n")
            body = text.strip("\n")
            if not re.match(r"^#{1,6}\s", body):              # keep the heading unless new text brings its own
                body = lines[span[0]] + "\n" + body
            new = "\n".join(lines[:span[0]] + body.split("\n") + ([""] if span[1] < len(lines) else []) + lines[span[1]:])
        elif mode == "find_replace":
            if not find:
                raise DocError("Say which exact text to replace.")
            n = old.count(find)
            if n != 1:
                raise DocError(f"That exact text appears {n} times; it must appear exactly once." if n else "That exact text isn't there.")
            new = old.replace(find, text, 1)
        else:
            raise DocError("mode must be append, replace, replace_section or find_replace.")
        if len(new) > MAX_TEXT:
            raise DocError("That would make the text too long; split it into another tab or document.")
        if old.strip() and mode != "append":
            conn.execute("INSERT INTO doc_revisions (doc_id, tab_id, body, at, who) VALUES (?, ?, ?, ?, ?)", (d["doc_id"], t["tab_id"] if t else None, old, _now(), _who()))
        if t:
            conn.execute("UPDATE doc_tabs SET body = ?, updated_at = ? WHERE tab_id = ?", (new, _now(), t["tab_id"]))
        else:
            conn.execute("UPDATE docs SET body = ? WHERE doc_id = ?", (new, d["doc_id"]))
        conn.execute("UPDATE docs SET updated_at = ?, updated_by = ? WHERE doc_id = ?", (_now(), _who(), d["doc_id"]))
        return {"doc_id": d["doc_id"], "tab": t["name"] if t else None, "chars": len(new), "saved_previous_version": bool(old.strip() and mode != "append")}


def create_tab(doc_id, name: str, content: str = "", position=None) -> dict:
    name = (name or "").strip()[:200]
    if not name:
        raise DocError("A tab needs a name.")
    with _crm() as conn:
        d = _doc(conn, doc_id)
        if conn.execute("SELECT 1 FROM doc_tabs WHERE doc_id = ? AND lower(name) = lower(?) AND deleted_at = ''", (d["doc_id"], name)).fetchone():
            raise DocError(f"This document already has a tab “{name}”.")
        tabs = [r["tab_id"] for r in conn.execute("SELECT tab_id FROM doc_tabs WHERE doc_id = ? AND deleted_at = '' ORDER BY position, tab_id", (d["doc_id"],))]
        cur = conn.execute("INSERT INTO doc_tabs (doc_id, name, body, position, updated_at) VALUES (?, ?, ?, ?, ?)", (d["doc_id"], name, (content or "")[:MAX_TEXT], len(tabs), _now()))
        if position is not None:
            _reorder(conn, d["doc_id"], cur.lastrowid, int(position))
        conn.execute("UPDATE docs SET updated_at = ?, updated_by = ? WHERE doc_id = ?", (_now(), _who(), d["doc_id"]))
        return {"doc_id": d["doc_id"], "tab_id": cur.lastrowid, "name": name}


def _reorder(conn, doc_id, tab_id, position: int) -> None:
    """position: 1 = first."""
    ids = [r["tab_id"] for r in conn.execute("SELECT tab_id FROM doc_tabs WHERE doc_id = ? AND deleted_at = '' ORDER BY position, tab_id", (doc_id,))]
    ids.remove(tab_id)
    ids.insert(max(0, min(len(ids), position - 1)), tab_id)
    for i, t in enumerate(ids):
        conn.execute("UPDATE doc_tabs SET position = ? WHERE tab_id = ?", (i, t))


def update_tab(doc_id, tab, name=None, position=None) -> dict:
    with _crm() as conn:
        d = _doc(conn, doc_id)
        t = _tab(conn, d["doc_id"], tab)
        if t is None:
            raise DocError("Which tab?")
        if name is not None and name.strip():
            conn.execute("UPDATE doc_tabs SET name = ?, updated_at = ? WHERE tab_id = ?", (name.strip()[:200], _now(), t["tab_id"]))
        if position is not None:
            _reorder(conn, d["doc_id"], t["tab_id"], int(position))
        return {"doc_id": d["doc_id"], "tabs": [r["name"] for r in conn.execute("SELECT name FROM doc_tabs WHERE doc_id = ? AND deleted_at = '' ORDER BY position, tab_id", (d["doc_id"],))]}


def delete_tab(doc_id, tab) -> dict:
    with _crm() as conn:
        d = _doc(conn, doc_id)
        t = _tab(conn, d["doc_id"], tab)
        if t is None:
            raise DocError("Which tab?")
        conn.execute("UPDATE doc_tabs SET deleted_at = ? WHERE tab_id = ?", (_now(), t["tab_id"]))
        return {"deleted_tab": t["name"]}


def count_for_funnel(funnel_id) -> int:
    with _crm() as conn:
        return conn.execute("SELECT COUNT(*) FROM docs WHERE funnel_id = ? AND deleted_at = ''", (int(funnel_id),)).fetchone()[0] + \
            conn.execute("SELECT COUNT(*) FROM doc_folders WHERE funnel_id = ? AND deleted_at = ''", (int(funnel_id),)).fetchone()[0]


def funnel_brand_changed(funnel_id, key: str) -> None:
    """A funnel got another brand: its folders and documents follow."""
    with _crm() as conn:
        conn.execute("UPDATE docs SET brand = ? WHERE funnel_id = ?", (key, int(funnel_id)))
        conn.execute("UPDATE doc_folders SET brand = ? WHERE funnel_id = ?", (key, int(funnel_id)))


# ------------------------------------------------------------------ routes (the app and Claude use the same ones)
def register(app):
    from functools import wraps
    from flask import jsonify, request

    def guarded(fn):
        @wraps(fn)
        def inner(*a, **k):
            try:
                return jsonify(fn(*a, **k))
            except (DocError, store.ValidationError) as exc:
                return jsonify({"error": str(exc)}), 400
        return inner

    def b():
        return request.get_json(silent=True) or {}

    def arg(name):
        return request.args.get(name) or None

    @app.get("/api/crm/docs")
    @guarded
    def docs_overview():
        return {"places": overview()}

    @app.get("/api/crm/docs/tree")
    @guarded
    def docs_tree():
        return tree(arg("brand"), arg("funnel_id"))

    @app.get("/api/crm/docs/search")
    @guarded
    def docs_search():
        return {"results": search(request.args.get("q", ""), arg("brand"), arg("funnel_id"), request.args.get("content", "1") != "0")}

    @app.get("/api/crm/docs/<int:doc_id>")
    @guarded
    def docs_read(doc_id):
        return read(doc_id, arg("tab"), arg("section"))

    @app.post("/api/crm/docs")
    @guarded
    def docs_create():
        x = b()
        return create_doc(x.get("title", ""), x.get("brand"), x.get("funnel_id"), x.get("folder_id"), x.get("content", ""), x.get("tabs"))

    @app.put("/api/crm/docs/<int:doc_id>")
    @guarded
    def docs_update(doc_id):
        x = b()
        return update_doc(doc_id, x.get("title"), x.get("folder_id"), x.get("brand"), x.get("funnel_id"), bool(x.get("to_top")))

    @app.post("/api/crm/docs/<int:doc_id>/delete")
    @guarded
    def docs_delete(doc_id):
        return delete_doc(doc_id)

    @app.post("/api/crm/docs/<int:doc_id>/write")
    @guarded
    def docs_write(doc_id):
        x = b()
        return write(doc_id, x.get("text", ""), x.get("mode") or "append", x.get("tab"), x.get("section"), x.get("find"))

    @app.post("/api/crm/docs/<int:doc_id>/tabs")
    @guarded
    def docs_tab_create(doc_id):
        x = b()
        return create_tab(doc_id, x.get("name", ""), x.get("content", ""), x.get("position"))

    @app.put("/api/crm/docs/<int:doc_id>/tabs/<int:tab_id>")
    @guarded
    def docs_tab_update(doc_id, tab_id):
        x = b()
        return update_tab(doc_id, tab_id, x.get("name"), x.get("position"))

    @app.post("/api/crm/docs/<int:doc_id>/tabs/<int:tab_id>/delete")
    @guarded
    def docs_tab_delete(doc_id, tab_id):
        return delete_tab(doc_id, tab_id)

    @app.post("/api/crm/docs/folders")
    @guarded
    def docs_folder_create():
        x = b()
        return create_folder(x.get("name", ""), x.get("brand"), x.get("funnel_id"), x.get("parent_id"))

    @app.put("/api/crm/docs/folders/<int:folder_id>")
    @guarded
    def docs_folder_update(folder_id):
        x = b()
        return update_folder(folder_id, x.get("name"), x.get("parent_id"), x.get("brand"), x.get("funnel_id"), bool(x.get("to_top")))

    @app.post("/api/crm/docs/folders/<int:folder_id>/delete")
    @guarded
    def docs_folder_delete(folder_id):
        return delete_folder(folder_id, bool(b().get("with_contents")))
