"""Home's Recent activity: each recently active contact once, with the latest thing that happened with them, whether a message
(email, LinkedIn), a recorded call (Fathom) or a call/meeting/conversation logged by hand. Times are compared as real moments
(messages may be stored in UTC, calls and logged conversations in local time)."""

from datetime import datetime, timezone

from . import store


def _moment(text) -> float:
    try:
        d = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=store.TZ)
    return d.timestamp()


def with_calls(message_rows: list, limit: int = 5) -> list:
    latest = {}                                       # person_id -> the newest item

    def offer(pid, item):
        if pid and (pid not in latest or _moment(item["last_message_at"]) > _moment(latest[pid]["last_message_at"])):
            latest[pid] = item

    base = {r["person_id"]: r for r in message_rows}
    for r in message_rows:
        offer(r["person_id"], {**r, "kind": "message"})
    from . import calls, contacts, interactions
    with calls._crm() as conn:
        rows = conn.execute("SELECT call_id, title, started_at, summary FROM calls ORDER BY started_at DESC LIMIT 50").fetchall()
        people = calls._people_on(conn, [r["call_id"] for r in rows])
        info = {}
        ids = {p["person_id"] for ps in people.values() for p in ps}
        for r in conn.execute(f"""SELECT p.person_id, p.name, co.company_name, fn.name AS funnel_name, fs.name AS funnel_stage_name FROM people p
                                  LEFT JOIN companies co ON co.company_id = p.company_id LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id
                                  LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id
                                  WHERE p.person_id IN ({','.join('?' for _ in ids) or 'NULL'})""", sorted(ids)):
            info[r["person_id"]] = dict(r)
    for c in rows:
        for p in people.get(c["call_id"], []):
            who = base.get(p["person_id"]) or info.get(p["person_id"]) or {"person_id": p["person_id"], "name": p["name"]}
            offer(p["person_id"], {**{k: who.get(k) for k in ("person_id", "name", "company_name", "funnel_name", "funnel_stage_name")},
                                   "kind": "call", "last_channel": "call", "last_message_direction": "call",
                                   "last_message_text": c["title"] or "Call", "last_message_at": c["started_at"], "call_id": c["call_id"]})
    with interactions._crm() as conn:
        recs = conn.execute("""SELECT r.*, p.name FROM contact_records r JOIN people p ON p.person_id = r.person_id
                               WHERE r.kind = 'conversation' AND r.deleted_at = '' ORDER BY r.happened_at DESC LIMIT 50""").fetchall()
    for r in recs:
        who = base.get(r["person_id"]) or info.get(r["person_id"]) or {"person_id": r["person_id"], "name": r["name"]}
        offer(r["person_id"], {**{k: who.get(k) for k in ("person_id", "name", "company_name", "funnel_name", "funnel_stage_name")},
                               "kind": "logged", "last_channel": r["channel"] or "conversation", "last_message_direction": "logged",
                               "last_message_text": (r["text"] or "")[:200], "last_message_at": r["happened_at"]})
    return sorted(latest.values(), key=lambda x: _moment(x["last_message_at"]), reverse=True)[:limit]
