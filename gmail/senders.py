"""The emails waiting for review, grouped by sender, so they are sorted one decision per sender instead of one per email.

Each sender lands in one group, with one example email:
  written   - the user has written to them at least once (asked of the mailbox itself, all of it, not only what was synced)
  automatic - no-reply addresses, mailing lists, receipts and notifications
  selling   - sales wording ("book a demo", "limited offer", ...)
  never     - the user has never written to them
  unknown   - the mailbox could not be asked; nothing is guessed
Claude suggests keep or remove per group (connector: email_inbox, sort_email_inbox); nothing changes until the user says yes.
Kept senders become contacts with all their waiting emails, and later emails go straight onto them. Removed senders join the CRM's
excluded_contacts, so their later emails skip review; their waiting emails stay 30 days so they can be brought back.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from . import storage

KEEP_DAYS = 30
REMOVED_REASON = "You removed this sender."
AUTOMATIC_ADDRESS = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon|postmaster|bounces?|newsletters?|"
                               r"digest|updates?|billing|invoices?|receipts?|orders?|marketing|news)([+._-].*)?@", re.I)
AUTOMATIC_WORDS = ("unsubscribe", "view in browser", "manage your preferences", "email preferences", "you are receiving this")
SELLING_WORDS = ("book a demo", "book a call", "quick call", "15 minutes", "15-minute", "limited time", "limited offer", "special offer", "% off",
                 "discount", "free trial", "webinar", "partnership opportunity", "grow your", "boost your", "increase your", "scale your",
                 "more leads", "more customers", "would you be open", "are you the right person", "pricing", "promo code")
GROUPS = {
    "written": ("You've written to them", "keep"),
    "automatic": ("Automatic: no-reply, mailing lists, receipts, notifications", "remove"),
    "selling": ("Selling something", "remove"),
    "never": ("You've never written to them", "check"),
    "unknown": ("Couldn't check whether you've written to them", "check"),
}


def _memory_path(account_id: str):
    return storage.account_dir(account_id) / "senders.json"


def _memory(account_id: str) -> dict:
    try:
        return json.loads(_memory_path(account_id).read_text())
    except (OSError, ValueError):
        return {}


def _save_memory(account_id: str, memory: dict) -> None:
    _memory_path(account_id).write_text(json.dumps(memory, indent=1))


def _text(message: dict) -> str:
    return f"{message.get('subject') or ''}\n{(message.get('textBody') or message.get('snippet') or '')[:3000]}".lower()


def _group(address: str, messages: list[dict], wrote) -> str:
    if wrote is True:
        return "written"
    if AUTOMATIC_ADDRESS.match(address) or any(m.get("isBulk") for m in messages) or any(w in _text(m) for m in messages for w in AUTOMATIC_WORDS):
        return "automatic"
    if any(w in _text(m) for m in messages for w in SELLING_WORDS):
        return "selling"
    return "never" if wrote is False else "unknown"


def waiting_by_sender(account_id: str, contact_of) -> dict:
    """{address: {"name", "messages": [...]}} for every email waiting for review. contact_of(message) -> (address, name, direction)
    or None (server._email_contact_info)."""
    scan_state = storage.load_scan_state(account_id)
    out: dict = {}
    for mid, entry in scan_state.items():
        if entry.get("status") != "pending_review":
            continue
        message = storage.load_pending(account_id, mid)
        info = contact_of(message) if message else None
        if not info:
            continue
        address, name, direction = info
        row = out.setdefault(address.lower(), {"address": address, "name": name, "messages": [], "outgoing": False})
        row["messages"].append(message)
        row["outgoing"] = row["outgoing"] or direction == "outgoing"
    return out


def groups(account_id: str, contact_of, wrote_to) -> list[dict]:
    """The waiting senders in their groups, each with its count and one example email, newest first.
    wrote_to(address) -> True / False / None (the mailbox could not be asked); a clear answer is remembered."""
    forget_old_removals(account_id)
    memory = _memory(account_id)
    asked = memory.setdefault("wrote_to", {})
    by_group: dict = {k: [] for k in GROUPS}
    for key, row in waiting_by_sender(account_id, contact_of).items():
        if row["outgoing"]:
            wrote = True
        elif key in asked:
            wrote = asked[key]
        else:
            wrote = wrote_to(row["address"])
            if wrote is not None:
                asked[key] = wrote
        newest = max(row["messages"], key=lambda m: m.get("receivedAt") or "")
        by_group[_group(row["address"], row["messages"], wrote)].append({
            "sender": row["address"], "name": row["name"], "emails": len(row["messages"]), "latest": (newest.get("receivedAt") or "")[:10],
            "example": {"subject": newest.get("subject") or "(no subject)", "excerpt": re.sub(r"\s+", " ", newest.get("textBody") or newest.get("snippet") or "")[:300]}})
    _save_memory(account_id, memory)
    return [{"group": k, "title": GROUPS[k][0], "suggested": GROUPS[k][1], "senders": sorted(v, key=lambda s: s["latest"], reverse=True)}
            for k, v in by_group.items() if v]


def ids_from(account_id: str, contact_of, addresses: list[str]) -> dict:
    """{address: [waiting email ids]} for the given senders (any capitals)."""
    wanted = {a.strip().lower() for a in addresses or [] if a and a.strip()}
    found: dict = {}
    for key, row in waiting_by_sender(account_id, contact_of).items():
        if key in wanted:
            found[row["address"]] = [m["id"] for m in row["messages"]]
    return found


def remove(account_id: str, address: str, ids: list[str], conn) -> None:
    """The sender's later emails skip review (excluded_contacts); their waiting emails are set aside, kept 30 days to bring back."""
    now = datetime.now(timezone.utc).isoformat()
    for ident in {address, address.lower()}:
        conn.execute("INSERT OR REPLACE INTO excluded_contacts (channel, channel_identifier, excluded_at) VALUES ('email', ?, ?)", (ident, now))
    scan_state = storage.load_scan_state(account_id)
    for mid in ids:
        if mid in scan_state:
            scan_state[mid] = {**scan_state[mid], "status": "dismissed", "reason": REMOVED_REASON, "removedAt": now}
    storage.save_scan_state(account_id, scan_state)
    memory = _memory(account_id)
    memory.setdefault("removed", {})[address.lower()] = now
    _save_memory(account_id, memory)


def bring_back(account_id: str, address: str, conn) -> int:
    """Undo a removal within 30 days: later emails come in again and the set-aside ones wait for review again."""
    key = address.strip().lower()
    memory = _memory(account_id)
    if key not in memory.get("removed", {}):
        return -1
    conn.execute("DELETE FROM excluded_contacts WHERE channel = 'email' AND lower(channel_identifier) = ?", (key,))
    scan_state = storage.load_scan_state(account_id)
    back = 0
    for mid, entry in scan_state.items():
        if entry.get("reason") == REMOVED_REASON and (entry.get("senderEmail") or "").lower() == key and storage.load_pending(account_id, mid):
            scan_state[mid] = {**entry, "status": "pending_review", "reason": "", "removedAt": ""}
            back += 1
    storage.save_scan_state(account_id, scan_state)
    memory["removed"].pop(key, None)
    _save_memory(account_id, memory)
    return back


def removed(account_id: str) -> list[str]:
    return sorted(_memory(account_id).get("removed", {}))


def forget_old_removals(account_id: str) -> None:
    """After 30 days a removal is final: the set-aside emails are deleted."""
    memory = _memory(account_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    old = {k for k, at in memory.get("removed", {}).items() if at < cutoff}
    if not old:
        return
    scan_state = storage.load_scan_state(account_id)
    for mid, entry in scan_state.items():
        if entry.get("reason") == REMOVED_REASON and (entry.get("removedAt") or "") < cutoff:
            storage.delete_pending(account_id, mid)
    for k in old:
        memory["removed"].pop(k, None)
    _save_memory(account_id, memory)
