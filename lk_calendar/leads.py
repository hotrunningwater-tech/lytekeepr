"""Lead lists (CSV in, SQLite behind the scenes), statuses, and the reminder rules.

Statuses are labels; their ORDER does not matter, any status can be set at any time.
Each channel has its own built-in set, and the user can add custom labels (no reminder attached):
  instagram: new, dm sent, customer replied, customer not interested
  linkedin:  new, connection request sent, connection accepted, message sent, replied
Reminders (one auto reminder per lead): sent / message sent -> "Follow up" 3 days later;
accepted -> "Send DM" the next day; replied / done -> reminder closed; custom -> reminder left as it is.
"""

import csv
import io
import logging
import re
import secrets
from datetime import date, datetime, timedelta

from . import store

FOLLOW_UP_DAYS = 3
FOLLOW_UP_TIME = store.UNTIMED_TIME       # follow-ups have no clock time: stored at 08:00 and flagged untimed
WARM_REPLY_DAYS = 1       # a reply creates a "Reply to ..." task this many days after the reply
ACCEPTED_DM_DAYS = 1      # connection accepted -> 'Send DM' reminder this many days later

# Internal keys are stored in the database; LABELS are what the user sees and says.
BUILTIN = {
    "instagram": ("new", "sent", "replied", "done"),
    "linkedin": ("new", "sent", "accepted", "message_sent", "replied"),
    "email": ("new", "sent", "replied", "bounced", "done"),
}
LABELS = {
    "instagram": {"new": "new", "sent": "dm sent", "replied": "customer replied", "done": "customer not interested"},
    "linkedin": {"new": "new", "sent": "connection request sent", "accepted": "connection accepted",
                 "message_sent": "message sent", "replied": "replied"},
    "email": {"new": "new", "sent": "email sent", "replied": "replied", "bounced": "bounced", "done": "not interested"},
}
_EXTRA_ALIASES = {
    "instagram": {"sent": "sent", "dm": "sent", "messaged": "sent", "contacted": "sent", "message sent": "sent",
                  "replied": "replied", "reply": "replied", "responded": "replied",
                  "done": "done", "closed": "done", "not interested": "done", "lost": "done",
                  "lead": "new", "not contacted": "new", "to contact": "new", "": "new"},
    "linkedin": {"sent": "sent", "request sent": "sent", "connection sent": "sent", "connection request": "sent",
                 "accepted": "accepted", "connected": "accepted", "accepted connection": "accepted",
                 "dm sent": "message_sent", "messaged": "message_sent", "message": "message_sent", "dm": "message_sent",
                 "reply": "replied", "responded": "replied", "customer replied": "replied",
                 "lead": "new", "not contacted": "new", "to contact": "new", "": "new"},
    "email": {"sent": "sent", "emailed": "sent", "contacted": "sent", "reply": "replied", "responded": "replied", "bounce": "bounced",
              "wrong address": "bounced", "done": "done", "closed": "done", "lost": "done", "lead": "new", "not contacted": "new", "to contact": "new", "": "new"},
}
CHANNELS = {
    "linkedin": {"project": "lytekeepr", "label": "LinkedIn"},
    "instagram": {"project": "aspan", "label": "Instagram"},
    "email": {"project": "nexy", "label": "Email"},
}
MAX_ROWS = 5000

CONTACT_KEYS = ("contact", "name", "full name", "fullname", "username", "handle", "profile name", "business name", "commenter")
STATUS_KEYS = ("status", "stage", "outreach status")
DATE_KEYS = ("date", "status date", "sent date", "date sent", "last contacted")
FOLLOW_UP_KEYS = ("follow up date", "followup date", "follow up", "next follow up")
URL_KEYS = ("url", "profile url", "profile", "link", "linkedin url", "instagram url", "instagram", "commenter url")
NOTE_KEYS = ("notes", "note", "comment", "comments")
INSTAGRAM_KEYS = ("instagram", "instagram url", "username", "handle", "profile url", "profileurl", "url")
FALSY = {"", "0", "no", "n", "false", "none", "-"}
def _norm(key: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (key or "").strip().lower())


def _aliases(channel: str) -> dict:
    out = {_norm(label): key for key, label in LABELS[channel].items()}
    out.update({_norm(k): v for k, v in _EXTRA_ALIASES[channel].items()})
    return out


def custom_labels(channel: str, conn=None) -> list[str]:
    own = conn is None
    conn = conn or store._conn()
    try:
        return [r["label"] for r in conn.execute("SELECT label FROM custom_statuses WHERE channel = ? ORDER BY label", (channel,))]
    finally:
        if own:
            conn.close()


def valid_labels(channel: str, conn=None) -> list[str]:
    return [LABELS[channel][k] for k in BUILTIN[channel]] + custom_labels(channel, conn)


def label_for(channel: str, key: str) -> str:
    return LABELS.get(channel, {}).get(key, key)


def resolve_status(channel: str, text: str, conn=None) -> str:
    """User wording ('dm sent', 'Connection accepted', a custom label...) -> the stored key."""
    if channel not in CHANNELS:
        raise store.ValidationError("channel must be linkedin, instagram or email")
    n = _norm(text)
    key = _aliases(channel).get(n)
    if key:
        return key
    if n in {_norm(c) for c in custom_labels(channel, conn)}:
        return n
    raise store.ValidationError(
        f'"{text}" is not a {CHANNELS[channel]["label"]} status. Statuses: ' + ", ".join(valid_labels(channel, conn)) + ".")


def add_custom_status(label: str, channel: str | None = None) -> dict:
    n = _norm(label)
    if not n or len(n) > 40:
        raise store.ValidationError("a status name must be 1 to 40 characters")
    channels = [channel] if channel else list(CHANNELS)
    for ch in channels:
        if ch not in CHANNELS:
            raise store.ValidationError("channel must be linkedin, instagram or email")
        if n in _aliases(ch):
            raise store.ValidationError(f'"{n}" already exists as a {CHANNELS[ch]["label"]} status')
    with store._conn() as conn:
        for ch in channels:
            conn.execute("INSERT OR IGNORE INTO custom_statuses (channel, label) VALUES (?, ?)", (ch, n))
    return {"added": n, "for": channels}


def _instagram_handle(text: str) -> str:
    m = re.search(r"instagram\.com/([^/?#\s]+)", text or "", re.I)
    handle = (m.group(1) if m else (text or "").strip()).lstrip("@")
    return handle if re.fullmatch(r"[A-Za-z0-9._]{1,40}", handle) else ""


def _contact_for(channel: str, row: dict) -> str:
    if channel == "instagram":
        handle = _instagram_handle(_pick(row, INSTAGRAM_KEYS))
        if handle:
            return "@" + handle
    return _pick(row, CONTACT_KEYS)


def _auto_notes(channel: str, row: dict) -> str:
    # A short human line shown on the lead card when the file has no notes column.
    if channel == "instagram":
        parts = [_pick(row, ("business name",)), _pick(row, ("category",)), _pick(row, ("country",))]
        followers = _pick(row, ("followers", "follower count"))
        if followers:
            parts.append(f"{followers} followers")
    else:
        parts = [_pick(row, ("headline",)), _pick(row, ("company",))]
    return " · ".join(p for p in parts if p)[:300]


def _infer_status(row: dict, has_date: bool) -> str:
    # No status column (e.g. the LinkedIn weekly file): work it out from what is filled in.
    if _pick(row, ("replied",)).lower() not in FALSY:
        return "replied"
    if _pick(row, ("accepted",)).lower() not in FALSY:
        return "accepted"
    return "sent" if has_date else "new"


def _pick(row: dict, keys) -> str:
    for k in keys:
        if k in row and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def _parse_date(text: str, fallback: str) -> str:
    if not text:
        return fallback
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10] if fmt in ("%Y-%m-%d", "%Y/%m/%d") else text, fmt).date().isoformat()
        except ValueError:
            continue
    raise store.ValidationError(f'unrecognised date "{text}" (use YYYY-MM-DD)')


def _reminder_fields(lead: dict, bucket: str = "cold", conn=None) -> dict:
    """The one auto task a lead gets: 'Reply to' when warm, 'Send DM' once accepted, otherwise 'Follow up'. No clock time."""
    status_day = date.fromisoformat(lead["status_date"])
    custom = date.fromisoformat(lead["follow_up_date"]) if lead.get("follow_up_date") else None
    if bucket == "task":
        row = conn.execute("SELECT title FROM status_tasks WHERE channel = ? AND status_key = ?", (lead["channel"], lead["status"])).fetchone() if conn else None
        title, due = f"{row['title'] if row else 'Check'}: {lead['contact']}", custom or status_day + timedelta(days=WARM_REPLY_DAYS)
    elif bucket == "warm":
        title, due = f"Reply to {lead['contact']}", custom or status_day + timedelta(days=WARM_REPLY_DAYS)
    elif lead["status"] == "accepted":
        title, due = f"Send DM: {lead['contact']}", custom or status_day + timedelta(days=ACCEPTED_DM_DAYS)
    else:
        title, due = f"Follow up: {lead['contact']}", custom or status_day + timedelta(days=FOLLOW_UP_DAYS)
    return {
        "title": title,
        "project": lead["project"],
        "kind": "reminder",
        "start_date": due.isoformat(),
        "start_time": FOLLOW_UP_TIME,
        "duration_min": 15,
        "untimed": 1,
        "source": "auto",
        "lead_ref": f"lead:{lead['id']}",
    }


def _insert_event(conn, fields: dict) -> None:
    conn.execute(
        """INSERT INTO events (title, project, kind, start_date, start_time, duration_min,
           repeat, repeat_days, notes, source, lead_ref, created_at, untimed)
           VALUES (:title, :project, :kind, :start_date, :start_time, :duration_min,
           :repeat, :repeat_days, :notes, :source, :lead_ref, :created_at, :untimed)""",
        {**fields, "created_at": store.now_local().isoformat()},
    )


def apply_rules(conn, lead: dict) -> str | None:
    """Make the calendar match the lead's status bucket. Returns 'created'/'moved'/'closed'/'removed' or None.
    cold -> follow-up / send-DM task (grouped on screen); warm -> own 'Reply to' task; closed -> task completed;
    none -> nothing (a brand-new lead has no task; custom labels leave things as they are)."""
    ref = f"lead:{lead['id']}"
    existing = conn.execute("SELECT * FROM events WHERE lead_ref = ? AND source = 'auto'", (ref,)).fetchone()
    bucket = store.bucket_for(conn, lead["channel"], lead["status"])
    if bucket in ("cold", "warm", "task"):
        fields = store.clean(_reminder_fields(lead, bucket, conn))
        if not existing:
            _insert_event(conn, fields)
            return "created"
        conn.execute("DELETE FROM completions WHERE event_id = ?", (existing["id"],))
        if existing["start_date"] != fields["start_date"] or existing["title"] != fields["title"] or existing["untimed"] != 1:
            conn.execute("UPDATE events SET start_date = ?, start_time = ?, untimed = 1, title = ? WHERE id = ?",
                         (fields["start_date"], fields["start_time"], fields["title"], existing["id"]))
            return "moved"
        return None
    if existing and bucket == "closed":
        conn.execute("INSERT OR IGNORE INTO completions VALUES (?, ?)", (existing["id"], existing["start_date"]))
        return "closed"
    if existing and (lead["status"] == "new" or (bucket == "none" and lead["status"] in store.LATER_STATUSES)):
        # back to untouched, or moved on to a stage with no task of its own (call booked, confirmed, onboarding): the old task goes
        conn.execute("DELETE FROM events WHERE id = ?", (existing["id"],))
        conn.execute("DELETE FROM completions WHERE event_id = ?", (existing["id"],))
        return "removed"
    return None


def import_csv(channel: str, raw: bytes) -> dict:
    if channel not in CHANNELS:
        raise store.ValidationError("channel must be linkedin, instagram or email")
    if len(raw) > 5 * 1024 * 1024:
        raise store.ValidationError("file is larger than 5 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise store.ValidationError("the file is empty")
    project = CHANNELS[channel]["project"]
    today = store.now_local().date().isoformat()
    result = {"added": 0, "updated": 0, "unchanged": 0, "reminders_created": 0,
              "reminders_moved": 0, "reminders_closed": 0, "errors": []}

    with store._conn() as conn:
        for n, raw_row in enumerate(reader, start=2):
            if n - 1 > MAX_ROWS:
                result["errors"].append(f"stopped after {MAX_ROWS} rows")
                break
            row = {_norm(k): (v or "") for k, v in raw_row.items() if k}
            contact = _contact_for(channel, row)
            if not contact:
                if any(str(v).strip() for v in row.values()):
                    result["errors"].append(f"row {n}: no contact")
                continue
            try:
                status_text = _pick(row, STATUS_KEYS)
                date_text = _pick(row, DATE_KEYS)
                if status_text:
                    status = resolve_status(channel, status_text, conn)
                else:
                    status = _infer_status(row, bool(date_text))
                status_date = _parse_date(date_text or _pick(row, ("scraped at",)), today)
                follow_up = _parse_date(_pick(row, FOLLOW_UP_KEYS), "") if _pick(row, FOLLOW_UP_KEYS) else ""
            except store.ValidationError as err:
                result["errors"].append(f"row {n} ({contact}): {err}")
                continue
            url = _pick(row, URL_KEYS)
            notes = _pick(row, NOTE_KEYS) or _auto_notes(channel, row)
            if channel == "instagram" and url and "instagram.com" not in url:
                url = ""

            current = conn.execute("SELECT * FROM leads WHERE channel = ? AND contact = ?", (channel, contact)).fetchone()
            if current is None:
                cur = conn.execute(
                    """INSERT INTO leads (channel, project, contact, status, status_date, follow_up_date, url, notes, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (channel, project, contact, status, status_date, follow_up, url, notes, store.now_local().isoformat()))
                lead = dict(conn.execute("SELECT * FROM leads WHERE id = ?", (cur.lastrowid,)).fetchone())
                result["added"] += 1
                _after_change(conn, lead)
                outcome = apply_rules(conn, lead)
            else:
                # Placeholder until re-import is designed: an existing lead's status and reminders are
                # never changed by an import; only a missing link or note is filled in.
                lead = dict(current)
                changed = False
                if url and url != lead["url"]:
                    lead["url"], changed = url, True
                if notes and notes != lead["notes"]:
                    lead["notes"], changed = notes, True
                if not changed:
                    result["unchanged"] += 1
                    continue
                conn.execute("UPDATE leads SET url=?, notes=?, updated_at=? WHERE id=?",
                             (lead["url"], lead["notes"], store.now_local().isoformat(), lead["id"]))
                result["updated"] += 1
                outcome = None
            key = {"created": "reminders_created", "moved": "reminders_moved", "closed": "reminders_closed"}.get(outcome)
            if key:
                result[key] += 1
    return result


def _after_change(conn, lead: dict) -> None:
    """Keep the CRM in step: the lead belongs to a person, and a person in a funnel moves to the stage of their furthest status."""
    try:
        from . import contacts
        person_id = contacts.link_lead(conn, lead)
        if person_id:
            from . import brands
            brands.file_lead(conn, lead, person_id)      # its tasks are filed under the contact's brand
            contacts.sync_stage(conn, person_id)
    except Exception:
        logging.getLogger(__name__).exception("could not update the CRM for lead %s", lead.get("id"))


def _log(conn, batch: str, lead: dict, new_status: str, kind: str, old_event_date: str = "") -> None:
    conn.execute("""INSERT INTO lead_history (batch, lead_id, old_status, old_status_date, old_follow_up, new_status,
                    changed_at, kind, old_event_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (batch, lead["id"], lead["status"], lead["status_date"], lead["follow_up_date"], new_status,
                  store.now_local().isoformat(), kind, old_event_date))


def set_status(lead_id: int, status_text: str, batch: str | None = None) -> dict | None:
    """Set a lead's status (any status, any time), dated today. Logged so it can be undone."""
    batch = batch or secrets.token_hex(4)
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            return None
        lead = dict(row)
        status = resolve_status(lead["channel"], status_text, conn)
        lead["changed"] = lead["status"] != status          # False = it already had this status; nothing was touched
        if lead["changed"]:
            _log(conn, batch, lead, status, "status")
            lead["status"], lead["status_date"], lead["follow_up_date"] = status, store.now_local().date().isoformat(), ""
            conn.execute("UPDATE leads SET status=?, status_date=?, follow_up_date='', updated_at=? WHERE id=?",
                         (status, lead["status_date"], store.now_local().isoformat(), lead_id))
            apply_rules(conn, lead)
            _after_change(conn, lead)
        lead["status_label"] = label_for(lead["channel"], lead["status"])
        return lead


def _set_follow_up(conn, lead: dict, day: str, batch: str) -> None:
    """Give a lead a custom follow-up date: its reminder is moved there (or created if it has none)."""
    ref = f"lead:{lead['id']}"
    existing = conn.execute("SELECT * FROM events WHERE lead_ref = ? AND source = 'auto'", (ref,)).fetchone()
    _log(conn, batch, lead, lead["status"], "followup", existing["start_date"] if existing else "")
    conn.execute("UPDATE leads SET follow_up_date=?, updated_at=? WHERE id=?", (day, store.now_local().isoformat(), lead["id"]))
    lead["follow_up_date"] = day
    if existing:
        conn.execute("UPDATE events SET start_date = ? WHERE id = ?", (day, existing["id"]))
        conn.execute("DELETE FROM completions WHERE event_id = ?", (existing["id"],))
    else:
        _insert_event(conn, store.clean(_reminder_fields(lead, store.bucket_for(conn, lead['channel'], lead['status']), conn)))


def set_follow_up(lead_id: int, day: str, batch: str | None = None) -> dict | None:
    d = store._parse_date(day, "date").isoformat()
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            return None
        lead = dict(row)
        _set_follow_up(conn, lead, d, batch or secrets.token_hex(4))
        return lead


def reschedule_follow_ups(to_date: str, lead_ids: list[int] | None = None, from_date: str | None = None,
                          batch: str | None = None) -> dict:
    """Move open lead reminders to another date. from_date: a date, or 'overdue'; lead_ids: only these leads."""
    day = store._parse_date(to_date, "to_date").isoformat()
    batch = batch or secrets.token_hex(4)
    today = store.now_local().date().isoformat()
    if from_date and from_date != "overdue":
        from_date = store._parse_date(from_date, "from_date").isoformat()
    moved = []
    with store._conn() as conn:
        rows = conn.execute("""SELECT e.start_date, l.* FROM events e JOIN leads l ON e.lead_ref = 'lead:' || l.id
                               WHERE e.source = 'auto'
                                 AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.event_id = e.id AND c.occ_date = e.start_date)""").fetchall()
        for r in rows:
            r = dict(r)
            due = r.pop("start_date")
            if lead_ids is not None and r["id"] not in lead_ids:
                continue
            if from_date == "overdue" and not due < today:
                continue
            if from_date and from_date != "overdue" and due != from_date:
                continue
            if due == day:
                continue
            _set_follow_up(conn, r, day, batch)
            moved.append(r["contact"])
    return {"moved": len(moved), "to": day, "first_few": moved[:5]}


def undo_last_change() -> dict:
    """Revert the most recent batch of changes (everything one chat request / click changed)."""
    with store._conn() as conn:
        last = conn.execute("SELECT batch FROM lead_history ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            return {"reverted": 0, "note": "Nothing to undo."}
        rows = conn.execute("SELECT * FROM lead_history WHERE batch = ? ORDER BY id DESC", (last["batch"],)).fetchall()
        names = []
        for h in rows:
            lead = dict(conn.execute("SELECT * FROM leads WHERE id = ?", (h["lead_id"],)).fetchone())
            if h["kind"] == "followup":
                conn.execute("UPDATE leads SET follow_up_date=?, updated_at=? WHERE id=?",
                             (h["old_follow_up"], store.now_local().isoformat(), h["lead_id"]))
                ref = f"lead:{h['lead_id']}"
                if h["old_event_date"]:
                    conn.execute("UPDATE events SET start_date = ? WHERE lead_ref = ? AND source = 'auto'", (h["old_event_date"], ref))
                else:
                    conn.execute("DELETE FROM events WHERE lead_ref = ? AND source = 'auto'", (ref,))
            else:
                conn.execute("UPDATE leads SET status=?, status_date=?, follow_up_date=?, updated_at=? WHERE id=?",
                             (h["old_status"], h["old_status_date"], h["old_follow_up"], store.now_local().isoformat(), h["lead_id"]))
                lead = dict(conn.execute("SELECT * FROM leads WHERE id = ?", (h["lead_id"],)).fetchone())
                apply_rules(conn, lead)
                _after_change(conn, lead)
            names.append(lead["contact"])
        conn.execute("DELETE FROM lead_history WHERE batch = ?", (last["batch"],))
        what = "follow-up dates" if rows[0]["kind"] == "followup" else "statuses"
        return {"reverted": len(rows), "what": what, "first_few": names[:5]}


def list_leads(channel: str | None = None, file_order: bool = False) -> list[dict]:
    """file_order=True keeps the order leads were imported in (for "the first 20")."""
    with store._conn() as conn:
        order = "id" if file_order else "contact"
        if channel:
            rows = conn.execute(f"SELECT * FROM leads WHERE channel = ? ORDER BY {order}", (channel,))
        else:
            rows = conn.execute(f"SELECT * FROM leads ORDER BY channel, {order}")
        return [dict(r) for r in rows]


def reminders_by_lead() -> dict:
    """lead id -> {'date': when its auto reminder is due, 'closed': whether it was completed}."""
    out = {}
    with store._conn() as conn:
        for r in conn.execute("""SELECT e.lead_ref, e.start_date,
                                 EXISTS (SELECT 1 FROM completions c WHERE c.event_id = e.id AND c.occ_date = e.start_date) AS closed
                                 FROM events e WHERE e.source = 'auto' AND e.lead_ref LIKE 'lead:%'"""):
            out[int(r["lead_ref"][5:])] = {"date": r["start_date"], "closed": bool(r["closed"])}
    return out


def sample_csv(channel: str) -> str:
    """Dummy lead list with dates relative to today, so reminders land sensibly."""
    today = store.now_local().date()
    ago = lambda n: (today - timedelta(days=n)).isoformat()
    if channel == "linkedin":
        rows = [("Dana Roy", "sent", ago(3), "https://www.linkedin.com/in/dana-roy-demo"),
                ("Sam Keller", "sent", ago(5), "https://www.linkedin.com/in/sam-keller-demo"),
                ("Priya Nair", "sent", ago(1), "https://www.linkedin.com/in/priya-nair-demo"),
                ("Marc Tremblay", "replied", ago(2), "https://www.linkedin.com/in/marc-tremblay-demo"),
                ("Lena Fischer", "new", ago(0), "https://www.linkedin.com/in/lena-fischer-demo")]
        header = "contact,status,date,url"
    else:
        rows = [("@studio.mara", "sent", ago(3), "https://www.instagram.com/studio.mara"),
                ("@jules.bakes", "sent", ago(2), "https://www.instagram.com/jules.bakes"),
                ("@northside.yoga", "replied", ago(4), "https://www.instagram.com/northside.yoga"),
                ("@atelier.lune", "new", ago(0), "https://www.instagram.com/atelier.lune")]
        header = "contact,status,date,url"
    return header + "\n" + "\n".join(",".join(r) for r in rows) + "\n"


def set_status_bucket(status_text: str, bucket: str, channel: str | None = None) -> dict:
    """Change how a status is treated (cold/warm/closed/none) and bring the leads already in that status in line."""
    if bucket not in store.BUCKETS:
        raise store.ValidationError("bucket must be cold, warm, closed or none")
    out = {"status": status_text, "bucket": bucket, "channels": [], "leads_updated": 0}
    for ch in ([channel] if channel else list(CHANNELS)):
        try:
            key = resolve_status(ch, status_text)
        except store.ValidationError:
            if channel:
                raise
            continue
        store.set_bucket(ch, key, bucket)
        out["channels"].append(ch)
        with store._conn() as conn:
            for row in conn.execute("SELECT * FROM leads WHERE channel = ? AND status = ?", (ch, key)).fetchall():
                if apply_rules(conn, dict(row)):
                    out["leads_updated"] += 1
    if not out["channels"]:
        raise store.ValidationError(f'"{status_text}" is not a status on either channel')
    return out


def migrate_buckets() -> str:
    """One-time upgrade of existing data to the cold/warm model: every automatic task becomes 'no specific time',
    closed leads get their task completed, and warm leads get their own 'Reply to' task."""
    with store._conn() as conn:
        if conn.execute("SELECT 1 FROM sync_state WHERE key = 'migrated_buckets_v1'").fetchone():
            return "already done"
        conn.execute("UPDATE events SET untimed = 1, start_time = ? WHERE source = 'auto'", (store.UNTIMED_TIME,))
        changed = 0
        for row in conn.execute("SELECT * FROM leads").fetchall():
            lead = dict(row)
            bucket = store.bucket_for(conn, lead["channel"], lead["status"])
            if bucket in ("warm", "closed") and apply_rules(conn, lead):
                changed += 1
        conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('migrated_buckets_v1', ?)", (store.now_local().isoformat(),))
        return f"migrated ({changed} leads adjusted)"


def migrate_statuses_v2() -> str:
    """One-time: the Email channel's rules, and the statuses for the later funnel stages (the same on every channel)."""
    with store._conn() as conn:
        if conn.execute("SELECT 1 FROM sync_state WHERE key = 'migrated_statuses_v2'").fetchone():
            return "already done"
        for (ch, key), bucket in store.DEFAULT_BUCKETS.items():
            conn.execute("INSERT OR IGNORE INTO status_buckets (channel, status_key, bucket) VALUES (?, ?, ?)", (ch, key, bucket))
        for (ch, key), stage in store.DEFAULT_STAGES.items():
            conn.execute("INSERT OR IGNORE INTO status_stages (channel, status_key, stage_name) VALUES (?, ?, ?)", (ch, key, stage))
        for ch in CHANNELS:
            for key, (stage, bucket, task) in store.LATER_STATUSES.items():
                conn.execute("INSERT OR IGNORE INTO custom_statuses (channel, label) VALUES (?, ?)", (ch, key))
                conn.execute("INSERT OR IGNORE INTO status_stages (channel, status_key, stage_name) VALUES (?, ?, ?)", (ch, key, stage))
                conn.execute("INSERT OR IGNORE INTO status_buckets (channel, status_key, bucket) VALUES (?, ?, ?)", (ch, key, bucket))
                if task:
                    conn.execute("INSERT OR IGNORE INTO status_tasks (channel, status_key, title) VALUES (?, ?, ?)", (ch, key, task))
        conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('migrated_statuses_v2', ?)", (store.now_local().isoformat(),))
        return "migrated"
