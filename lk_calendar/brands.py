"""Brands: one account running several businesses (e.g. LyteKeepr, Aspan, Nexy, Windowly) with shared contacts and connected accounts.

- The brands table (calendar database) holds each brand's key, name and colour. The key is what the calendar's "project" field stores.
- A funnel belongs to a brand; the people in it take that brand. A contact outside a branded funnel has their own brand ('' = none yet).
- Emails, calls and opportunities have no brand of their own: they use their contact's or funnel's.
- A contact's follow-ups and tasks are filed under the contact's brand; with no brand yet they keep the channel's old default.
- "google" is a calendar source, never a brand.
"""

import re
from datetime import datetime

from . import store

PALETTE = ("#7fa8b8", "#e98b74", "#21493f", "#c9a227", "#6c8ebf", "#a46fa8", "#5b9a68", "#c0645a")


def _crm():
    from .contacts import _crm as crm
    return crm()


def all_brands() -> list:
    with store._conn() as conn:
        return [dict(r) for r in conn.execute("SELECT key, name, color, position FROM brands ORDER BY position, created_at, key")]


def names() -> dict:
    return {b["key"]: b["name"] for b in all_brands()}


def resolve(text) -> str:
    """A brand key from a key or a name (any case). '' stays '' (no brand). Unknown → ValidationError listing the brands."""
    t = str(text or "").strip()
    if not t:
        return ""
    for b in all_brands():
        if t.lower() in (b["key"].lower(), b["name"].lower()):
            return b["key"]
    raise store.ValidationError(f"There is no brand “{t}”. Brands: {', '.join(b['name'] for b in all_brands()) or 'none yet'}.")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())[:30] or "brand"


def create(name: str, color: str = "") -> dict:
    name = str(name or "").strip()[:60]
    if not name:
        raise store.ValidationError("A brand needs a name.")
    existing = all_brands()
    if any(b["name"].lower() == name.lower() for b in existing):
        raise store.ValidationError(f"There is already a brand called “{name}”.")
    keys = {b["key"] for b in existing} | set(store.CALENDAR_SOURCES)
    key, n = _slug(name), 2
    while key in keys:
        key, n = f"{_slug(name)}{n}", n + 1
    color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", str(color or "")) else PALETTE[len(existing) % len(PALETTE)]
    with store._conn() as conn:
        conn.execute("INSERT INTO brands (key, name, color, position, created_at) VALUES (?, ?, ?, ?, ?)",
                     (key, name, color, len(existing), datetime.now().isoformat()))
    return {"key": key, "name": name, "color": color}


def update(key: str, name=None, color=None) -> dict:
    current = next((b for b in all_brands() if b["key"] == key), None)
    if not current:
        raise store.ValidationError("No such brand.")
    if name is not None:
        name = str(name).strip()[:60]
        if not name:
            raise store.ValidationError("A brand needs a name.")
        if any(b["name"].lower() == name.lower() and b["key"] != key for b in all_brands()):
            raise store.ValidationError(f"There is already a brand called “{name}”.")
        current["name"] = name
    if color is not None:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(color)):
            raise store.ValidationError("Colour must look like #21493f.")
        current["color"] = color
    with store._conn() as conn:
        conn.execute("UPDATE brands SET name = ?, color = ? WHERE key = ?", (current["name"], current["color"], key))
    return current


def usage(key: str) -> dict:
    with _crm() as c:
        funnels = c.execute("SELECT COUNT(*) FROM funnels WHERE brand = ?", (key,)).fetchone()[0]
        people = c.execute("SELECT COUNT(*) FROM people WHERE brand = ?", (key,)).fetchone()[0]
    with store._conn() as conn:
        items = conn.execute("SELECT COUNT(*) FROM events WHERE project = ?", (key,)).fetchone()[0]
        items += conn.execute("SELECT COUNT(*) FROM leads WHERE project = ?", (key,)).fetchone()[0]
    return {"funnels": funnels, "contacts": people, "calendar": items}


def delete(key: str) -> None:
    if key not in store.brand_keys():
        raise store.ValidationError("No such brand.")
    if len(store.brand_keys()) == 1:
        raise store.ValidationError("Keep at least one brand.")
    u = usage(key)
    if any(u.values()):
        raise store.ValidationError(f"This brand is still used by {u['funnels']} funnel(s), {u['contacts']} contact(s) and {u['calendar']} calendar item(s) "
                                    "or outreach records. Move them to another brand first.")
    with store._conn() as conn:
        conn.execute("DELETE FROM brands WHERE key = ?", (key,))
        conn.execute("DELETE FROM brand_access WHERE brand = ?", (key,))


# ------------------------------------------------------------------ contacts and funnels
def funnel_brand(funnel_id) -> str:
    if not funnel_id:
        return ""
    with _crm() as c:
        row = c.execute("SELECT brand FROM funnels WHERE funnel_id = ?", (int(funnel_id),)).fetchone()
    return (row["brand"] or "") if row else ""


def person_brand(person_id) -> str:
    with _crm() as c:
        row = c.execute("SELECT brand FROM people WHERE person_id = ?", (int(person_id),)).fetchone()
    return (row["brand"] or "") if row else ""


def set_person_brand(person_id: int, brand) -> str:
    """Give a contact a brand. A contact in a branded funnel has that funnel's brand: change the funnel's brand or move them out first."""
    key = resolve(brand)
    with _crm() as c:
        row = c.execute("""SELECT p.name, p.brand, f.name AS funnel_name, f.brand AS funnel_brand FROM people p
                           LEFT JOIN funnels f ON f.funnel_id = p.funnel_id WHERE p.person_id = ?""", (int(person_id),)).fetchone()
        if not row:
            raise store.ValidationError("No such contact.")
        if row["funnel_brand"] and key != row["funnel_brand"]:
            raise store.ValidationError(f"{row['name']} is in the funnel “{row['funnel_name']}”, which belongs to {names().get(row['funnel_brand'], row['funnel_brand'])}. "
                                        "Their brand comes from the funnel: move them to another funnel or change the funnel's brand.")
        c.execute("UPDATE people SET brand = ? WHERE person_id = ?", (key, int(person_id)))
    file_calendar([int(person_id)])
    return key


def set_funnel_brand(funnel_id: int, brand) -> str:
    """Give a funnel a brand; everyone in it takes that brand. Clearing it leaves the people's brands as they are."""
    key = resolve(brand)
    with _crm() as c:
        if not c.execute("SELECT 1 FROM funnels WHERE funnel_id = ?", (int(funnel_id),)).fetchone():
            raise store.ValidationError("No such funnel.")
        c.execute("UPDATE funnels SET brand = ? WHERE funnel_id = ?", (key, int(funnel_id)))
        ids = [r[0] for r in c.execute("SELECT person_id FROM people WHERE funnel_id = ?", (int(funnel_id),))]
        if key and ids:
            c.execute("UPDATE people SET brand = ? WHERE funnel_id = ?", (key, int(funnel_id)))
    if key:
        file_calendar(ids)
    return key


def joined_funnel(person_ids: list, funnel_id) -> None:
    """People were put into a funnel: they take its brand (if it has one)."""
    key = funnel_brand(funnel_id)
    if not key or not person_ids:
        return
    ids = [int(i) for i in person_ids]
    with _crm() as c:
        c.execute(f"UPDATE people SET brand = ? WHERE person_id IN ({','.join('?' for _ in ids)})", [key, *ids])
    file_calendar(ids)


def lead_project(person_id, channel: str) -> str:
    """Where a contact's outreach on a channel is filed: their brand, or (no brand yet) the channel's old default."""
    from . import leads
    key = person_brand(person_id) if person_id else ""
    return key or store.default_project(leads.CHANNELS.get(channel, {}).get("project", ""))


def file_calendar(person_ids: list) -> None:
    """Put each contact's outreach records and their tasks/follow-ups under the contact's brand (Google meetings stay as they are)."""
    from . import leads
    for pid in person_ids:
        key = person_brand(pid)
        with store._conn() as conn:
            for lead in conn.execute("SELECT id, channel, project FROM leads WHERE person_id = ?", (int(pid),)).fetchall():
                project = key or store.default_project(leads.CHANNELS.get(lead["channel"], {}).get("project", ""))
                conn.execute("UPDATE leads SET project = ? WHERE id = ?", (project, lead["id"]))
                conn.execute("UPDATE events SET project = ? WHERE lead_ref = ? AND source != 'google'", (project, f"lead:{lead['id']}"))


def file_lead(conn, lead: dict, person_id) -> None:
    """One outreach record was just linked or changed (inside the caller's transaction): file it and its tasks under the contact's brand."""
    key = person_brand(person_id) if person_id else ""
    if not key or lead.get("project") == key:
        return
    conn.execute("UPDATE leads SET project = ? WHERE id = ?", (key, lead["id"]))
    conn.execute("UPDATE events SET project = ? WHERE lead_ref = ? AND source != 'google'", (key, f"lead:{lead['id']}"))
    lead["project"] = key


def meeting_brands(events: list) -> None:
    """Google meetings keep their source; each also gets 'brand': the brand of the first invited contact that has one."""
    emails = {a.lower() for e in events if e.get("source") == "google" for a in _attendee_emails(e)}
    if not emails:
        return
    with _crm() as c:
        marks = ",".join("?" for _ in emails)
        found = {r["email"]: r["brand"] for r in c.execute(
            f"""SELECT lower(a.channel_identifier) AS email, p.brand FROM channel_accounts a JOIN people p ON p.person_id = a.person_id
                WHERE a.channel = 'email' AND lower(a.channel_identifier) IN ({marks}) AND p.brand != ''""", sorted(emails))}
    for e in events:
        if e.get("source") == "google":
            e["brand"] = next((found[a.lower()] for a in _attendee_emails(e) if a.lower() in found), "")


def _attendee_emails(e: dict) -> list:
    import json
    raw = e.get("attendees") or "[]"
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    out = []
    for a in items or []:
        addr = a.get("email") if isinstance(a, dict) else a
        if isinstance(addr, str) and "@" in addr:
            out.append(addr)
    return out


# ------------------------------------------------------------------ routes (reading is open to the team; changes are for the admin, see access.guard)
def register(app):
    from flask import jsonify, request

    def fail(exc):
        return jsonify({"error": str(exc)}), 400

    @app.get("/api/crm/brands")
    def brands_list():
        from . import access
        return jsonify({"brands": [b for b in all_brands() if access.brand_allowed(b["key"])],      # a member sees only their brands
                        "sources": [{"key": "google", "name": "Google", "color": "#7b6fa8"}]})

    @app.post("/api/crm/brands")
    def brands_create():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify({"brand": create(body.get("name", ""), body.get("color", "")), "brands": all_brands()}), 201
        except store.ValidationError as exc:
            return fail(exc)

    @app.put("/api/crm/brands/<key>")
    def brands_update(key):
        body = request.get_json(silent=True) or {}
        try:
            return jsonify({"brand": update(key, body.get("name"), body.get("color")), "brands": all_brands()})
        except store.ValidationError as exc:
            return fail(exc)

    @app.post("/api/crm/brands/<key>/delete")
    def brands_delete(key):
        try:
            delete(key)
            return jsonify({"ok": True, "brands": all_brands()})
        except store.ValidationError as exc:
            return fail(exc)

    @app.post("/api/crm/funnels/<int:funnel_id>/brand")
    def funnel_set_brand(funnel_id):
        try:
            return jsonify({"ok": True, "brand": set_funnel_brand(funnel_id, (request.get_json(silent=True) or {}).get("brand", ""))})
        except store.ValidationError as exc:
            return fail(exc)
