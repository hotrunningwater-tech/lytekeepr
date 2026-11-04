"""One company at a glance (#9, #10): its record (name, website, notes), its people, its opportunities (the main company on them, or
anyone from it taking part), and the recent history of everyone there merged (an email or call shared by several appears once).
Only what the person may see: their contacts, their funnels' opportunities. Read by the CRM's company card and by Claude (get_company)."""

from . import access

HISTORY_ITEMS = 30


def overview(company_id: int) -> dict | None:
    from . import contacts, interactions, opportunities
    c = contacts.get_company(int(company_id))
    if not c:
        return None
    visible = access.person_ids()
    people = [p for p in c.get("contacts") or [] if visible is None or p.get("id") in visible]
    ids = [p["id"] for p in people]
    with opportunities._crm() as conn:                 # its own connection makes sure the opportunity tables exist
        opp_ids = {r[0] for r in conn.execute("SELECT opportunity_id FROM opportunities WHERE company_id = ?", (int(company_id),))}
        if ids:
            opp_ids |= {r[0] for r in conn.execute(f"SELECT DISTINCT opportunity_id FROM opportunity_people WHERE person_id IN ({','.join('?' for _ in ids)})", ids)}
    opps = []
    for oid in sorted(opp_ids):
        o = opportunities.get(oid)
        if o and access.can_opportunity(o) and not o["work"]:       # a company page lists sales opportunities only
            opps.append({"opportunity_id": o["opportunity_id"], "name": o["name"], "funnel": o.get("funnel") or "", "stage": o.get("stage") or "",
                         "people": [p["name"] for p in o.get("participants") or [] if p.get("active", True)]})
    merged: dict = {}
    for p in people:
        for m in interactions.person_history(p["id"]):
            key = m.get("message_id") or f"{m.get('channel')}:{m.get('timestamp')}:{(m.get('message_text') or '')[:80]}"
            entry = merged.setdefault(key, {**m, "contacts": []})
            if p.get("display") not in entry["contacts"]:
                entry["contacts"].append(p.get("display") or p.get("name") or "")
    history = interactions._sorted(list(merged.values()))[-HISTORY_ITEMS:]
    return {"company_id": c["id"], "name": c["name"], "website": c.get("website") or "", "notes": c.get("notes") or "",
            "people": [{"contact_id": p["id"], "name": p.get("display") or p.get("name"), "title": p.get("job_title") or "", "email": p.get("email") or "",
                        "funnel": p.get("funnel_name") or "", "stage": p.get("stage_name") or ""} for p in people],
            "opportunities": opps,
            "history": [{"when": m.get("timestamp"), "channel": m.get("channel"), "direction": m.get("direction"), "contacts": m["contacts"],
                         "text": (m.get("message_text") or "")[:400]} for m in reversed(history)]}      # newest first


def register(app):
    from flask import jsonify

    @app.get("/api/calendar/companies/<int:company_id>/overview")
    def company_overview(company_id):
        out = overview(company_id)
        return (jsonify(out), 200) if out else (jsonify({"error": "not found"}), 404)
