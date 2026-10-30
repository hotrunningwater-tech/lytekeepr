"""Email lookups (Claude and the phone), on the emails saved in the LyteKeepr tab only (filed by the admin, or from contacts automatically).
Who sees what (the same rules as everywhere): the admin sees all of them; a team member only if the admin switched on "Sees my emails"
for them (Settings → Team), and then only emails with contacts they may see (their brands and funnels).

1. lookup: quick, from the saved index (who, when, subject, preview); word search inside the full text only when asked.
2. latest: one contact's last email from them and last email from you, in full.
3. thread: a whole conversation, every email in full (for deep reading or summaries).
"""

import html
import re

MAX_BODY = 8000


def _accounts() -> list:
    from gmail import storage
    return storage.list_account_ids()


def _me() -> set:
    return {a.lower() for a in _accounts()}


def _addr(x) -> str:
    return ((x or {}).get("email") or "").strip().lower() if isinstance(x, dict) else str(x or "").strip().lower()


def _body(full: dict) -> str:
    text = (full.get("textBody") or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() and full.get("htmlBody"):
        from gmail.parser import html_to_text
        text = html_to_text(full["htmlBody"])
    text = re.sub(r"\n[ \t]+\n", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_BODY] + ("\n… (cut)" if len(text) > MAX_BODY else "")


def contact_addresses(contact) -> tuple:
    """(name, {email addresses}) for a contact id, a contact name, or an email address."""
    from . import contacts, access
    c = str(contact or "").strip()
    if "@" in c and " " not in c:
        return c, {c.lower()}
    if c.isdigit():
        if not access.can_person(int(c)):
            raise ValueError("You don't have access to that contact.")
        found = [contacts.get_contact(int(c))]
    else:                                             # by name (or company): names and addresses only, never notes; two people = ask
        from assistant.contact_lookup import find_people
        ids = list(dict.fromkeys(m["person_id"] for m in find_people(c, email_only=True) if access.can_person(m["person_id"])))
        if len(ids) > 1:
            who = [contacts.get_contact(i) or {} for i in ids[:8]]
            raise ValueError(f"More than one contact matches '{c}': " + "; ".join(f"{w.get('display')} ({w.get('email')})" for w in who)
                             + ". Ask the user which one (or use the email address).")
        found = [contacts.get_contact(ids[0])] if ids else []
    found = [x for x in found if x]
    if not found:
        raise ValueError(f"No contact with an email address matching '{c}'.")
    p = found[0]
    with contacts._crm() as conn:
        addrs = {r[0].strip().lower() for r in conn.execute(
            "SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email' AND channel_identifier LIKE '%@%'", (p["id"],))}
    if not addrs:
        raise ValueError(f"{p['display']} has no email address saved.")
    return p["display"], addrs


def allowed_addresses():
    """None = every saved email (the admin); else the email addresses of the contacts this team member may see. Refused without "Sees my emails"."""
    from flask import g
    from . import access, users, contacts
    if users.is_admin():
        return None
    if not (getattr(g, "user", None) or {}).get("share_emails"):
        raise PermissionError("The admin hasn't shared their emails with you (Settings → Team → Sees my emails).")
    ids = access.person_ids()
    if ids is None:
        return None if (getattr(g, "user", None) or {}).get("access") == "all" and not access.brand_scopes() else _addresses_of(contacts, None)
    return _addresses_of(contacts, ids)


def _addresses_of(contacts, ids):
    with contacts._crm() as conn:
        rows = conn.execute("SELECT person_id, channel_identifier FROM channel_accounts WHERE channel = 'email' AND channel_identifier LIKE '%@%'").fetchall()
    return {r[1].strip().lower() for r in rows if ids is None or r[0] in ids}


def _entries():
    """The saved emails this person may see; for the admin also the Primary-inbox emails from people who are not contacts yet
    (waiting for review in the Gmail tab), so a search finds a new prospect's email too (marked not_saved)."""
    from gmail import storage
    allowed = allowed_addresses()
    for account in _accounts():
        index = storage.load_index(account)
        for mid, e in index.items():
            if allowed is not None and _addr(e.get("from")) not in allowed and not ({_addr(t) for t in (e.get("to") or [])} & allowed):
                continue                                  # an email with nobody this person may see
            yield account, mid, e
        if allowed is None:
            for mid in storage.get_pending_review_ids(account):
                if mid in index:
                    continue
                m = storage.load_pending(account, mid)
                if m:
                    yield account, mid, {"from": m.get("from"), "to": m.get("to") or [], "subject": m.get("subject"), "snippet": m.get("snippet"),
                                         "receivedAt": m.get("receivedAt"), "threadId": m.get("threadId"), "not_saved": True}


def _load(account, mid) -> dict:
    """A saved email, or one still waiting for review."""
    from gmail import storage
    return storage.load_message(account, mid) or storage.load_pending(account, mid) or {}


def _row(account, mid, e, me) -> dict:
    sender = _addr(e.get("from"))
    return {"email_id": mid, "thread_id": e.get("threadId"), "when": e.get("receivedAt"),
            "from": ((e.get("from") or {}).get("name") or sender) if isinstance(e.get("from"), dict) else sender, "from_email": sender,
            "to": [_addr(t) for t in (e.get("to") or [])], "subject": e.get("subject") or "(no subject)",
            "preview": html.unescape(e.get("snippet") or ""), "direction": "from you" if sender in me else "to you",
            **({"not_saved": True} if e.get("not_saved") else {})}


def lookup(contact=None, query=None, since=None, in_body: bool = False, limit: int = 20) -> dict:
    from gmail import storage
    me = _me()
    name, addrs = contact_addresses(contact) if contact else (None, None)
    words = [w for w in str(query or "").lower().split() if w]
    out = []
    for account, mid, e in _entries():
        sender, recipients = _addr(e.get("from")), {_addr(t) for t in (e.get("to") or [])}
        if addrs and sender not in addrs and not (recipients & addrs):
            continue
        if since and (e.get("receivedAt") or "") < str(since):
            continue
        if words:
            hay = " ".join([e.get("subject") or "", html.unescape(e.get("snippet") or ""), str(e.get("from") or "")]).lower()
            if not all(w in hay for w in words):
                if not in_body:
                    continue
                full = _load(account, mid)
                if not all(w in (hay + " " + _body(full).lower()) for w in words):
                    continue
        out.append(_row(account, mid, e, me))
    out.sort(key=lambda r: r["when"] or "", reverse=True)
    return {"contact": name, "count": len(out), "emails": out[: max(1, min(int(limit or 20), 300))]}


def latest(contact) -> dict:
    """The last email from them and the last one from you to them, in full."""
    from gmail import storage
    me = _me()
    name, addrs = contact_addresses(contact)
    theirs = mine = None
    for account, mid, e in _entries():
        sender, recipients = _addr(e.get("from")), {_addr(t) for t in (e.get("to") or [])}
        when = e.get("receivedAt") or ""
        if sender in addrs and (not theirs or when > theirs[2]):
            theirs = (account, mid, when, e)
        elif sender in me and recipients & addrs and (not mine or when > mine[2]):
            mine = (account, mid, when, e)
    def full(x):
        if not x:
            return None
        return {**_row(x[0], x[1], x[3], me), "text": _body(_load(x[0], x[1]))}
    t, m = full(theirs), full(mine)
    replied = bool(t and m and t["when"] > m["when"])
    return {"contact": name, "last_from_them": t, "last_from_you": m,
            "they_replied_after_your_last_email": replied if (t and m) else None}


def thread(email_id=None, thread_id=None) -> dict:
    """Every saved email of one conversation, oldest first, in full."""
    from gmail import storage
    me = _me()
    tid = thread_id
    if not tid and email_id:
        tid = next((e.get("threadId") for _, mid, e in _entries() if mid == email_id), None)
    if not tid:
        raise ValueError("No such email or thread among the saved emails.")
    rows = [{**_row(a, mid, e, me), "text": _body(_load(a, mid))} for a, mid, e in _entries() if e.get("threadId") == tid]
    rows.sort(key=lambda r: r["when"] or "")
    return {"thread_id": tid, "subject": rows[0]["subject"] if rows else "", "count": len(rows), "emails": rows}


def message(email_id) -> dict:
    """One saved email in full (if this person may see it)."""
    from gmail import storage
    me = _me()
    for a, mid, e in _entries():
        if mid == email_id:
            return {**_row(a, mid, e, me), "text": _body(_load(a, mid))}
    raise ValueError("No such email among the ones you can see.")


def reply(text: str, email_id=None, contact=None, preview: bool = False) -> dict:
    """Reply in the thread of one saved email (or of the contact's latest email): Re: subject, to its sender (or, for your own email,
    to its recipients), sent from the admin's Gmail and saved on the contact like any sent email. The admin only."""
    from . import users
    if not users.is_admin():
        raise PermissionError("Replies go out from the admin's Gmail; only the admin can send them.")
    if not (text or "").strip():
        raise ValueError("Write the reply first.")
    target = None
    if email_id:
        target = next(((a, mid, e) for a, mid, e in _entries() if mid == email_id), None)
    elif contact:
        found = latest(contact)
        last = max([x for x in (found["last_from_them"], found["last_from_you"]) if x], key=lambda x: x["when"] or "", default=None)
        if last:
            target = next(((a, mid, e) for a, mid, e in _entries() if mid == last["email_id"]), None)
    if not target:
        raise ValueError("No such email among the saved ones to reply to.")
    account, mid, e = target
    from gmail.sync import get_active_account_id
    sending = (get_active_account_id() or "").lower()
    me = {sending} if sending else _me()              # who to answer: only the mailbox sending now is "you" (an old mailbox is a normal address)
    sender = _addr(e.get("from"))
    to = [sender] if sender not in me else [t for t in (_addr(x) for x in e.get("to") or []) if t and t not in me]
    if not to:
        raise ValueError("Couldn't tell who to reply to.")
    from mailserver import own as own_mail
    if own_mail.is_enabled():                           # own mail server: the Message-ID was kept when the email was synced
        message_id = own_mail.message_id_header(mid)
    else:
        from gmail import client as gmail_client
        meta = gmail_client.get_message(mid)
        headers = {h.get("name", "").lower(): h.get("value", "") for h in (meta.get("payload", {}).get("headers") or [])}
        message_id = headers.get("message-id")
    subject = e.get("subject") or ""
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if preview:                                       # exactly what would go out, worked out here (not by Claude): shown before any yes
        from . import contacts
        with contacts._crm() as conn:
            def who(addr):
                r = conn.execute("""SELECT p.name FROM channel_accounts a JOIN people p ON p.person_id = a.person_id
                                    WHERE a.channel = 'email' AND lower(a.channel_identifier) = ? LIMIT 1""", (addr,)).fetchone()
                return (r[0] if r and r[0] else "") or ""
            to_names = [who(t) for t in to]
        return {"to": to, "to_names": to_names, "cc": [], "bcc": [], "subject": subject, "body": text.strip(), "email_id": mid,
                "in_reply_to": {"from": _addr(e.get("from")), "from_name": (e.get("from") or {}).get("name") or "", "date": e.get("receivedAt") or "",
                                "subject": e.get("subject") or ""}}
    from assistant import email_send_flow
    name = ((e.get("from") or {}).get("name") if sender not in me else "") or to[0]
    result = email_send_flow.send_email(None, name, ", ".join(to), subject, text.strip(), thread=(e.get("threadId"), message_id) if message_id else None)
    return {**result, "to": to, "subject": subject, "in_thread": bool(message_id)}


def register(app):
    from flask import jsonify, request

    @app.post("/api/crm/email/reply")
    def email_reply_route():
        b = request.get_json(silent=True) or {}
        return run(reply, b.get("text", ""), b.get("email_id"), b.get("contact"), bool(b.get("preview")))

    def run(fn, *a):
        try:
            return jsonify(fn(*a))
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/crm/email/message")
    def email_message_route():
        return run(message, request.args.get("email_id"))

    @app.get("/api/crm/email/lookup")
    def email_lookup_route():
        a = request.args
        return run(lookup, a.get("contact"), a.get("q"), a.get("since"), a.get("in_body") == "1", a.get("limit") or 20)

    @app.get("/api/crm/email/latest")
    def email_latest_route():
        return run(latest, request.args.get("contact"))

    @app.get("/api/crm/email/thread")
    def email_thread_route():
        return run(thread, request.args.get("email_id"), request.args.get("thread_id"))
