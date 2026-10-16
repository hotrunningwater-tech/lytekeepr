"""Find a CRM contact by name, company, email address or channel handle, and read their full record for the AI.

Used by the Home chat: the AI can email someone you only describe by company, and it writes from what is saved about them
(details, notes, raw notes, funnel stage, statuses, recent messages), not from the messages alone.
"""

from __future__ import annotations

import difflib

from crm import db as crm_db


def find_people(query: str, *, email_only: bool = False) -> list[dict]:
    """Matching people as [{person_id, name, company, email}]. name falls back to the company when the contact has no name."""
    typed = (query or "").strip().lower()
    if not typed:
        return []
    like = f"%{typed}%"
    conn = crm_db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT p.person_id, p.name, co.company_name AS company,
                   (SELECT channel_identifier FROM channel_accounts e WHERE e.person_id = p.person_id AND e.channel = 'email' ORDER BY e.account_id LIMIT 1) AS email
            FROM people p
            LEFT JOIN companies co ON co.company_id = p.company_id
            LEFT JOIN channel_accounts a ON a.person_id = p.person_id
            WHERE lower(IFNULL(p.name, '')) LIKE ? OR lower(IFNULL(co.company_name, '')) LIKE ?
                  OR lower(IFNULL(a.channel_identifier, '')) LIKE ? OR lower(IFNULL(a.url, '')) LIKE ?
            """,
            (like, like, like, like),
        ).fetchall()
        if not rows:                                   # tolerate small typos
            everyone = conn.execute(
                """
                SELECT p.person_id, p.name, co.company_name AS company,
                       (SELECT channel_identifier FROM channel_accounts e WHERE e.person_id = p.person_id AND e.channel = 'email' ORDER BY e.account_id LIMIT 1) AS email
                FROM people p LEFT JOIN companies co ON co.company_id = p.company_id
                """
            ).fetchall()
            best, best_ratio = None, 0.0
            for r in everyone:
                words = []
                for text in (r["name"], r["company"]):
                    t = (text or "").strip().lower()
                    if t:
                        words += t.split() + [t]
                ratio = max((difflib.SequenceMatcher(None, typed, w).ratio() for w in words), default=0.0)
                if ratio > best_ratio:
                    best, best_ratio = r, ratio
            rows = [best] if best is not None and best_ratio >= 0.75 else []
    finally:
        conn.close()
    people = {r["person_id"]: {"person_id": r["person_id"], "name": (r["name"] or r["company"] or "").strip(), "company": r["company"] or "", "email": r["email"] or ""} for r in rows}
    out = list(people.values())
    return [p for p in out if p["email"]] if email_only else out


def describe(p: dict) -> str:
    return f"{p['name']}" + (f" ({p['company']})" if p["company"] and p["company"] != p["name"] else "")


def contact_record(query: str) -> dict:
    """The AI-facing answer: the single matching contact's full record as text, or a clear not_found / ambiguous."""
    people = find_people(query)
    if not people:
        return {"status": "not_found", "message": f"No contact in the CRM matches '{query}'."}
    if len(people) > 1:
        return {"status": "ambiguous", "message": f"More than one contact matches '{query}': " + "; ".join(describe(p) for p in people[:8]) + ". Ask the user which one."}
    from lk_calendar import contact_ai
    p = people[0]
    return {"status": "found", "person_name": p["name"], "email": p["email"], "record": contact_ai._ask_context(p["person_id"])}


def check_reply(query: str, channel: str = "") -> dict:
    """Did this contact reply? Compares their latest incoming message with the user's latest outgoing one (same history the contact page shows)."""
    people = find_people(query)
    if not people:
        return {"status": "not_found", "message": f"No contact in the CRM matches '{query}'."}
    if len(people) > 1:
        return {"status": "ambiguous", "message": f"More than one contact matches '{query}': " + "; ".join(describe(p) for p in people[:8]) + ". Ask the user which one."}
    from lk_calendar import contacts
    p = people[0]
    with contacts._crm() as conn:
        detail = crm_db.get_person_detail(conn, p["person_id"]) or {}
    msgs = contacts.messages_with_saved_emails(p["person_id"], detail.get("messages") or [])
    ch = (channel or "").lower()
    if ch:
        msgs = [m for m in msgs if (m.get("channel") or "").lower() == ch]

    def latest(direction):
        best = None
        for m in msgs:
            if (m.get("direction") or "").lower() != direction:
                continue
            d = contacts._when(m.get("timestamp"))
            if d and (best is None or d > best[0]):
                best = (d, m)
        return best

    def show(best):
        d, m = best
        return {"when": d.strftime("%b %d, %I:%M %p").replace(" 0", " "), "channel": m.get("channel"), "text": (m.get("message_text") or "")[:600]}

    inc, out = latest("incoming"), latest("outgoing")
    name = p["name"]
    if not inc and not out:
        return {"status": "found", "person_name": name, "answer": "No messages with this contact yet."}
    if not inc:
        return {"status": "found", "person_name": name, "answer": f"{name} has not written to you. Your last message to them:", "your_last_message": show(out)}
    if not out or inc[0] > out[0]:
        return {"status": "found", "person_name": name, "answer": f"Yes, {name} wrote after your last message." if out else f"{name} has written to you; you have not written to them.",
                "their_latest_message": show(inc), "your_last_message": show(out) if out else None}
    return {"status": "found", "person_name": name, "answer": f"No reply yet: {name}'s last message came before yours.",
            "your_last_message": show(out), "their_latest_message": show(inc)}
