"""Who may see which contacts.

The admin, and anyone the admin gave "Everything", see the whole database. Everyone else sees the contacts in the funnels that were
shared with them, plus the contacts they added themselves. The rule lives here, in one place; the routes only ask these functions.
"""

from flask import g, jsonify

from . import users


def _current() -> dict | None:
    try:
        return getattr(g, "user", None)
    except RuntimeError:
        return None


def is_full() -> bool:
    """The whole database: the admin, someone given "Everything", or nobody signed in (the login gate is off here)."""
    user = _current()
    return user is None or user.get("role") == "admin" or user.get("access") == "all"


def _cached(name, build):
    try:
        if not hasattr(g, "_access"):
            g._access = {}
        if name not in g._access:
            g._access[name] = build()
        return g._access[name]
    except RuntimeError:
        return build()


def funnel_ids() -> set | None:
    """The funnels this person may see; None = all of them."""
    if is_full():
        return None
    return _cached("funnels", lambda: users.funnels_shared_with(_current()["id"]))


def person_ids() -> set | None:
    """The contacts this person may see; None = all of them."""
    if is_full():
        return None

    def build():
        from . import contacts
        funnels, me = funnel_ids(), _current()["id"]
        with contacts._crm() as conn:
            marks = ",".join("?" for _ in funnels) or "NULL"
            rows = conn.execute(f"SELECT person_id FROM people WHERE owner_id = ? OR funnel_id IN ({marks})", [me, *sorted(funnels)])
            ids = {r["person_id"] for r in rows}
            from . import opportunities                     # the people on opportunities in those funnels, to work with them
            return ids | opportunities.visible_person_ids(conn, funnels)
    return _cached("people", build)


def can_person(person_id) -> bool:
    ids = person_ids()
    return ids is None or int(person_id or 0) in ids


def can_opportunity(opp: dict | None) -> bool:
    """An opportunity is visible with its funnel (or to whoever created it)."""
    if not opp:
        return False
    if is_full():
        return True
    return can_funnel(opp.get("funnel_id")) or (opp.get("owner_id") is not None and opp.get("owner_id") == (_current() or {}).get("id"))


def can_funnel(funnel_id) -> bool:
    ids = funnel_ids()
    return ids is None or int(funnel_id or 0) in ids


def forget() -> None:
    """After something changed who can see what within this request (a contact was created, a funnel shared): look again."""
    try:
        if hasattr(g, "_access"):
            del g._access
    except RuntimeError:
        pass


def denied(what: str = "that"):
    return jsonify({"error": f"You do not have access to {what}."}), 403


# ------------------------------------------------------------------ the request guard (applied to every request, see server.py)
import re

_PERSON_PATHS = (re.compile(r"^/api/crm/people/(\d+)(/.*)?$"), re.compile(r"^/api/calendar/contacts/(\d+)(/.*)?$"), re.compile(r"^/api/calendar/people/(\d+)(/.*)?$"))
_FUNNEL_PATH = re.compile(r"^/api/crm/funnels/(\d+)/(board|delete|stages)$")
_STRUCTURE_PATHS = (re.compile(r"^/api/crm/funnels/\d+/stages$"), re.compile(r"^/api/crm/funnels/stages/\d+/(rename|move-position|delete)$"))
_LEAD_PATH = re.compile(r"^/api/calendar/leads/(\d+)$")
_EVENT_PATH = re.compile(r"^/api/calendar/events/(\d+)(/.*)?$")
_COMPANY_PATH = re.compile(r"^/api/calendar/companies/(\d+)$")


def lead_person(lead_id):
    from . import store
    with store._conn() as conn:
        row = conn.execute("SELECT person_id FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    return row["person_id"] if row else None


def can_event(event: dict) -> bool:
    """An event is visible when it belongs to a contact the person may see, or it is theirs (created by them, or from their own Google
    Calendar). The admin's own events and Google Calendar are only for those with the whole database."""
    if is_full():
        return True
    lead = event.get("lead")
    if lead:
        return can_person(lead.get("person_id"))
    return event.get("owner_id") is not None and event.get("owner_id") == (_current() or {}).get("id")      # incl. their own Google Calendar


def can_company(company_id) -> bool:
    ids = person_ids()
    if ids is None:
        return True
    from . import contacts
    with contacts._crm() as conn:
        return any(r["person_id"] in ids for r in conn.execute("SELECT person_id FROM people WHERE company_id = ?", (int(company_id),)))


def guard(path: str, method: str, body: dict):
    """A 403 response when this request touches something the person may not see, else None. Admins and full-access people pass at once."""
    if is_full() or not path.startswith(("/api/crm/", "/api/calendar/")):
        return None
    for pattern in _PERSON_PATHS:
        m = pattern.match(path)
        if m and not can_person(m.group(1)):
            return denied("that contact")
    m = _FUNNEL_PATH.match(path)
    if m and not can_funnel(m.group(1)):
        return denied("that funnel")
    if any(p.match(path) for p in _STRUCTURE_PATHS):
        return denied("changing funnel stages (ask the admin)")
    if path == "/api/crm/funnels/move":
        ids = body.get("person_ids") if isinstance(body.get("person_ids"), list) else []
        if any(not can_person(i) for i in ids) or (body.get("funnel_id") is not None and not can_funnel(body.get("funnel_id"))):
            return denied("that contact or funnel")
    if path == "/api/crm/funnels/stages/move" and not can_person(body.get("person_id")):
        return denied("that contact")
    if path == "/api/crm/people/merge" and not (can_person(body.get("keep_person_id")) and can_person(body.get("merge_person_id"))):
        return denied("that contact")
    m = _LEAD_PATH.match(path)
    if m and not can_person(lead_person(m.group(1))):
        return denied("that contact")
    m = _EVENT_PATH.match(path)
    if m and method != "POST" or (m and path.endswith(("/done", "/meeting"))):
        from . import store
        with store._conn() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (int(m.group(1)),)).fetchone()
        if row:
            ev = dict(row)
            if ev["lead_ref"].startswith("lead:"):
                with store._conn() as conn:
                    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (int(ev["lead_ref"][5:] or 0),)).fetchone()
                ev["lead"] = dict(lead) if lead else None
            if not can_event(ev):
                return denied("that task")
    m = _COMPANY_PATH.match(path)
    if m and not can_company(m.group(1)):
        return denied("that company")
    return None
