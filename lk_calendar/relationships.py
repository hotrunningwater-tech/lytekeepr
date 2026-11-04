"""Who is in touch with whom: a relationship graph built by plain code from the emails saved in LyteKeepr (no AI).

From each saved email only the addresses (From, To, Cc), the thread and the date are read, plus the new (unquoted) part of
the text to notice when a contact's full name is mentioned (a flag on a suggestion, never a suggestion by itself).
Left out: you and your team, generic addresses (noreply@, info@, support@ ...) and big group emails (more than 10 people).

The graph gives:
- pairs: how many separate threads two people share, first and last time;
- introductions: someone who first appears in a thread because a participant added them (the sender of that message);
and from it, suggestions that wait in Needs attention for Accept or Dismiss (a dismissed one never comes back):
- "add_to_opportunity": a person keeps appearing (2+ threads) with someone on an opportunity they are not on;
- "new_contact": someone a contact brought into a thread keeps appearing (2+ threads) but is not a contact yet.
Nothing is ever added without the user accepting it.
"""

import json
import logging
import re
from contextlib import contextmanager

from . import store

log = logging.getLogger(__name__)
MIN_THREADS = 2
MAX_PEOPLE = 10
CLOSE_DAYS = 90          # repeated threads count only when they happen within this many days of each other
FADE_DAYS = 180          # a pair with nothing newer than this makes no suggestion
GENERIC = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|info|support|hello|contact|team|admin|notifications?|notify|mailer-daemon|postmaster|"
                     r"billing|invoices?|accounts?|sales|news|newsletters?|updates?|marketing|help|service|bounce[s]?|calendar|events?)([+._-].*)?$", re.I)
GENERIC_DOMAINS = ("calendar.google.com", "docs.google.com", "zoom.us", "mailchimp", "sendgrid", "hubspot", "intercom", "linkedin.com", "facebookmail.com")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rel_seen (message_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS rel_people (email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL DEFAULT '', last_seen TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS rel_thread_people (thread_id TEXT NOT NULL, email TEXT NOT NULL, first_seen TEXT NOT NULL, PRIMARY KEY (thread_id, email));
CREATE TABLE IF NOT EXISTS rel_pair_threads (a TEXT NOT NULL, b TEXT NOT NULL, thread_id TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                                             PRIMARY KEY (a, b, thread_id));
CREATE TABLE IF NOT EXISTS rel_intros (thread_id TEXT NOT NULL, introducer TEXT NOT NULL, newcomer TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (thread_id, newcomer));
CREATE TABLE IF NOT EXISTS rel_mentions (person_id INTEGER NOT NULL, thread_id TEXT NOT NULL, sender TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (person_id, thread_id));
CREATE TABLE IF NOT EXISTS rel_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, opportunity_id INTEGER NOT NULL DEFAULT 0, email TEXT NOT NULL,
    with_email TEXT NOT NULL DEFAULT '', introducer TEXT NOT NULL DEFAULT '', threads INTEGER NOT NULL DEFAULT 0, mentions INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (kind, opportunity_id, email)
);
"""


@contextmanager
def _crm():
    from . import contacts, opportunities
    with contacts._crm() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(opportunities.SCHEMA)     # suggestions name their opportunity; a brand-new workspace has none yet
        yield conn


def _now() -> str:
    return store.now_local().isoformat(timespec="minutes")


def _addr(x) -> tuple[str, str]:
    x = x or {}
    return (x.get("email") or "").strip().lower(), (x.get("name") or "").strip()


def is_generic(email: str) -> bool:
    if "@" not in email:
        return True
    local, domain = email.split("@", 1)
    return bool(GENERIC.match(local)) or any(g in domain for g in GENERIC_DOMAINS)


def _team_emails() -> set:
    """You and your team: never part of the graph (you are on every email)."""
    out = set()
    try:
        with store._conn() as conn:
            out |= {(r["email"] or "").lower() for r in conn.execute("SELECT email FROM users")}
    except Exception:
        pass
    try:
        from gmail import storage
        out |= {a.lower() for a in storage.list_account_ids() if "@" in a}
    except Exception:
        pass
    return {e for e in out if e}


def _new_part(text: str) -> str:
    """The text a person actually wrote in this email: stop at the quoted earlier messages."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^On .{5,120} wrote:$", s) or re.match(r"^-{2,}\s*(Original|Forwarded) Message", s, re.I) or re.match(r"^From:\s", s):
            break
        lines.append(line)
    return "\n".join(lines)


# ------------------------------------------------------------------ reading the saved emails into the graph
def _saved_messages():
    from gmail import storage
    for account_id in storage.list_account_ids():
        for message_id, entry in storage.load_index(account_id).items():
            yield account_id, message_id, entry


def ingest() -> int:
    """Add every saved email not seen yet (oldest first, so introductions are read in order). Returns how many were added."""
    from gmail import storage
    team = _team_emails()
    with _crm() as conn:
        seen = {r["message_id"] for r in conn.execute("SELECT message_id FROM rel_seen")}
        names = [(r["person_id"], r["name"]) for r in conn.execute("SELECT person_id, name FROM people WHERE name LIKE '% %'")]
    todo = sorted(((a, m, e) for a, m, e in _saved_messages() if m not in seen), key=lambda x: x[2].get("receivedAt") or "")
    added = 0
    for account_id, message_id, entry in todo:
        full = storage.load_message(account_id, message_id) or {}
        sender, sender_name = _addr(full.get("from") or entry.get("from"))
        people = [(sender, sender_name)] + [_addr(t) for t in (full.get("to") or entry.get("to") or [])] + [_addr(t) for t in (full.get("cc") or [])]
        thread = str(entry.get("threadId") or full.get("threadId") or message_id)
        at = (entry.get("receivedAt") or full.get("sentAt") or "")[:16]
        everyone = {e for e, _ in people if e}
        kept = {}
        for e, n in people:
            if e and e not in team and not is_generic(e):
                kept.setdefault(e, n)
        with _crm() as conn:
            conn.execute("INSERT OR IGNORE INTO rel_seen VALUES (?)", (message_id,))
            added += 1
            if len(everyone) > MAX_PEOPLE or not kept or is_generic(sender):
                continue                                   # a big group email, an automated one (noreply@ ...), or only you and generic addresses
            before = {r["email"] for r in conn.execute("SELECT email FROM rel_thread_people WHERE thread_id = ?", (thread,))}
            for e, n in kept.items():
                conn.execute("""INSERT INTO rel_people (email, name, first_seen, last_seen) VALUES (?, ?, ?, ?)
                                ON CONFLICT(email) DO UPDATE SET name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                                last_seen = MAX(last_seen, excluded.last_seen)""", (e, n, at, at))
                conn.execute("INSERT OR IGNORE INTO rel_thread_people VALUES (?, ?, ?)", (thread, e, at))
                if before and e not in before and e != sender and sender in kept:      # someone added to a running thread by the sender
                    conn.execute("INSERT OR IGNORE INTO rel_intros VALUES (?, ?, ?, ?)", (thread, sender, e, at))
            ems = sorted(kept)
            for i in range(len(ems)):
                for j in range(i + 1, len(ems)):
                    conn.execute("""INSERT INTO rel_pair_threads VALUES (?, ?, ?, ?, ?)
                                    ON CONFLICT(a, b, thread_id) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen)""", (ems[i], ems[j], thread, at, at))
            body = _new_part(full.get("textBody") or "")
            if body and names:
                low = body.lower()
                for pid, name in names:
                    if name and name.lower() in low:
                        conn.execute("INSERT OR IGNORE INTO rel_mentions VALUES (?, ?, ?, ?)", (pid, thread, sender, at))
    return added


# ------------------------------------------------------------------ reading the graph
def _contact_by_email(conn) -> dict:
    return {(r["e"] or "").lower(): r["person_id"] for r in conn.execute(
        "SELECT lower(channel_identifier) AS e, person_id FROM channel_accounts WHERE channel = 'email' AND channel_identifier LIKE '%@%'")}


def _emails_of(conn, person_id) -> set:
    return {(r["e"] or "").lower() for r in conn.execute(
        "SELECT lower(channel_identifier) AS e FROM channel_accounts WHERE person_id = ? AND channel = 'email' AND channel_identifier LIKE '%@%'", (person_id,))}


def connections(person_id: int, limit: int = 15) -> list[dict]:
    """The people this contact keeps appearing with in emails, strongest first."""
    with _crm() as conn:
        mine = _emails_of(conn, person_id)
        if not mine:
            return []
        marks = ",".join("?" for _ in mine)
        per = {}
        for r in conn.execute(f"""SELECT CASE WHEN a IN ({marks}) THEN b ELSE a END AS other, thread_id, MIN(first_seen) AS seen
                                  FROM rel_pair_threads WHERE a IN ({marks}) OR b IN ({marks}) GROUP BY other, thread_id""", [*mine, *mine, *mine]):
            per.setdefault(r["other"], []).append(r["seen"])
        now = _now()
        ranked = sorted(((other, _pair_stats(ds, now)) for other, ds in per.items()), key=lambda x: x[1]["last"], reverse=True)   # newest first...
        ranked.sort(key=lambda x: -x[1]["score"])                                                                            # ...then strongest
        rows = [{"other": o, "threads": st["threads"], "last_seen": st["last"], "score": st["score"], "recent": st["recent"]} for o, st in ranked[: int(limit)]]
        by_email = _contact_by_email(conn)
        names = {r["email"]: r["name"] for r in conn.execute("SELECT email, name FROM rel_people")}
        out = []
        for r in rows:
            if r["other"] in mine:
                continue
            pid = by_email.get(r["other"])
            pname = conn.execute("SELECT name FROM people WHERE person_id = ?", (pid,)).fetchone()["name"] if pid else ""
            out.append({"email": r["other"], "name": pname or names.get(r["other"], "") or r["other"], "contact_id": pid,
                        "threads": r["threads"], "recent_threads": r["recent"], "last_seen": r["last_seen"], "strength": r["score"]})
    return out


def _days(a: str, b: str) -> float:
    from datetime import datetime
    try:
        return abs((datetime.fromisoformat(a[:16]) - datetime.fromisoformat(b[:16])).total_seconds()) / 86400
    except ValueError:
        return 9999.0


def _weight(seen: str, now: str) -> float:
    """How much one shared thread counts today: recent threads count fully, old ones fade."""
    d = _days(seen, now)
    return 1.0 if d <= CLOSE_DAYS else 0.5 if d <= FADE_DAYS else 0.2


def _pair_stats(thread_dates: list, now: str) -> dict:
    """threads (all), recent (inside FADE_DAYS), close (most threads within any CLOSE_DAYS window), score, last."""
    dates = sorted(d for d in thread_dates if d)
    close, j = 0, 0
    for i in range(len(dates)):
        while _days(dates[i], dates[j]) > CLOSE_DAYS:
            j += 1
        close = max(close, i - j + 1)
    return {"threads": len(dates), "recent": sum(1 for d in dates if _days(d, now) <= FADE_DAYS), "close": close,
            "score": round(sum(_weight(d, now) for d in dates), 2), "last": dates[-1] if dates else ""}


def _qualifies(st: dict) -> bool:
    """A suggestion needs repeats close in time (2+ threads within 90 days) and something in the last 6 months."""
    return st["close"] >= MIN_THREADS and st["recent"] >= 1


def _intro_of(conn, email: str, among: set) -> str:
    row = conn.execute(f"SELECT introducer FROM rel_intros WHERE newcomer = ? AND introducer IN ({','.join('?' for _ in among) or 'NULL'}) ORDER BY at LIMIT 1",
                       [email, *sorted(among)]).fetchone()
    return row["introducer"] if row else ""


def suggest() -> int:
    """Look for repeated patterns and add new suggestions (never re-adds one that was dismissed or accepted)."""
    from . import opportunities
    made = 0
    with _crm() as conn:
        conn.executescript(opportunities.SCHEMA)
        by_email = _contact_by_email(conn)
        names = {r["email"]: r["name"] for r in conn.execute("SELECT email, name FROM rel_people")}
        dates = {}
        for r in conn.execute("SELECT a, b, thread_id, MIN(first_seen) AS seen FROM rel_pair_threads GROUP BY a, b, thread_id"):
            dates.setdefault((r["a"], r["b"]), []).append(r["seen"])
        now = _now()
        stats = {k: _pair_stats(v, now) for k, v in dates.items()}
        pairs = {k: (st["close"], st["last"]) for k, st in stats.items() if _qualifies(st)}      # only repeats close in time, not faded
        def together(x, y):
            return pairs.get((min(x, y), max(x, y)), (0, ""))[0]
        # 1. someone who keeps showing up with an opportunity's people, but is not on it
        for o in conn.execute("""SELECT o.opportunity_id, o.name FROM opportunities o JOIN funnels f ON f.funnel_id = o.funnel_id
                                 WHERE f.kind != 'work'""").fetchall():          # work items have no people: never suggested for
            members = conn.execute("SELECT person_id, active FROM opportunity_people WHERE opportunity_id = ?", (o["opportunity_id"],)).fetchall()
            member_ids = {m["person_id"] for m in members}
            active_emails = set()
            for m in members:
                if m["active"]:
                    active_emails |= _emails_of(conn, m["person_id"])
            if not active_emails:
                continue
            candidates = {}
            for (a, b), (n, last) in pairs.items():
                if n < MIN_THREADS:
                    continue
                for x, y in ((a, b), (b, a)):
                    if x in active_emails and y not in active_emails:
                        best = candidates.get(y)
                        if not best or n > best[0]:
                            candidates[y] = (n, x)
            for email, (n, with_email) in candidates.items():
                pid = by_email.get(email)
                if pid in member_ids:
                    continue                                   # already on it (or was, and left: their history decides)
                intro = _intro_of(conn, email, active_emails)
                who = names.get(email) or email
                mentions = conn.execute("SELECT COUNT(*) FROM rel_mentions WHERE person_id = ?", (pid,)).fetchone()[0] if pid else 0
                last = stats.get((min(email, with_email), max(email, with_email)), {}).get("last", "")[:10]
                reason = (f"{who} appeared in {n} separate email threads within 3 months with {names.get(with_email) or with_email} (last {last})"
                          + (f", who brought them in" if intro == with_email else (f"; introduced by {names.get(intro) or intro}" if intro else "")) + ".")
                cur = conn.execute("""INSERT OR IGNORE INTO rel_suggestions (kind, opportunity_id, email, with_email, introducer, threads, mentions, reason, created_at, updated_at)
                                      VALUES ('add_to_opportunity', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                   (o["opportunity_id"], email, with_email, intro, n, mentions, reason, _now(), _now()))
                if cur.rowcount:
                    made += 1
                else:
                    conn.execute("UPDATE rel_suggestions SET threads = ?, reason = ?, updated_at = ? WHERE kind = 'add_to_opportunity' AND opportunity_id = ? AND email = ? AND status = 'open'",
                                 (n, reason, _now(), o["opportunity_id"], email))
        # 2. someone a contact brought into a thread, who keeps appearing, but is not a contact yet
        for r in conn.execute("SELECT DISTINCT introducer, newcomer FROM rel_intros").fetchall():
            if r["newcomer"] in by_email or r["introducer"] not in by_email:
                continue
            n = together(r["introducer"], r["newcomer"])
            if n < MIN_THREADS:
                continue
            intro_name = conn.execute("SELECT name FROM people WHERE person_id = ?", (by_email[r["introducer"]],)).fetchone()["name"]
            who = names.get(r["newcomer"]) or r["newcomer"]
            cur = conn.execute("""INSERT OR IGNORE INTO rel_suggestions (kind, opportunity_id, email, with_email, introducer, threads, reason, created_at, updated_at)
                                  VALUES ('new_contact', 0, ?, ?, ?, ?, ?, ?, ?)""",
                               (r["newcomer"], r["introducer"], r["introducer"], n, f"{intro_name} brought {who} into a conversation, and they have been in {n} threads together within 3 months.", _now(), _now()))
            made += cur.rowcount
    return made


def update() -> dict:
    added = ingest()
    made = suggest()
    if added or made:
        log.info("relationship graph: %s new emails, %s new suggestions", added, made)
    return {"emails": added, "suggestions": made}


def update_quietly() -> None:
    """For hooks after mail is scanned or saved: a problem here never breaks the sync."""
    try:
        update()
    except Exception:
        log.exception("updating the relationship graph failed; it will run again after the next sync")


# ------------------------------------------------------------------ suggestions
def list_suggestions(visible_opportunity=lambda opp_id: True) -> list[dict]:
    with _crm() as conn:
        by_email = _contact_by_email(conn)
        names = {r["email"]: r["name"] for r in conn.execute("SELECT email, name FROM rel_people")}
        rows = conn.execute("""SELECT s.*, o.name AS opportunity FROM rel_suggestions s LEFT JOIN opportunities o ON o.opportunity_id = s.opportunity_id
                               WHERE s.status = 'open' ORDER BY s.threads DESC, s.updated_at DESC""").fetchall()
    out = []
    adding = {r["email"] for r in rows if r["kind"] == "add_to_opportunity" and r["opportunity"]}
    for r in rows:
        if r["kind"] == "add_to_opportunity" and (not r["opportunity"] or not visible_opportunity(r["opportunity_id"])):
            continue
        if r["kind"] == "new_contact" and (r["email"] in by_email or r["email"] in adding):
            continue                                   # already a contact, or adding them to an opportunity creates the contact anyway
        out.append({"suggestion_id": r["suggestion_id"], "kind": r["kind"], "opportunity_id": r["opportunity_id"] or None, "opportunity": r["opportunity"] or "",
                    "email": r["email"], "name": names.get(r["email"]) or r["email"], "contact_id": by_email.get(r["email"]),
                    "introducer_email": r["introducer"], "introducer_contact_id": by_email.get(r["introducer"]) if r["introducer"] else None,
                    "threads": r["threads"], "mentions": r["mentions"], "reason": r["reason"]})
    return out


def _get(conn, sid):
    r = conn.execute("SELECT * FROM rel_suggestions WHERE suggestion_id = ?", (sid,)).fetchone()
    if not r or r["status"] != "open":
        raise ValueError("That suggestion is no longer open.")
    return r


def dismiss(sid: int) -> None:
    with _crm() as conn:
        _get(conn, sid)
        conn.execute("UPDATE rel_suggestions SET status = 'dismissed', updated_at = ? WHERE suggestion_id = ?", (_now(), sid))


def accept(sid: int) -> dict:
    """Do what the suggestion says: create the contact if needed, and add them to the opportunity (with who introduced them)."""
    from . import contacts, opportunities
    with _crm() as conn:
        s = dict(_get(conn, sid))
        by_email = _contact_by_email(conn)
        name = (conn.execute("SELECT name FROM rel_people WHERE email = ?", (s["email"],)).fetchone() or {"name": ""})["name"]
    pid = by_email.get(s["email"])
    if not pid:
        created = contacts.create_contact({"name": name or s["email"].split("@")[0], "email": s["email"],
                                           "notes": "Added from LyteKeepr's relationship suggestions: " + s["reason"]}, require_brand=False)   # waits under "Needs a brand"
        pid = created["id"]
    result = {"contact_id": pid}
    if s["kind"] == "add_to_opportunity" and s["opportunity_id"]:
        intro = by_email.get(s["introducer"]) if s["introducer"] else None
        opportunities.add_participant(s["opportunity_id"], pid, "", False, intro if intro != pid else None)
        result["opportunity_id"] = s["opportunity_id"]
    with _crm() as conn:
        conn.execute("UPDATE rel_suggestions SET status = 'accepted', updated_at = ? WHERE suggestion_id = ?", (_now(), sid))
    return result


def register(app):
    from flask import jsonify, request
    from . import access, opportunities

    def visible(opp_id):
        return access.can_opportunity(opportunities.get(opp_id))

    @app.get("/api/crm/suggestions")
    def rel_suggestions():
        if not access.is_full():          # team members: only suggestions for opportunities they can see, no new-contact ones
            return jsonify({"suggestions": [x for x in list_suggestions(visible) if x["kind"] == "add_to_opportunity"
                                            and (not x.get("contact_id") or access.can_person(x["contact_id"]))]})
        return jsonify({"suggestions": list_suggestions()})

    @app.post("/api/crm/suggestions/<int:sid>/accept")
    def rel_accept(sid):
        s = next((x for x in list_suggestions(visible) if x["suggestion_id"] == sid), None)
        if not s or (s["kind"] == "new_contact" and not access.is_full()):
            return jsonify({"error": "That suggestion is no longer open."}), 404
        try:
            return jsonify(accept(sid))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/crm/suggestions/<int:sid>/dismiss")
    def rel_dismiss(sid):
        s = next((x for x in list_suggestions(visible) if x["suggestion_id"] == sid), None)
        if not s:
            return jsonify({"error": "That suggestion is no longer open."}), 404
        dismiss(sid)
        return jsonify({"ok": True})

    @app.get("/api/crm/people/<int:person_id>/connections")
    def rel_connections(person_id):
        if not access.can_person(person_id):
            return jsonify({"error": "You don't have access to that contact."}), 403
        allowed = access.person_ids()
        rows = connections(person_id)
        if allowed is not None:            # team members see only people they may see
            rows = [r for r in rows if r["contact_id"] and r["contact_id"] in allowed]
        return jsonify({"connections": rows})

    @app.post("/api/crm/relationships/refresh")
    def rel_refresh():
        if not access.is_full():
            return jsonify({"error": "Only the admin can refresh this."}), 403
        return jsonify(update())
