"""Gone quiet: contacts in a funnel nobody has been in touch with for a while.
The last contact with a person is the newest of: their messages (email, LinkedIn, Instagram), emails saved from Gmail to or
from their address, recorded calls they were on, conversations logged by hand, and outreach status changes ("dm sent" ...).
Listed: contacts in a funnel who were contacted at least once, last more than `days` days ago.
Left out: contacts the follow-up system already has an open reminder for (they show under Follow-ups), and contacts put off
with "Not now" until that date (kept in sync_state under 'quiet_snoozed', a {person_id: date} map).
Plain code, no AI. A team member gets only the contacts they may see."""

import json
from datetime import date, datetime, timedelta

from . import access, store

DEFAULT_DAYS = 14
SNOOZE_KEY = "quiet_snoozed"


class QuietError(ValueError):
    pass


def _moment(text):
    from .recent import _moment as m              # the same real-moment rule as Recent activity
    return m(text)


def last_contacts(person_ids: list) -> dict:
    """person_id -> {"at": the newest moment (as stored), "how": "email" | "linkedin" | "instagram" | "call" | "logged" | "outreach"}."""
    from . import contacts
    if not person_ids:
        return {}
    best: dict = {}

    def offer(pid, at, how):
        if pid in wanted and at and (pid not in best or _moment(at) > _moment(best[pid]["at"])):
            best[pid] = {"at": at, "how": how}

    wanted = set(person_ids)
    marks = ",".join("?" for _ in wanted)
    ids = sorted(wanted)
    with contacts._crm() as conn:
        for r in conn.execute(f"SELECT person_id, channel, MAX(timestamp) AS at FROM messages WHERE person_id IN ({marks}) GROUP BY person_id, channel", ids):
            offer(r["person_id"], r["at"], r["channel"])
        for r in conn.execute(f"""SELECT cp.person_id, MAX(c.started_at) AS at FROM call_people cp JOIN calls c ON c.call_id = cp.call_id
                                  WHERE cp.person_id IN ({marks}) AND c.status != 'no_show' GROUP BY cp.person_id""", ids):
            offer(r["person_id"], r["at"], "call")
        for r in conn.execute(f"""SELECT person_id, MAX(happened_at) AS at FROM contact_records
                                  WHERE kind = 'conversation' AND deleted_at = '' AND person_id IN ({marks}) GROUP BY person_id""", ids):
            offer(r["person_id"], r["at"], "logged")
        owners: dict = {}
        for r in conn.execute(f"""SELECT person_id, lower(trim(channel_identifier)) AS addr FROM channel_accounts
                                  WHERE channel = 'email' AND channel_identifier LIKE '%@%' AND person_id IN ({marks})""", ids):
            owners.setdefault(r["addr"], set()).add(r["person_id"])
    if owners:                                    # emails saved from Gmail, matched by address (one pass over the saved index)
        try:
            from gmail import storage
            for account_id in storage.list_account_ids():
                for entry in storage.load_index(account_id).values():
                    at = entry.get("receivedAt") or ""
                    addrs = {((entry.get("from") or {}).get("email") or "").strip().lower()}
                    addrs |= {((t or {}).get("email") or "").strip().lower() for t in (entry.get("to") or [])}
                    for a in addrs & owners.keys():
                        for pid in owners[a]:
                            offer(pid, at, "email")
        except Exception:                         # no Gmail here, or an unreadable index: the other sources still count
            pass
    with store._conn() as conn:
        for r in conn.execute(f"""SELECT person_id, MAX(status_date) AS at FROM leads WHERE status != 'new' AND person_id IN ({marks})
                                  GROUP BY person_id""", ids):
            offer(r["person_id"], r["at"], "outreach")
    return best


def _covered() -> set:
    """Contacts with an open follow-up reminder: those are already under Follow-ups."""
    with store._conn() as conn:
        rows = conn.execute("""SELECT l.person_id FROM events e JOIN leads l ON e.lead_ref = 'lead:' || l.id
                               WHERE e.source = 'auto' AND l.person_id IS NOT NULL
                                 AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.event_id = e.id AND c.occ_date = e.start_date)""").fetchall()
    return {r["person_id"] for r in rows}


def _snoozed() -> dict:
    try:
        return json.loads(store.get_state(SNOOZE_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def gone_quiet(days: int = DEFAULT_DAYS, funnel_id=None, limit: int = 200) -> dict:
    from . import contacts
    days = max(1, min(int(days or DEFAULT_DAYS), 365))
    today = store.now_local().date()
    cutoff = datetime.combine(today - timedelta(days=days), datetime.min.time()).replace(tzinfo=store.TZ).timestamp()
    sql = """SELECT p.person_id, p.name, co.company_name, p.funnel_id, fn.name AS funnel_name, fs.name AS stage_name
             FROM people p JOIN funnels fn ON fn.funnel_id = p.funnel_id
             LEFT JOIN companies co ON co.company_id = p.company_id LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id
             WHERE p.person_status = 'active'"""
    args: list = []
    if funnel_id not in (None, ""):
        sql += " AND p.funnel_id = ?"
        args.append(int(funnel_id))
    with contacts._crm() as conn:
        people = [dict(r) for r in conn.execute(sql, args)]
    visible = access.person_ids()
    if visible is not None:
        people = [p for p in people if p["person_id"] in visible]
    covered = _covered()
    snoozed = {int(k): v for k, v in _snoozed().items() if v > today.isoformat()}
    people = [p for p in people if p["person_id"] not in covered and p["person_id"] not in snoozed]
    last = last_contacts([p["person_id"] for p in people])
    out = []
    for p in people:
        seen = last.get(p["person_id"])
        if not seen or _moment(seen["at"]) >= cutoff:              # never contacted is "new", not "gone quiet"
            continue
        quiet_days = (today - datetime.fromtimestamp(_moment(seen["at"]), store.TZ).date()).days
        out.append({**p, "last_contact_at": seen["at"], "last_contact_how": seen["how"], "days_quiet": quiet_days})
    out.sort(key=lambda x: -x["days_quiet"])
    return {"days": days, "count": len(out), "contacts": out[:max(1, min(int(limit or 200), 500))]}


def snooze(person_id: int, days: int = DEFAULT_DAYS) -> dict:
    """'Not now': off the list for `days` days (it comes back after that if still nobody got in touch)."""
    if not access.can_person(person_id):
        raise QuietError("You don't have access to that contact")
    until = (store.now_local().date() + timedelta(days=max(1, min(int(days or DEFAULT_DAYS), 365)))).isoformat()
    today = store.now_local().date().isoformat()
    marks = {k: v for k, v in _snoozed().items() if v > today}   # old ones drop off
    marks[str(int(person_id))] = until
    store.set_state(SNOOZE_KEY, json.dumps(marks))
    return {"person_id": int(person_id), "until": until}


def register(app):
    from flask import jsonify, request

    @app.get("/api/crm/quiet")
    def quiet_list():
        return jsonify(gone_quiet(request.args.get("days", DEFAULT_DAYS, type=int), request.args.get("funnel_id"),
                                  request.args.get("limit", 200, type=int)))

    @app.post("/api/crm/quiet/<int:person_id>/snooze")
    def quiet_snooze(person_id):
        try:
            return jsonify(snooze(person_id, (request.get_json(silent=True) or {}).get("days", DEFAULT_DAYS)))
        except QuietError as exc:
            return jsonify({"error": str(exc)}), 403
