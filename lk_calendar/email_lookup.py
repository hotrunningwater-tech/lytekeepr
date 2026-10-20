"""Email lookups for Claude, on the emails saved in the LyteKeepr tab only (filed by you, or from contacts automatically). Admin only.

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
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>", " ", full["htmlBody"])))
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
    else:
        found = [x for x in contacts.list_contacts(c, limit=5) if access.can_person(x["id"])]
    found = [x for x in found if x]
    if not found:
        raise ValueError(f"No contact matching '{c}'.")
    p = found[0]
    with contacts._crm() as conn:
        addrs = {r[0].strip().lower() for r in conn.execute(
            "SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email' AND channel_identifier LIKE '%@%'", (p["id"],))}
    if not addrs:
        raise ValueError(f"{p['display']} has no email address saved.")
    return p["display"], addrs


def _entries():
    from gmail import storage
    for account in _accounts():
        for mid, e in storage.load_index(account).items():
            yield account, mid, e


def _row(account, mid, e, me) -> dict:
    sender = _addr(e.get("from"))
    return {"email_id": mid, "thread_id": e.get("threadId"), "when": e.get("receivedAt"),
            "from": ((e.get("from") or {}).get("name") or sender) if isinstance(e.get("from"), dict) else sender, "from_email": sender,
            "to": [_addr(t) for t in (e.get("to") or [])], "subject": e.get("subject") or "(no subject)",
            "preview": html.unescape(e.get("snippet") or ""), "direction": "from you" if sender in me else "to you"}


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
                full = storage.load_message(account, mid) or {}
                if not all(w in (hay + " " + _body(full).lower()) for w in words):
                    continue
        out.append(_row(account, mid, e, me))
    out.sort(key=lambda r: r["when"] or "", reverse=True)
    return {"contact": name, "count": len(out), "emails": out[: max(1, min(int(limit or 20), 100))]}


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
        return {**_row(x[0], x[1], x[3], me), "text": _body(storage.load_message(x[0], x[1]) or {})}
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
    rows = [{**_row(a, mid, e, me), "text": _body(storage.load_message(a, mid) or {})} for a, mid, e in _entries() if e.get("threadId") == tid]
    rows.sort(key=lambda r: r["when"] or "")
    return {"thread_id": tid, "subject": rows[0]["subject"] if rows else "", "count": len(rows), "emails": rows}


def register(app):
    from flask import jsonify, request
    from . import users

    def run(fn, *a):
        if not users.is_admin():
            return jsonify({"error": "Emails are the admin's mailbox; not available to team members."}), 403
        try:
            return jsonify(fn(*a))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/email/lookup")
    def email_lookup_route():
        a = request.args
        return run(lookup, a.get("contact"), a.get("q"), a.get("since"), a.get("in_body") == "1", a.get("limit") or 20)

    @app.get("/api/email/latest")
    def email_latest_route():
        return run(latest, request.args.get("contact"))

    @app.get("/api/email/thread")
    def email_thread_route():
        return run(thread, request.args.get("email_id"), request.args.get("thread_id"))
