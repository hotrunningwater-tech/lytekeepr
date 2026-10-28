"""Who is waiting: every contact (optionally of one brand) whose conversation stopped, split by who owes the next message.
For each contact the last message is found in their messages (email, LinkedIn, Instagram), the emails saved in LyteKeepr to or from
their address, and outreach steps ("dm sent" counts as yours, "replied" as theirs). Then:
- waiting_on_you: they wrote last and you haven't answered for `you_days` days (3 by default);
- waiting_on_them: you wrote last and they haven't answered for `days` days (14 by default).
A recorded call or a logged conversation after the last message means you were in touch: nobody is waiting.
Each row says whether a follow-up is planned for them, and whether it is overdue (then it is on you).
Left out: contacts marked "not interested" on any channel (moving someone to Lost sets that), contacts never written to, and contacts put
off with "Not now" until that date (kept in sync_state under 'quiet_snoozed', a {person_id: date} map).
Plain code, no AI. A team member gets only the contacts they may see."""

import json
from datetime import datetime, timedelta

from . import access, store

DEFAULT_DAYS = 14
DEFAULT_YOU_DAYS = 3
SNOOZE_KEY = "quiet_snoozed"
SNIPPET = 160


class QuietError(ValueError):
    pass


def _moment(text):
    from .recent import _moment as m              # the same real-moment rule as Recent activity
    return m(text)


def last_touches(person_ids: list) -> dict:
    """person_id -> {"in": newest message from them, "out": newest from you, "touch": newest call / logged conversation};
    each {"at", "how", "text"} or None."""
    from . import contacts
    if not person_ids:
        return {}
    wanted = set(person_ids)
    best = {pid: {"in": None, "out": None, "touch": None} for pid in wanted}

    def offer(pid, side, at, how, text=""):
        if pid in wanted and at:
            cur = best[pid][side]
            if cur is None or _moment(at) > _moment(cur["at"]):
                best[pid][side] = {"at": at, "how": how, "text": (text or "")[:SNIPPET]}

    marks = ",".join("?" for _ in wanted)
    ids = sorted(wanted)
    with contacts._crm() as conn:
        for r in conn.execute(f"""SELECT m.person_id, m.channel, m.direction, m.message_text, m.timestamp FROM messages m
                                  WHERE m.person_id IN ({marks}) AND m.timestamp = (SELECT MAX(x.timestamp) FROM messages x
                                        WHERE x.person_id = m.person_id AND x.direction = m.direction AND x.channel = m.channel)""", ids):
            offer(r["person_id"], "in" if r["direction"] == "incoming" else "out", r["timestamp"], r["channel"], r["message_text"])
        owners: dict = {}
        for r in conn.execute(f"""SELECT person_id, lower(trim(channel_identifier)) AS addr FROM channel_accounts
                                  WHERE channel = 'email' AND channel_identifier LIKE '%@%' AND person_id IN ({marks})""", ids):
            owners.setdefault(r["addr"], set()).add(r["person_id"])
    from . import calls, interactions              # their own connections make sure the tables exist
    with calls._crm() as conn:
        for r in conn.execute(f"""SELECT cp.person_id, MAX(c.started_at) AS at FROM call_people cp JOIN calls c ON c.call_id = cp.call_id
                                  WHERE cp.person_id IN ({marks}) AND c.status != 'no_show' GROUP BY cp.person_id""", ids):
            offer(r["person_id"], "touch", r["at"], "call")
    with interactions._crm() as conn:
        for r in conn.execute(f"""SELECT person_id, MAX(happened_at) AS at FROM contact_records
                                  WHERE kind = 'conversation' AND deleted_at = '' AND person_id IN ({marks}) GROUP BY person_id""", ids):
            offer(r["person_id"], "touch", r["at"], "logged")
    if owners:                                    # emails saved in LyteKeepr, matched by address (one pass over the saved index)
        try:
            from gmail import storage
            for account_id in storage.list_account_ids():
                for entry in storage.load_index(account_id).values():
                    at = entry.get("receivedAt") or ""
                    sender = ((entry.get("from") or {}).get("email") or "").strip().lower()
                    for pid in owners.get(sender, ()):
                        offer(pid, "in", at, "email", entry.get("snippet") or entry.get("subject") or "")
                    for t in entry.get("to") or []:
                        for pid in owners.get(((t or {}).get("email") or "").strip().lower(), ()):
                            offer(pid, "out", at, "email", entry.get("subject") or "")
        except Exception:                         # no Gmail here, or an unreadable index: the other sources still count
            pass
    with store._conn() as conn:
        buckets = store.bucket_map(conn)
        for r in conn.execute(f"SELECT person_id, channel, status, status_date FROM leads WHERE status != 'new' AND person_id IN ({marks})", ids):
            side = {"cold": "out", "warm": "in"}.get(buckets.get((r["channel"], r["status"]), "none"))
            if side:
                offer(r["person_id"], side, r["status_date"], "outreach")
    return best


def by_channel(person_ids: list) -> dict:
    """person_id -> channel -> {"in": newest from them, "out": newest from you}, each {"at", "how", "text"} or None:
    the live state of each conversation (a status says how outreach went; this says who owes the next message)."""
    from . import contacts
    if not person_ids:
        return {}
    wanted, ids = set(person_ids), sorted(set(person_ids))
    marks = ",".join("?" for _ in ids)
    out: dict = {}

    def offer(pid, ch, side, at, text=""):
        if pid not in wanted or not at:
            return
        slot = out.setdefault(pid, {}).setdefault(ch, {"in": None, "out": None})
        if slot[side] is None or _moment(at) > _moment(slot[side]["at"]):
            slot[side] = {"at": at, "how": ch, "text": (text or "")[:SNIPPET]}

    with contacts._crm() as conn:
        for r in conn.execute(f"""SELECT m.person_id, m.channel, m.direction, m.message_text, m.timestamp FROM messages m
                                  WHERE m.person_id IN ({marks}) AND m.timestamp = (SELECT MAX(x.timestamp) FROM messages x
                                        WHERE x.person_id = m.person_id AND x.direction = m.direction AND x.channel = m.channel)""", ids):
            offer(r["person_id"], r["channel"], "in" if r["direction"] == "incoming" else "out", r["timestamp"], r["message_text"])
        owners: dict = {}
        for r in conn.execute(f"""SELECT person_id, lower(trim(channel_identifier)) AS addr FROM channel_accounts
                                  WHERE channel = 'email' AND channel_identifier LIKE '%@%' AND person_id IN ({marks})""", ids):
            owners.setdefault(r["addr"], set()).add(r["person_id"])
    if owners:
        try:
            from gmail import storage
            for account_id in storage.list_account_ids():
                for entry in storage.load_index(account_id).values():
                    at = entry.get("receivedAt") or ""
                    for pid in owners.get(((entry.get("from") or {}).get("email") or "").strip().lower(), ()):
                        offer(pid, "email", "in", at, entry.get("snippet") or "")
                    for t in entry.get("to") or []:
                        for pid in owners.get(((t or {}).get("email") or "").strip().lower(), ()):
                            offer(pid, "email", "out", at)
        except Exception:
            pass
    return out


def conversation(slot: dict | None) -> dict | None:
    """{"waiting_on": "you" | "them", "at": when the last message was written} for one channel's conversation, or None."""
    if not slot:
        return None
    msgs = [m for m in (slot.get("in"), slot.get("out")) if m]
    if not msgs:
        return None
    last = max(msgs, key=lambda m: _moment(m["at"]))
    return {"waiting_on": "you" if last is slot.get("in") else "them",
            "at": datetime.fromtimestamp(_moment(last["at"]), store.TZ).date().isoformat()}


def _follow_ups() -> dict:
    """person_id -> the date of their earliest open follow-up reminder (shown on the row: planned, or overdue = on you)."""
    with store._conn() as conn:
        rows = conn.execute("""SELECT l.person_id, MIN(e.start_date) AS d FROM events e JOIN leads l ON e.lead_ref = 'lead:' || l.id
                               WHERE e.source = 'auto' AND l.person_id IS NOT NULL
                                 AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.event_id = e.id AND c.occ_date = e.start_date)
                               GROUP BY l.person_id""").fetchall()
    return {r["person_id"]: r["d"] for r in rows}


def _snoozed() -> dict:
    try:
        return json.loads(store.get_state(SNOOZE_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def waiting(days: int = DEFAULT_DAYS, you_days: int = DEFAULT_YOU_DAYS, brand: str = "", side: str = "", limit: int = 200) -> dict:
    """Every contact (of `brand` if given) whose conversation stopped, split into waiting_on_you and waiting_on_them."""
    from . import brands as brand_mod, contacts
    days = max(1, min(int(days or DEFAULT_DAYS), 365))
    you_days = max(1, min(int(you_days or DEFAULT_YOU_DAYS), 365))
    brand = brand_mod.resolve(brand) if brand else ""
    today = store.now_local().date()
    with contacts._crm() as conn:
        people = [dict(r) for r in conn.execute("""SELECT p.person_id, p.name, co.company_name, fn.name AS funnel_name, fs.name AS stage_name,
                                                          COALESCE(NULLIF(fn.brand, ''), p.brand, '') AS brand
                                                   FROM people p LEFT JOIN companies co ON co.company_id = p.company_id
                                                   LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id""")]
    if brand:
        people = [p for p in people if p["brand"] == brand]
    visible = access.person_ids()
    if visible is not None:
        people = [p for p in people if p["person_id"] in visible]
    with store._conn() as conn:                                    # "not interested" on any channel: never waiting, on either side
        done = {r["person_id"] for r in conn.execute("SELECT person_id FROM leads WHERE status = 'done' AND person_id IS NOT NULL")}
    people = [p for p in people if p["person_id"] not in done]
    follow_ups = _follow_ups()
    snoozed = {int(k) for k, v in _snoozed().items() if v > today.isoformat()}
    people = [p for p in people if p["person_id"] not in snoozed]
    touches = last_touches([p["person_id"] for p in people])
    out = {"waiting_on_you": [], "waiting_on_them": []}
    for p in people:
        t = touches.get(p["person_id"]) or {}
        last_in, last_out, touch = t.get("in"), t.get("out"), t.get("touch")
        msgs = [m for m in (last_in, last_out) if m]
        if not msgs:
            continue                                               # never written to: new, not waiting
        last = max(msgs, key=lambda m: _moment(m["at"]))
        if touch and _moment(touch["at"]) >= _moment(last["at"]):
            continue                                               # a call or a logged talk after it: you were in touch
        quiet = (today - datetime.fromtimestamp(_moment(last["at"]), store.TZ).date()).days
        theirs = last is last_in
        if quiet < (you_days if theirs else days):
            continue
        row = {**p, "contact_id": p["person_id"], "side": "waiting_on_you" if theirs else "waiting_on_them", "days_quiet": quiet,
               "last_at": last["at"], "last_how": last["how"]}
        if p["person_id"] in follow_ups:                           # a follow-up is planned; overdue means it is on you
            d = follow_ups[p["person_id"]]
            row["follow_up"] = {"date": d, "overdue": d < today.isoformat()}
        if theirs and access.is_full():                            # their words: message previews belong to whoever owns the mailbox
            row["their_last_words"] = last["text"]
        out[row["side"]].append(row)
    for k in out:
        out[k].sort(key=lambda x: -x["days_quiet"])
        out[k] = out[k][:max(1, min(int(limit or 200), 500))]
    if side in ("you", "waiting_on_you"):
        out["waiting_on_them"] = []
    elif side in ("them", "waiting_on_them"):
        out["waiting_on_you"] = []
    return {"days": days, "you_days": you_days, "brand": brand, **out,
            "count": len(out["waiting_on_you"]) + len(out["waiting_on_them"])}


def snooze(person_id: int, days: int = DEFAULT_DAYS) -> dict:
    """'Not now': off both lists for `days` days (it comes back after that if the conversation is still stuck)."""
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
        a = request.args
        try:
            return jsonify(waiting(a.get("days", DEFAULT_DAYS, type=int), a.get("you_days", DEFAULT_YOU_DAYS, type=int), a.get("brand", ""),
                                   a.get("side", ""), a.get("limit", 200, type=int)))
        except store.ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/crm/quiet/<int:person_id>/snooze")
    def quiet_snooze(person_id):
        try:
            return jsonify(snooze(person_id, (request.get_json(silent=True) or {}).get("days", DEFAULT_DAYS)))
        except QuietError as exc:
            return jsonify({"error": str(exc)}), 403
