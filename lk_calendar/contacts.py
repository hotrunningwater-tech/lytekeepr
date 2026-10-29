"""Companies, contacts, and the universal import.

Storage is the CRM database's own tables: companies, people, channel_accounts (one row per LinkedIn/Instagram account of a person).
A company with no named person gets one person whose name is the account handle or the business name, so everything belongs to a person.

One universal field list for every channel (a channel that lacks a field leaves it empty). A file is first SCANNED
(its columns are read and each is matched to a universal field, from a remembered mapping or by name), the user checks
the matches, and then it is IMPORTED: people become contacts (with their company), and optionally funnel entries
(the existing `leads` rows, which keep carrying status, follow-ups and reminders).
"""

import csv
import io
import json
from pathlib import Path
import re
import secrets
import time
from datetime import date, datetime, timedelta

from contextlib import contextmanager
from urllib.parse import unquote

from crm import db as crm

from . import brands, leads, store

FIELDS = [   # key, label  (business_name becomes the company; status/dates feed the funnel entry)
    ("name", "Name"), ("url", "URL (profile link)"), ("business_name", "Business name"), ("job_title", "Job title"),
    ("headline", "Headline / bio"), ("location", "Location"), ("industry", "Industry / type"), ("audience", "Audience size"),
    ("phone", "Phone / WhatsApp"), ("telegram", "Telegram"), ("email", "Email"), ("website", "Website"), ("verified", "Verified"),
    ("date_found", "Date found"), ("notes", "Notes"), ("status", "Status"), ("status_date", "Status date"), ("follow_up_date", "Follow-up date"),
]
FIELD_KEYS = {k for k, _ in FIELDS}
SPECIAL = [("extra", "Keep as extra field"), ("ignore", "Ignore this column")]
CONTACT_COLUMNS = ["name", "url", "job_title", "headline", "location", "industry", "audience", "phone", "telegram", "email", "website",
                   "verified", "date_found", "notes"]
PERSON_COLUMNS = [c for c in CONTACT_COLUMNS if c not in ("url", "email")]       # url and email live on the channel accounts

SYNONYMS = {
    "url": "url|profile url|profile|link|linkedin url|instagram url|instagram|profileurl|profile link|commenter url",
    "name": "name|full name|fullname|commenter|person|contact",
    "business_name": "company|business name|organization|organisation|current company|company name|business",
    "job_title": "current role|title|job title|position|role|commenter position",
    "headline": "headline|bio|biography|description|about",
    "location": "location|country|city|region",
    "industry": "industry|category|type",
    "audience": "connections|followers|follower count|followercount|audience",
    "phone": "phone|whatsapp|whatsapp phone|mobile",
    "telegram": "telegram",
    "email": "email|e mail|mail",
    "website": "website|site|web|externalurl|external url",
    "verified": "verified|isverified|is verified",
    "date_found": "scraped at|scrapedat|date found|found",
    "notes": "notes|note|comment|comments",
    "status": "status|stage",
    "status_date": "date|status date|sent date|date sent",
    "follow_up_date": "follow up date|followup date|follow up|next follow up",
}
_SYN = {syn: key for key, group in SYNONYMS.items() for syn in group.split("|")}

MAX_ROWS = 10000
MAX_BYTES = 8 * 1024 * 1024
_UPLOADS: dict = {}          # token -> scanned file (kept in memory for a short while; one app process)
UPLOAD_TTL = 30 * 60


def _norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _norm_url(text: str) -> str:
    t = (text or "").strip().lower().split("?")[0].split("#")[0].rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", t)


def detect_channel(url: str) -> str:
    u = (url or "").lower()
    return "linkedin" if "linkedin.com" in u else "instagram" if "instagram.com" in u else ""


# ----------------------------------------------------------------- reading files
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if not (v.hour or v.minute) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_table(raw: bytes, filename: str) -> tuple[list[str], list[dict]]:
    """Any CSV or XLSX -> (headers, rows as dicts). Empty rows are dropped."""
    if len(raw) > MAX_BYTES:
        raise store.ValidationError("the file is larger than 8 MB")
    name = (filename or "").lower()
    if name.endswith(".xls"):
        raise store.ValidationError("Old .xls files aren't supported. Open it in Excel and save it as .xlsx or CSV.")
    if name.endswith(".xlsx"):
        try:
            import openpyxl
            ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).worksheets[0]
            grid = [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
        except Exception as exc:
            raise store.ValidationError(f"Could not read that Excel file: {exc}")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        grid = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect=dialect)]
    grid = [r for r in grid if any(c for c in r)]
    if len(grid) < 2:
        raise store.ValidationError("the file has no data rows")
    headers, seen = [], {}
    for i, h in enumerate(grid[0]):
        h = h or f"column {i + 1}"
        seen[h] = seen.get(h, 0) + 1
        headers.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
    rows = [dict(zip(headers, (r + [""] * len(headers))[:len(headers)])) for r in grid[1:MAX_ROWS + 1]]
    return headers, rows


# ----------------------------------------------------------------- scan: read the columns, guess the matches
def signature(headers: list[str]) -> str:
    return "|".join(sorted(_norm_header(h) for h in headers))


def _week() -> str:
    return "Week of " + (date.today() - timedelta(days=date.today().weekday())).strftime("%b %d").replace(" 0", " ")


def scan(raw: bytes, filename: str) -> dict:
    headers, rows = read_table(raw, filename)
    now = time.time()
    for k in [k for k, v in _UPLOADS.items() if now - v["ts"] > UPLOAD_TTL]:
        _UPLOADS.pop(k)
    token = secrets.token_urlsafe(12)
    _UPLOADS[token] = {"name": filename, "headers": headers, "rows": rows, "ts": now}
    with store._conn() as conn:
        saved = conn.execute("SELECT * FROM import_mappings WHERE signature = ?", (signature(headers),)).fetchone()
    saved_map = json.loads(saved["mapping"]) if saved else {}
    cols = []
    for h in headers:
        sample = [r[h] for r in rows if r.get(h)][:3]
        if h in saved_map:
            guess, matched = saved_map[h], True
        elif _norm_header(h) in _SYN:
            guess, matched = _SYN[_norm_header(h)], True
        else:
            guess, matched = "extra", False
        cols.append({"name": h, "sample": sample, "field": guess, "matched": matched})
    urls = [r.get(c["name"], "") for c in cols if c["field"] == "url" for r in rows[:50]]
    chans = [detect_channel(u) for u in urls if u]
    channel = max(set(chans), key=chans.count) if chans else ""
    return {"token": token, "filename": filename, "rows": len(rows), "columns": cols, "channel": channel,
            "saved_mapping": saved["name"] if saved else None,
            "fields": [{"key": k, "label": v} for k, v in FIELDS + SPECIAL],
            "default_batch": " · ".join(x for x in (channel.title(), _week()) if x)}


# ----------------------------------------------------------------- storage (CRM tables)
_ready: set = set()


@contextmanager
def _crm():
    from profiles.store import get_active_profile_id
    workspace = get_active_profile_id()
    if workspace not in _ready:            # each workspace's database is brought up to date once (not just the first one used)
        crm.init_db()
        _ready.add(workspace)
    conn = crm.get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return store.now_local().isoformat()


def _identifier(channel: str, url: str) -> str:
    """The same identity the CRM already uses: LinkedIn slug, Instagram handle (lower case)."""
    url = (url or "").strip()
    if not url:
        return ""
    if channel == "email":
        return url.lower()
    if channel == "instagram":
        return leads._instagram_handle(url).lower()
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#]+)", url, re.I)
    return unquote(m.group(1)).lower() if m else _norm_url(url)


_COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|limited|co|corp|corporation|company|the|gmbh|sa|plc)\b")


def _norm_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", _COMPANY_NOISE.sub(" ", n)).strip()


def _company(conn, name: str, website: str = "", source_file: str = "") -> int | None:
    """The company with this name, written any way (capitals, "Inc.", "The ...": same company). Created only if none matches."""
    name = (name or "").strip()
    if not name or name.lower() in ("n/a", "na", "-"):
        return None
    want = _norm_company(name) or name.lower()
    row = None
    for r in conn.execute("SELECT company_id, company_name, website FROM companies"):
        if (_norm_company(r["company_name"]) or r["company_name"].lower()) == want:
            row = r
            break
    if row:
        if website and not row["website"]:
            conn.execute("UPDATE companies SET website = ? WHERE company_id = ?", (website[:300], row["company_id"]))
        return row["company_id"]
    return conn.execute("INSERT INTO companies (company_name, website, created_at, source_file) VALUES (?, ?, ?, ?)",
                        (name[:200], website[:300], _now(), source_file)).lastrowid


def _phone_key(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 7 else ""


def _fallback_name(channel: str, url: str, company_name: str) -> str:
    """Only an Instagram @handle stands in for a missing name. A company or a link is never copied into the name."""
    if channel == "instagram":
        h = leads._instagram_handle(url)
        if h:
            return "@" + h
    return ""


def _find_person(conn, channel: str, ident: str, name: str, company_id) -> int | None:
    if channel and ident:
        acc = crm.find_channel_account(conn, channel, ident)
        return acc["person_id"] if acc else None
    if name:
        r = conn.execute("SELECT person_id FROM people WHERE lower(name) = lower(?) AND company_id IS ?", (name, company_id)).fetchone()
        return r["person_id"] if r else None
    return None


def _insert_person(conn, vals: dict, *, channel: str, company_id, company_name: str, extras: dict, source_file: str, batch: str) -> int:
    url = str(vals.get("url", "")).strip()
    name = (str(vals.get("name", "")).strip() or _fallback_name(channel, url, company_name))[:200]
    row = {c: str(vals.get(c, ""))[:2000] for c in PERSON_COLUMNS if c != "name"}
    row.update(name=name, company_id=company_id, extras=json.dumps(extras, ensure_ascii=False),
               raw_notes=str(vals.get("raw_notes", ""))[:200000],
               source_file=source_file[:200], batch=batch[:100], created_at=_now())
    cols = list(row)
    pid = conn.execute(f"INSERT INTO people ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", row).lastrowid
    try:
        from . import users
        conn.execute("UPDATE people SET owner_id = ? WHERE person_id = ?", (users.current_id(), pid))      # who added it: they always see their own contacts
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not record who added the new contact")
    if channel or url:
        ident = _identifier(channel, url) or f"name:{name or company_name}"
        conn.execute("INSERT INTO channel_accounts (person_id, channel, channel_identifier, display_name, url) VALUES (?, ?, ?, ?, ?)",
                     (pid, channel or "other", ident, name, "" if channel == "email" else url[:500]))
    return pid


def sync_lead_identity(pid: int) -> None:
    """The calendar names a person's statuses and tasks after the person (LinkedIn: their name, email: their address). When either changes,
    the status record and the title of its follow-up task follow, so no task still carries an old name or address."""
    with _crm() as conn:
        p = conn.execute("SELECT name FROM people WHERE person_id = ?", (pid,)).fetchone()
        name = ((p["name"] if p else "") or "").strip()
        email = (conn.execute("SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email' ORDER BY account_id LIMIT 1", (pid,)).fetchone() or [""])[0] or ""
    with store._conn() as cal:
        for lead in [dict(r) for r in cal.execute("SELECT id, channel, contact FROM leads WHERE person_id = ?", (pid,))]:
            new = name if lead["channel"] == "linkedin" else email if lead["channel"] == "email" else ""
            if not new or new == lead["contact"]:
                continue
            if cal.execute("SELECT 1 FROM leads WHERE channel = ? AND contact = ? AND id != ?", (lead["channel"], new, lead["id"])).fetchone():
                continue                                             # another lead already uses that name: leave this one alone
            cal.execute("UPDATE leads SET contact = ? WHERE id = ?", (new, lead["id"]))
            for ev in cal.execute("SELECT id, title FROM events WHERE lead_ref = ? AND source = 'auto'", (f"lead:{lead['id']}",)).fetchall():
                cal.execute("UPDATE events SET title = ? WHERE id = ?", (ev["title"].replace(lead["contact"], new), ev["id"]))


def _set_email(conn, pid: int, email: str, name: str) -> None:
    """Make the contact's email channel record say this address (add it, change it, or remove it when cleared and it has no messages)."""
    e = (email or "").strip().lower()
    acct = conn.execute("SELECT account_id, channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email' ORDER BY account_id LIMIT 1", (pid,)).fetchone()
    if not e:
        if acct and not conn.execute("SELECT 1 FROM messages WHERE account_id = ? LIMIT 1", (acct["account_id"],)).fetchone():
            conn.execute("DELETE FROM channel_accounts WHERE account_id = ?", (acct["account_id"],))
        return
    if acct is None:
        _ensure_email_account(conn, pid, e, name)
    elif (acct["channel_identifier"] or "").lower() != e:
        other = crm.find_channel_account(conn, "email", e)
        if other and other["person_id"] != pid:
            raise store.ValidationError("That email address already belongs to another contact.")
        conn.execute("UPDATE channel_accounts SET channel_identifier = ?, display_name = ? WHERE account_id = ?", (e, name or None, acct["account_id"]))


def _ensure_email_account(conn, pid: int, email: str, name: str) -> None:
    e = (email or "").strip().lower()
    if "@" not in e or crm.find_channel_account(conn, "email", e):
        return
    conn.execute("INSERT INTO channel_accounts (person_id, channel, channel_identifier, display_name, url) VALUES (?, ?, ?, ?, ?)", (pid, "email", e, name, ""))


SAVE_RULE = "A contact needs a name or a company, and one way to reach them: an email, a phone number, or a LinkedIn or Instagram link."


def _reachable(name: str, company: str, email: str, phone: str, links: list) -> bool:
    return bool((name or "").strip() or (company or "").strip()) and bool(
        (email or "").strip() or (phone or "").strip() or any(l for l in links if l))


_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINK_OK = re.compile(r"^(https?://|www\.|(linkedin|instagram)\.com/|@[\w.]+$)", re.I)


def check_formats(data: dict) -> None:
    email, url, phone = str(data.get("email", "")).strip(), str(data.get("url", "")).strip(), str(data.get("phone", "")).strip()
    if email and not _EMAIL_OK.match(email):
        raise store.ValidationError(f'"{email}" doesn\'t look like an email address.')
    if url and not _LINK_OK.match(url):
        raise store.ValidationError("The profile link must be a web address (a LinkedIn or Instagram link), not " + ("an email address." if "@" in url else f'"{url}".'))
    if phone and not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise store.ValidationError(f'"{phone}" doesn\'t look like a phone number.')


def _missing(name: str, company: str, email: str, phone: str, links: list) -> str:
    """What is missing for a contact to be saved ('' when it is fine)."""
    need = []
    if not ((name or "").strip() or (company or "").strip()):
        need.append("a name or a company")
    if not ((email or "").strip() or (phone or "").strip() or any(l for l in links if l)):
        need.append("a way to reach them (an email, a phone number, or a LinkedIn or Instagram link)")
    return ("This contact can't be saved yet. Please add " + " and ".join(need) + ".") if need else ""


def _person_state(conn, pid: int) -> tuple:
    p = conn.execute("""SELECT p.name, p.phone, co.company_name,
                               (SELECT channel_identifier FROM channel_accounts e WHERE e.person_id = p.person_id AND e.channel = 'email' ORDER BY e.account_id LIMIT 1) AS email
                        FROM people p LEFT JOIN companies co ON co.company_id = p.company_id WHERE p.person_id = ?""", (pid,)).fetchone()
    links = [r["url"] or r["channel_identifier"] for r in conn.execute(
        "SELECT channel, channel_identifier, url FROM channel_accounts WHERE person_id = ? AND channel IN ('linkedin', 'instagram') AND channel_identifier NOT LIKE 'name:%'", (pid,))]
    return (p["name"], p["company_name"], p["email"], p["phone"], links)


def append_raw_notes(contact_id: int, text: str) -> None:
    """The exact text the user pasted, saved by code (never by the AI), newest at the bottom."""
    text = (text or "").strip()
    if not text:
        return
    with _crm() as conn:
        row = conn.execute("SELECT raw_notes FROM people WHERE person_id = ?", (contact_id,)).fetchone()
        if row is None:
            raise store.ValidationError("no such person")
        entry = f"— {_now()[:16].replace('T', ' ')} —\n{text}"
        conn.execute("UPDATE people SET raw_notes = ? WHERE person_id = ?", ((row["raw_notes"] + "\n\n" if row["raw_notes"] else "") + entry, contact_id))


def _account_link(a) -> str:
    """The profile link of a LinkedIn / Instagram channel record: the saved one, or built from the handle when only that was kept."""
    if a["url"]:
        return a["url"]
    ident = (a["channel_identifier"] or "").strip()
    if not ident or ident.startswith("name:"):
        return ""
    return f"https://www.instagram.com/{ident}" if a["channel"] == "instagram" else f"https://www.linkedin.com/in/{ident}"


def contact_dict(conn, pid: int) -> dict | None:
    r = conn.execute("""SELECT p.*, co.company_name, fn.name AS funnel_name, fn.brand AS funnel_brand, fs.name AS stage_name FROM people p
                        LEFT JOIN companies co ON co.company_id = p.company_id LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id
                        LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id WHERE p.person_id = ?""", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("headline", "location", "industry", "audience", "phone", "telegram", "website", "verified", "date_found", "notes",
              "job_title", "batch", "source_file"):
        d[k] = d.get(k) or ""
    d["raw_notes"] = d.get("raw_notes") or ""
    d["id"], d["company"] = d.pop("person_id"), d.pop("company_name") or ""
    d["extras"] = json.loads(d.get("extras") or "{}")
    accts = conn.execute("SELECT channel, channel_identifier, url FROM channel_accounts WHERE person_id = ? ORDER BY account_id", (d["id"],)).fetchall()
    d["channels"] = [a["channel"] for a in accts if a["channel"] and a["channel"] != "other"]
    d["channel"] = d["channels"][0] if d["channels"] else ""
    d["url"] = next((_account_link(a) for a in accts if a["channel"] in ("linkedin", "instagram") and _account_link(a)), "")
    if True:                                         # the email address is the email channel record: the one home for it
        d["email"] = next((a["channel_identifier"] for a in accts if a["channel"] == "email" and "@" in (a["channel_identifier"] or "")), "")
    d["instagram"] = next(("@" + a["channel_identifier"] for a in accts if a["channel"] == "instagram" and a["channel_identifier"]
                           and not a["channel_identifier"].startswith("name:")), "")
    d["brand"], d["funnel_brand"] = d.get("brand") or "", d.get("funnel_brand") or ""      # funnel_brand set = the brand comes from the funnel
    d["display"] = d["name"] or d["email"] or d["phone"] or d["instagram"] or d["telegram"] or d["company"] or "(no name)"   # no name: how to reach them says who it is
    d["funnels"] = [{"channel": d["funnel_name"], "status": d["stage_name"] or "", "status_date": ""}] if d.get("funnel_id") else []
    return d


# ----------------------------------------------------------------- import
def import_rows(token: str, mapping: dict, options: dict) -> dict:
    up = _UPLOADS.get(token)
    if not up:
        raise store.ValidationError("That upload expired. Choose the file again.")
    channel_opt = (options.get("channel") or "").strip().lower()
    if channel_opt and channel_opt not in ("linkedin", "instagram"):
        raise store.ValidationError("channel must be linkedin or instagram")
    dup = options.get("duplicates", "skip")
    batch = (options.get("batch") or "").strip()[:100]
    if not options.get("brand"):
        raise store.ValidationError(brands.needed_message().replace("this contact", "the imported contacts"))
    import_brand = brands.resolve(options["brand"])
    res = {"added": 0, "skipped": 0, "filled": 0, "merged_in_file": 0, "excluded": 0, "companies_created": 0, "errors": [], "batch": batch}
    created: set = set()
    with _crm() as conn:
        companies_before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        for n, raw_row in enumerate(up["rows"], start=2):
            vals, extras = {}, {}
            for header, field in mapping.items():
                v = (raw_row.get(header) or "").strip()
                if not v:
                    continue
                if field == "extra":
                    extras[header] = v
                elif field in FIELD_KEYS:
                    vals[field] = (vals[field] + "\n" + v) if field == "notes" and vals.get(field) and v not in vals[field] else vals.get(field) or v
            channel = channel_opt or detect_channel(vals.get("url", ""))
            if not (vals.get("url") or vals.get("name") or vals.get("business_name")):
                res["errors"].append(f"row {n}: nothing to identify the person (no link, name or business)")
                continue
            ident = _identifier(channel, vals.get("url", ""))
            if channel and ident and crm.is_contact_excluded(conn, channel, ident):
                res["excluded"] += 1
                continue
            company_name = vals.get("business_name", "")
            company_id = _company(conn, company_name, vals.get("website", ""), up["name"])
            pid = _find_person(conn, channel, ident, vals.get("name", ""), company_id)
            if pid is None and vals.get("email"):
                acct = crm.find_channel_account(conn, "email", vals["email"].strip().lower())
                pid = acct["person_id"] if acct else None
            if pid is None and _phone_key(vals.get("phone", "")):
                key = _phone_key(vals["phone"])
                pid = next((r["person_id"] for r in conn.execute("SELECT person_id, phone FROM people WHERE IFNULL(phone, '') != ''") if _phone_key(r["phone"]) == key), None)
            if pid is None:
                pid = _insert_person(conn, vals, channel=channel, company_id=company_id, company_name=company_name, extras=extras, source_file=up["name"], batch=batch)
                conn.execute("UPDATE people SET brand = ? WHERE person_id = ?", (import_brand, pid))
                created.add(pid)
                res["added"] += 1
                _ensure_email_account(conn, pid, vals.get("email", ""), vals.get("name", "") or company_name)
            elif pid in created:
                res["merged_in_file"] += 1
            elif dup == "fill":
                cur = dict(conn.execute("SELECT * FROM people WHERE person_id = ?", (pid,)).fetchone())
                sets = {c: vals[c] for c in PERSON_COLUMNS if c != "name" and vals.get(c) and not cur.get(c)}
                ex = json.loads(cur.get("extras") or "{}")
                new_ex = {k: v for k, v in extras.items() if k not in ex}
                if new_ex:
                    sets["extras"] = json.dumps({**ex, **new_ex}, ensure_ascii=False)
                if company_id and not cur["company_id"]:
                    sets["company_id"] = company_id
                _ensure_email_account(conn, pid, vals.get("email", ""), cur.get("name") or "")
                if sets:
                    conn.execute(f"UPDATE people SET {', '.join(k + ' = :' + k for k in sets)} WHERE person_id = :id", {**sets, "id": pid})
                    res["filled"] += 1
                else:
                    res["skipped"] += 1
            else:
                res["skipped"] += 1
        res["companies_created"] = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] - companies_before
    if options.get("remember"):
        with store._conn() as conn:
            name = (options.get("mapping_name") or f"{(channel_opt or 'import').title()} file ({len(mapping)} columns)")[:80]
            conn.execute("""INSERT INTO import_mappings (name, signature, mapping, channel, used, updated_at) VALUES (?, ?, ?, ?, 1, ?)
                            ON CONFLICT(signature) DO UPDATE SET mapping = excluded.mapping, used = used + 1, updated_at = excluded.updated_at""",
                         (name, signature(up["headers"]), json.dumps(mapping, ensure_ascii=False), channel_opt, _now()))
    return res


def list_mappings() -> list[dict]:
    with store._conn() as conn:
        out = []
        for r in conn.execute("SELECT * FROM import_mappings ORDER BY updated_at DESC"):
            m = json.loads(r["mapping"])
            out.append({"id": r["id"], "name": r["name"], "columns": len(m), "matched": sum(1 for v in m.values() if v not in ("extra", "ignore")),
                        "used": r["used"], "updated_at": r["updated_at"]})
        return out


def delete_mapping(mapping_id: int) -> bool:
    with store._conn() as conn:
        return conn.execute("DELETE FROM import_mappings WHERE id = ?", (mapping_id,)).rowcount > 0


# ----------------------------------------------------------------- the company and contact pages
def list_companies(q: str = "") -> dict:
    with _crm() as conn:
        rows = conn.execute("""SELECT c.company_id AS id, c.company_name AS name, c.website, COUNT(p.person_id) AS contacts FROM companies c
                               LEFT JOIN people p ON p.company_id = c.company_id WHERE lower(c.company_name) LIKE ?
                               GROUP BY c.company_id ORDER BY c.company_name LIMIT 300""", (f"%{q.lower()}%",)).fetchall()
        return {"companies": [dict(r) for r in rows], "without_company": conn.execute("SELECT COUNT(*) FROM people WHERE company_id IS NULL").fetchone()[0],
                "total_contacts": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]}


def list_contacts(q: str = "", company_id=None, limit: int = 200) -> list[dict]:
    q = (q or "").strip()
    q = q[1:] if q.startswith("@") and len(q) > 1 and " " not in q else q        # "@handle" finds the Instagram handle too
    like = f"%{q.lower()}%"
    with _crm() as conn:
        where = """(lower(p.name) LIKE ? OR lower(IFNULL(p.notes,'')) LIKE ? OR lower(IFNULL(co.company_name,'')) LIKE ?
                    OR EXISTS (SELECT 1 FROM channel_accounts a WHERE a.person_id = p.person_id AND (lower(a.channel_identifier) LIKE ? OR lower(IFNULL(a.url,'')) LIKE ?)))"""
        args = [like] * 5
        if company_id == "none":
            where += " AND p.company_id IS NULL"
        elif company_id:
            where += " AND p.company_id = ?"; args.append(int(company_id))
        ids = [r[0] for r in conn.execute(f"SELECT p.person_id FROM people p LEFT JOIN companies co ON co.company_id = p.company_id WHERE {where} ORDER BY p.person_id DESC LIMIT ?",
                                          args + [min(int(limit), 500)])]
        return [contact_dict(conn, i) for i in ids]


def find_duplicate(email: str = "", url: str = "", exclude_id=None, phone: str = "") -> dict | None:
    """An existing contact that already owns this email (any capitalisation), LinkedIn / Instagram link or phone number (other than exclude_id)."""
    email, url = (email or "").strip().lower(), (url or "").strip()
    with _crm() as conn:
        pid, reason = None, ""
        if email:
            acct = crm.find_channel_account(conn, "email", email)
            if acct and acct["person_id"] != exclude_id:
                pid, reason = acct["person_id"], "email address"
        if pid is None and url:
            ch = detect_channel(url)
            ident = _identifier(ch, url) if ch else ""
            acct = crm.find_channel_account(conn, ch, ident) if ident else None
            if acct and acct["person_id"] != exclude_id:
                pid, reason = acct["person_id"], "profile link"
        if pid is None and _phone_key(phone):
            key = _phone_key(phone)
            for r in conn.execute("SELECT person_id, phone FROM people WHERE IFNULL(phone, '') != ''"):
                if r["person_id"] != exclude_id and _phone_key(r["phone"]) == key:
                    pid, reason = r["person_id"], "phone number"
                    break
        if pid is None:
            return None
        d = contact_dict(conn, pid)
        return {"id": d["id"], "name": d["name"], "company": d["company"], "raw_notes": d["raw_notes"], "reason": reason}


def delete_calendar_links(contact_id: int) -> int:
    """Remove the calendar side of a person: their statuses (leads), follow-up tasks and history."""
    with store._conn() as cal:
        ids = [r["id"] for r in cal.execute("SELECT id FROM leads WHERE person_id = ?", (contact_id,))]
        for lid in ids:
            for ev in cal.execute("SELECT id FROM events WHERE lead_ref = ?", (f"lead:{lid}",)).fetchall():
                cal.execute("DELETE FROM completions WHERE event_id = ?", (ev["id"],))
            cal.execute("DELETE FROM events WHERE lead_ref = ?", (f"lead:{lid}",))
            cal.execute("DELETE FROM lead_history WHERE lead_id = ?", (lid,))
            cal.execute("DELETE FROM leads WHERE id = ?", (lid,))
        for ev in cal.execute("SELECT id FROM events WHERE lead_ref = ?", (f"contact:{contact_id}",)).fetchall():     # tasks linked to them
            cal.execute("DELETE FROM completions WHERE event_id = ?", (ev["id"],))
        cal.execute("DELETE FROM events WHERE lead_ref = ?", (f"contact:{contact_id}",))
    return len(ids)


def collect_for_delete(contact_id: int) -> dict:
    """What belongs to a person and must go with them, read before the person is removed."""
    with _crm() as conn:
        emails = [r["channel_identifier"] for r in conn.execute("SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email'", (contact_id,))]
        row = conn.execute("SELECT company_id FROM people WHERE person_id = ?", (contact_id,)).fetchone()
        has_messages = conn.execute("SELECT COUNT(*) FROM messages WHERE person_id = ?", (contact_id,)).fetchone()[0] > 0
    return {"person_id": contact_id, "emails": emails, "company_id": row["company_id"] if row else None, "has_messages": has_messages}


def cleanup_after_delete(info: dict) -> None:
    """After the person is gone: their saved email drafts, and their company if nobody else is in it."""
    try:
        from assistant import email_send_flow
        email_send_flow.discard_drafts_for(info["person_id"], info["emails"])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not remove the saved drafts of a deleted contact")
    if info.get("company_id"):
        with _crm() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE company_id = ? LIMIT 1", (info["company_id"],)).fetchone():
                conn.execute("DELETE FROM companies WHERE company_id = ?", (info["company_id"],))


def delete_contact(contact_id: int) -> bool:
    """Delete a contact for good: details, notes, raw notes, messages, statuses, calendar tasks, saved drafts, and their company if they were its only person.
    A person who has synced messages is also put on the removed list, so the next sync doesn't bring them back."""
    info = collect_for_delete(contact_id)
    with _crm() as conn:
        if not conn.execute("SELECT 1 FROM people WHERE person_id = ?", (contact_id,)).fetchone():
            return False
    delete_calendar_links(contact_id)
    with _crm() as conn:
        if info["has_messages"]:
            crm.delete_person(conn, contact_id)
        else:
            conn.execute("DELETE FROM follow_ups WHERE person_id = ?", (contact_id,))
            conn.execute("DELETE FROM channel_accounts WHERE person_id = ?", (contact_id,))
            conn.execute("DELETE FROM people WHERE person_id = ?", (contact_id,))
    cleanup_after_delete(info)
    from . import interactions
    interactions.contact_deleted(contact_id)
    return True


def get_contact(contact_id: int) -> dict | None:
    with _crm() as conn:
        return contact_dict(conn, contact_id)


def get_company(company_id: int) -> dict | None:
    with _crm() as conn:
        co = conn.execute("SELECT company_id AS id, company_name AS name, website, notes FROM companies WHERE company_id = ?", (company_id,)).fetchone()
        if not co:
            return None
        ids = [r[0] for r in conn.execute("SELECT person_id FROM people WHERE company_id = ? ORDER BY person_id", (company_id,))]
        return {**dict(co), "contacts": [contact_dict(conn, i) for i in ids]}


def save_company(company_id, data: dict) -> dict:
    name = str(data.get("name", "")).strip()
    if not name:
        raise store.ValidationError("a company needs a name")
    with _crm() as conn:
        clash = conn.execute("SELECT company_id FROM companies WHERE lower(company_name) = lower(?) AND company_id != ?", (name, company_id or 0)).fetchone()
        if clash:
            raise store.ValidationError(f'"{name}" already exists')
        if company_id:
            conn.execute("UPDATE companies SET company_name=?, website=?, notes=? WHERE company_id=?",
                         (name[:200], str(data.get("website", ""))[:300], str(data.get("notes", ""))[:2000], company_id))
        else:
            company_id = conn.execute("INSERT INTO companies (company_name, website, notes, created_at, source_file) VALUES (?, ?, ?, ?, 'added by hand')",
                                      (name[:200], str(data.get("website", ""))[:300], str(data.get("notes", ""))[:2000], _now())).lastrowid
    return get_company(company_id)


def instagram_handle(text) -> str:
    """'@name', 'name' or an instagram.com link -> 'name'; '' for nothing; an error for something that is not a handle."""
    text = str(text or "").strip()
    if not text:
        return ""
    handle = leads._instagram_handle(text)
    if not handle:
        raise store.ValidationError("That doesn't look like an Instagram handle. Use @name (letters, numbers, dots and underscores).")
    return handle


def _set_instagram(conn, person_id: int, handle: str, name: str) -> None:
    """The contact's Instagram account: added, changed, or removed when the field is emptied."""
    acct = conn.execute("SELECT * FROM channel_accounts WHERE person_id = ? AND channel = 'instagram' ORDER BY account_id LIMIT 1", (person_id,)).fetchone()
    if not handle:
        if acct:
            conn.execute("DELETE FROM channel_accounts WHERE account_id = ?", (acct["account_id"],))
        return
    url = f"https://www.instagram.com/{handle}/"
    if acct:
        conn.execute("UPDATE channel_accounts SET channel_identifier = ?, url = ? WHERE account_id = ?", (handle.lower(), url, acct["account_id"]))
    else:
        conn.execute("INSERT INTO channel_accounts (person_id, channel, channel_identifier, display_name, url) VALUES (?, 'instagram', ?, ?, ?)",
                     (person_id, handle.lower(), name, url))


def create_contact(data: dict, require_brand: bool = True) -> dict:
    """A new contact. Someone adding a contact (the form, Claude, the API) must choose its brand; only automatic paths
    (require_brand=False: a suggestion accepted in one click) may leave it empty, and it then waits under "Needs a brand"."""
    check_formats(data)
    brand = brands.resolve(data.get("brand")) if data.get("brand") else ""      # checked before anything is saved
    if require_brand and not brand:
        raise store.ValidationError(brands.needed_message())
    handle = instagram_handle(data.get("instagram"))
    if handle and not str(data.get("name", "")).strip() and not str(data.get("company", "")).strip():
        data = {**data, "name": "@" + handle}                        # known only by their handle (like imported Instagram leads)
    if handle and not str(data.get("url", "")).strip():          # Instagram is their main link: it becomes the profile link too
        data = {**data, "url": f"https://www.instagram.com/{handle}/"}
        if not data.get("channel"):
            data["channel"] = "instagram"
    with _crm() as conn:
        company_name = str(data.get("company", "")).strip()
        company_id = data.get("company_id") or _company(conn, company_name, source_file="added by hand")
        if company_id and not company_name:
            row = conn.execute("SELECT company_name FROM companies WHERE company_id = ?", (company_id,)).fetchone()
            company_name = row["company_name"] if row else ""
        channel = data.get("channel") or detect_channel(data.get("url", ""))
        links = [data.get("url", "")] if channel in ("linkedin", "instagram") else []
        problem = _missing(str(data.get("name", "")), company_name, str(data.get("email", "")), str(data.get("phone", "")), links)
        if problem:
            raise store.ValidationError(problem)
        pid = _insert_person(conn, data, channel=channel, company_id=company_id, company_name=company_name, extras={},
                             source_file=data.get("source_file", "added by hand"), batch=str(data.get("batch", "")))
        _ensure_email_account(conn, pid, str(data.get("email", "")), str(data.get("name", "")) or company_name)
        if handle:
            _set_instagram(conn, pid, handle, str(data.get("name", "")) or "@" + handle)
        if brand:
            conn.execute("UPDATE people SET brand = ? WHERE person_id = ?", (brand, pid))
        made = contact_dict(conn, pid)
    if made and made.get("email"):
        _attach_mailbox_emails(pid)
        made = get_contact(pid) or made
    return made


def _attach_mailbox_emails(person_id: int) -> None:
    """Emails with them already in the mailbox (waiting because they weren't a contact yet) go into their history now."""
    import sys
    srv = sys.modules.get("server")                    # the running app (it owns the mailbox-to-CRM step)
    if srv is None or not hasattr(srv, "attach_mailbox_emails"):
        return
    try:
        srv.attach_mailbox_emails(person_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not add the mailbox emails to the new contact")


def update_contact(contact_id: int, data: dict) -> dict | None:
    check_formats(data)
    if "brand" in data and get_contact(contact_id):
        if (data.get("brand") or "") != brands.person_brand(contact_id):
            brands.set_person_brand(contact_id, data.get("brand"))       # refuses a brand that clashes with the contact's funnel
    with _crm() as conn:
        cur = conn.execute("SELECT * FROM people WHERE person_id = ?", (contact_id,)).fetchone()
        if not cur:
            return None
        sets = {k: str(data[k])[:2000] for k in PERSON_COLUMNS if k in data}
        if "raw_notes" in data:
            sets["raw_notes"] = str(data["raw_notes"])[:200000]
        if "company_id" in data:
            sets["company_id"] = int(data["company_id"]) if data["company_id"] else None
        elif "company" in data:
            sets["company_id"] = _company(conn, data["company"], source_file="added by hand")
        before = _reachable(*_person_state(conn, contact_id))
        if sets:
            conn.execute(f"UPDATE people SET {', '.join(k + ' = :' + k for k in sets)} WHERE person_id = :id", {**sets, "id": contact_id})
        if "name" in sets:
            conn.execute("UPDATE channel_accounts SET display_name = ? WHERE person_id = ?", (sets["name"], contact_id))
        if "email" in data:
            _set_email(conn, contact_id, str(data["email"]).strip(), sets.get("name") or cur["name"])
        if "url" in data:
            url = str(data["url"]).strip()
            kind = detect_channel(url)                     # a LinkedIn link updates the LinkedIn account, never the Instagram one
            acct = (conn.execute("SELECT * FROM channel_accounts WHERE person_id = ? AND channel = ? ORDER BY account_id LIMIT 1", (contact_id, kind)).fetchone() if kind else None) \
                or conn.execute("SELECT * FROM channel_accounts WHERE person_id = ? ORDER BY account_id LIMIT 1", (contact_id,)).fetchone()
            if acct:
                conn.execute("UPDATE channel_accounts SET url = ? WHERE account_id = ?", (url[:500], acct["account_id"]))
                if url and (acct["channel_identifier"] or "").startswith("name:") and acct["channel"] in ("linkedin", "instagram"):
                    conn.execute("UPDATE channel_accounts SET channel_identifier = ? WHERE account_id = ?", (_identifier(acct["channel"], url), acct["account_id"]))
            elif url:
                ch = detect_channel(url)
                name = sets.get("name") or cur["name"]
                conn.execute("INSERT INTO channel_accounts (person_id, channel, channel_identifier, display_name, url) VALUES (?, ?, ?, ?, ?)",
                             (contact_id, ch or "other", _identifier(ch, url) or f"name:{name}", name, url[:500]))
        if "instagram" in data:
            _set_instagram(conn, contact_id, instagram_handle(data["instagram"]), sets.get("name") or cur["name"])
        problem = _missing(*_person_state(conn, contact_id))
        if before and problem:
            raise store.ValidationError(problem)            # an edit may not strip a contact below the minimum
        result = contact_dict(conn, contact_id)
    try:
        sync_lead_identity(contact_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not carry a contact change into the calendar")
    if "email" in data and str(data.get("email") or "").strip():
        _attach_mailbox_emails(contact_id)
    return result


# ----------------------------------------------------------------- outreach: calendar leads <-> CRM people
def _lead_identity(lead: dict) -> tuple[str, str, str]:
    """(display name, profile url, identifier) for a calendar lead."""
    channel, url = lead["channel"], lead.get("url") or ""
    if channel == "email":
        return lead["contact"], lead["contact"], lead["contact"].strip().lower()
    if channel == "instagram":
        handle = lead["contact"].lstrip("@")
        url = url or f"https://www.instagram.com/{handle}"
        return "@" + handle, url, _identifier("instagram", url)
    return lead["contact"], url, _identifier("linkedin", url)


def link_lead(conn, lead: dict):
    """Give a calendar lead its CRM person (found by channel + profile link, else created). conn = the calendar connection."""
    if lead.get("person_id"):
        return lead["person_id"]
    name, url, ident = _lead_identity(lead)
    with _crm() as c:
        pid = _find_person(c, lead["channel"], ident, name, None) if ident else None
        if pid is None and not ident:
            pid = _find_person(c, lead["channel"], "", name, None)
        if pid is None:
            pid = _insert_person(c, {"name": name, "url": url, "notes": lead.get("notes", "")}, channel=lead["channel"], company_id=None,
                                 company_name="", extras={}, source_file="(existing leads)", batch="")
            if lead.get("project") and lead["project"] in store.brand_keys():      # the lead's brand becomes the contact's
                c.execute("UPDATE people SET brand = ? WHERE person_id = ? AND IFNULL(brand, '') = ''", (lead["project"], pid))
        if lead.get("updated_at"):       # "Added" is when the lead was added, not when its contact record was made
            c.execute("UPDATE people SET created_at = ? WHERE person_id = ?", (lead["updated_at"], pid))
    if lead.get("id"):
        conn.execute("UPDATE leads SET person_id = ? WHERE id = ?", (pid, lead["id"]))
    lead["person_id"] = pid
    return pid


def fix_added_dates() -> str:
    """One-time: contacts made from leads that already existed show the date their lead was added, not the day the contact record was made."""
    with store._conn() as cal:
        if cal.execute("SELECT 1 FROM sync_state WHERE key = 'fixed_added_dates_v1'").fetchone():
            return "already done"
        pairs = [(r["person_id"], r["first"]) for r in cal.execute(
            "SELECT person_id, MIN(updated_at) AS first FROM leads WHERE person_id IS NOT NULL AND updated_at != '' GROUP BY person_id")]
    with _crm() as c:
        for pid, first in pairs:
            c.execute("UPDATE people SET created_at = ? WHERE person_id = ? AND source_file = '(existing leads)'", (first, pid))
    with store._conn() as cal:
        cal.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('fixed_added_dates_v1', ?)", (store.now_local().isoformat(),))
    return "fixed"


def link_leads() -> int:
    """Link every calendar lead that has no person yet. Safe to run again; statuses and dates are never touched."""
    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM leads WHERE person_id IS NULL")]
        for lead in rows:
            link_lead(conn, lead)
    return len(rows)


def sync_stage(conn, person_id: int) -> None:
    """Stage and status are separate fields; the one automation between them: if the person is in a funnel, a status
    moves the card forward to the furthest stage reached by any of their statuses. Never backward, so a card placed by hand stays."""
    rows = conn.execute("""SELECT s.stage_name FROM leads l JOIN status_stages s ON s.channel = l.channel AND s.status_key = l.status
                           WHERE l.person_id = ?""", (person_id,)).fetchall()
    names = {r["stage_name"].lower() for r in rows}
    if not names:
        return
    with _crm() as c:
        p = c.execute("SELECT funnel_id, funnel_stage_id FROM people WHERE person_id = ?", (person_id,)).fetchone()
        if not p or not p["funnel_id"]:
            return
        stages = c.execute("SELECT stage_id, name, position FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (p["funnel_id"],)).fetchall()
        order = [r["stage_id"] for r in stages]
        now = order.index(p["funnel_stage_id"]) if p["funnel_stage_id"] in order else -1
        hits = [r for r in stages if r["name"].lower() in names]
        if hits and order.index(hits[-1]["stage_id"]) > now:
            c.execute("UPDATE people SET funnel_stage_id = ? WHERE person_id = ?", (hits[-1]["stage_id"], person_id))


def lost_on_stage(person_id: int) -> list:
    """Moved by hand to the stage "not interested" leads to (Lost by default; a renamed stage keeps the link): their outreach becomes
    "not interested" on each of their channels, so stage and status never disagree (and their open follow-ups close). Returns the channels set."""
    with _crm() as c:
        row = c.execute("""SELECT fs.name FROM people p JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id WHERE p.person_id = ?""",
                        (int(person_id),)).fetchone()
    if not row:
        return []
    with store._conn() as conn:
        lost_channels = {r["channel"] for r in conn.execute("SELECT channel FROM status_stages WHERE status_key = 'done' AND lower(stage_name) = ?",
                                                            (row["name"].strip().lower(),))}
    done = []
    for ch in outreach(int(person_id))["channels"]:
        if ch["channel"] in lost_channels:
            set_outreach(int(person_id), ch["channel"], "done")
            done.append(ch["channel"])
    return done


def _stage_place(person_id: int, stage_id=None) -> dict | None:
    """Where a card is (or would be): its funnel's stage names in order, the position of `stage_id` (or of its current stage), and the
    position of the conversation stage (the one "replied" leads to)."""
    with _crm() as c:
        p = c.execute("SELECT name, funnel_id, funnel_stage_id FROM people WHERE person_id = ?", (int(person_id),)).fetchone()
        if not p or not p["funnel_id"]:
            return None
        rows = c.execute("SELECT stage_id, name FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (p["funnel_id"],)).fetchall()
    ids = [r["stage_id"] for r in rows]
    names = [r["name"].strip().lower() for r in rows]
    with store._conn() as conn:
        talk = {r["stage_name"].strip().lower() for r in conn.execute("SELECT stage_name FROM status_stages WHERE status_key = 'replied'")}
    conv = next((i for i, n in enumerate(names) if n in talk), None)
    target = int(stage_id) if stage_id else p["funnel_stage_id"]
    return {"name": p["name"], "pos": ids.index(target) if target in ids else -1, "now": ids.index(p["funnel_stage_id"]) if p["funnel_stage_id"] in ids else -1,
            "conv": conv, "stage_name": rows[ids.index(target)]["name"] if target in ids else "", "funnel_id": p["funnel_id"],
            "stage_in_funnel": target in ids}


def stage_move_check(person_id: int, stage_id: int) -> str:
    """Before a hand move across the conversation line: a warning to confirm, or '' when nothing is odd.
    Forward into the conversation with no record of them writing, or back out of it while there is one."""
    from . import quiet
    place = _stage_place(person_id, stage_id)
    if not place or place["conv"] is None or not place["stage_in_funnel"]:
        return ""
    t = quiet.last_touches([int(person_id)]).get(int(person_id), {})
    wrote = bool(t.get("in") or t.get("touch"))
    old, new, conv, who, stage = place["now"], place["pos"], place["conv"], place["name"], place["stage_name"]
    if new >= conv > old and not wrote:
        return f"You're about to move {who} to {stage}, but there's no record of {who} replying. Move anyway?"
    if old >= conv > new and wrote:
        return f"You're about to move {who} back to {stage}, but there's a record of {who} writing to you. Move anyway?"
    return ""


def status_on_stage(person_id: int, allow_back: bool = False) -> dict:
    """A card moved by hand: each channel's status follows the stage, forward only, so stage and status never disagree.
    Lost -> "not interested"; In conversation -> "replied"; Confirmed / Onboarding / Completed -> those statuses. Uses the same
    status -> stage links that move a card when a status changes (a renamed stage keeps them). Returns {channel: new status}."""
    with _crm() as c:
        row = c.execute("""SELECT fs.name, p.funnel_id FROM people p JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id WHERE p.person_id = ?""",
                        (int(person_id),)).fetchone()
        if not row:
            return {}
        order = [r["name"].strip().lower() for r in c.execute("SELECT name FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (row["funnel_id"],))]
    stage = row["name"].strip().lower()
    changed = {}
    with store._conn() as conn:
        links = [dict(r) for r in conn.execute("SELECT channel, status_key, stage_name FROM status_stages")]
        leads_now = {r["channel"]: r["status"] for r in conn.execute("SELECT channel, status FROM leads WHERE person_id = ?", (int(person_id),))}

    def stage_of(channel, status):
        hit = next((l["stage_name"] for l in links if l["channel"] == channel and l["status_key"] == status), None)
        if hit is None and status in store.LATER_STATUSES:
            hit = store.LATER_STATUSES[status][0]
        return (hit or "").strip().lower()

    for ch in outreach(int(person_id))["channels"]:
        channel = ch["channel"]
        options = [l["status_key"] for l in links if l["channel"] == channel and l["stage_name"].strip().lower() == stage]
        options += [k for k, v in store.LATER_STATUSES.items() if v[0].strip().lower() == stage and k not in options]
        if not options:
            continue
        target = next((k for k in ("done", "replied", "sent", "new") if k in options), options[0])
        current = leads_now.get(channel, "new")
        if current == target:
            continue
        here = order.index(stage) if stage in order else -1
        was = order.index(stage_of(channel, current)) if stage_of(channel, current) in order else -1
        if target != "done" and was >= here and not allow_back:     # forward only, unless the card was moved back on purpose
            continue
        set_outreach(int(person_id), channel, target)
        changed[channel] = leads.label_for(channel, target)
    return changed


def lost_on_stage(person_id: int) -> list:
    return list(status_on_stage(person_id))


def sync_stages(person_ids: list) -> None:
    with store._conn() as conn:
        for pid in person_ids:
            sync_stage(conn, int(pid))


def outreach(person_id: int) -> dict:
    with _crm() as c:
        accts = [r["channel"] for r in c.execute("SELECT channel FROM channel_accounts WHERE person_id = ? ORDER BY account_id", (person_id,))]
    with store._conn() as conn:
        by_channel = {r["channel"]: dict(r) for r in conn.execute("SELECT * FROM leads WHERE person_id = ?", (person_id,))}
        out = []
        from . import quiet
        live = quiet.by_channel([int(person_id)]).get(int(person_id), {})
        for ch in dict.fromkeys([c for c in accts if c in leads.CHANNELS] + list(by_channel)):
            lead = by_channel.get(ch)
            out.append({"channel": ch, "label": leads.CHANNELS[ch]["label"], "lead_id": lead["id"] if lead else None,
                        "status": leads.label_for(ch, lead["status"]) if lead else "", "status_date": lead["status_date"] if lead else "",
                        "follow_up_date": lead["follow_up_date"] if lead else "", "options": leads.valid_labels(ch, conn),
                        "asks_follow_up": [o for o in leads.valid_labels(ch, conn) if store.bucket_for(conn, ch, leads.resolve_status(ch, o, conn)) == "cold"],
                        "conversation": quiet.conversation(live.get(ch)) if lead and _talking(conn, ch, lead["status"]) else None})
        return {"channels": out}


def _ensure_lead(person_id: int, channel: str) -> int:
    """The person's calendar lead on this channel, created with status 'new' if it doesn't exist yet."""
    if channel not in leads.CHANNELS:
        raise store.ValidationError("channel must be linkedin or instagram")
    with _crm() as c:
        person = c.execute("SELECT name FROM people WHERE person_id = ?", (person_id,)).fetchone()
        acct = c.execute("SELECT * FROM channel_accounts WHERE person_id = ? AND channel = ? ORDER BY account_id LIMIT 1", (person_id, channel)).fetchone()
        if channel == "email":                        # a name placeholder can come first; the real address is the one with an @
            acct = c.execute("""SELECT * FROM channel_accounts WHERE person_id = ? AND channel = 'email' AND channel_identifier LIKE '%@%'
                                ORDER BY account_id LIMIT 1""", (person_id,)).fetchone() or acct
    if not person:
        raise store.ValidationError("no such person")
    with store._conn() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE person_id = ? AND channel = ?", (person_id, channel)).fetchone()
        if lead is not None:
            return lead["id"]
        if channel == "email":
            address = (acct["channel_identifier"] if acct and "@" in (acct["channel_identifier"] or "") else "")
            if not address:
                raise store.ValidationError("add the person's email address first")
            contact, url = address, address
        elif channel == "instagram":
            handle = (acct["channel_identifier"] if acct and not (acct["channel_identifier"] or "").startswith("name:") else "")
            if not handle:
                raise store.ValidationError("add the person's Instagram link first")
            contact, url = "@" + handle, (acct["url"] or f"https://www.instagram.com/{handle}")
        else:
            contact, url = person["name"], (acct["url"] if acct else "") or ""
        existing = conn.execute("SELECT id FROM leads WHERE channel = ? AND contact = ?", (channel, contact)).fetchone()
        if existing:
            conn.execute("UPDATE leads SET person_id = ? WHERE id = ?", (person_id, existing["id"]))
            return existing["id"]
        return conn.execute("""INSERT INTO leads (channel, project, contact, status, status_date, follow_up_date, url, notes, updated_at, person_id)
                               VALUES (?, ?, ?, 'new', ?, '', ?, '', ?, ?)""",
                            (channel, brands.lead_project(person_id, channel), contact, store.now_local().date().isoformat(), url,
                             store.now_local().isoformat(), person_id)).lastrowid


def set_outreach(person_id: int, channel: str, status: str, follow_up_days=None) -> dict:
    lead_id = _ensure_lead(person_id, channel)
    result = leads.set_status(lead_id, status, follow_up_days=follow_up_days)
    return {"lead_id": lead_id, "status": result["status_label"], "changed": result["changed"], "asks_follow_up": result.get("asks_follow_up", False)}


def ensure_leads(person_ids: list) -> None:
    """Everyone in a funnel has a status on each of their channels; a fresh one starts at 'new' (no task)."""
    for pid in person_ids:
        with _crm() as c:
            channels = [r["channel"] for r in c.execute("SELECT channel FROM channel_accounts WHERE person_id = ?", (int(pid),))]
        for ch in dict.fromkeys(channels):
            if ch in leads.CHANNELS:
                try:
                    _ensure_lead(int(pid), ch)
                except store.ValidationError:
                    pass                              # e.g. an Instagram account with no link yet


def _talking(conn, channel: str, status: str) -> bool:
    """Past outreach: they replied (or it went further: call booked, confirmed ...). From here the live conversation tells more than a status."""
    return store.bucket_for(conn, channel, status) == "warm" or status in store.LATER_STATUSES


def statuses_for(person_ids: list) -> dict:
    """Each person's line per channel: the outreach status, or once they replied, the live conversation (waiting on you / them, since when)."""
    if not person_ids:
        return {}
    from . import quiet
    ph = ",".join("?" for _ in person_ids)
    out: dict = {}
    live = quiet.by_channel([int(i) for i in person_ids])
    with store._conn() as conn:
        for r in conn.execute(f"SELECT person_id, channel, status FROM leads WHERE person_id IN ({ph}) ORDER BY id", [int(i) for i in person_ids]):
            item = {"channel": r["channel"], "label": leads.CHANNELS[r["channel"]]["label"], "status": leads.label_for(r["channel"], r["status"])}
            if _talking(conn, r["channel"], r["status"]):
                item["conversation"] = quiet.conversation(live.get(r["person_id"], {}).get(r["channel"]))
            out.setdefault(r["person_id"], []).append(item)
    return out


def migrate_funnel_stages() -> str:
    """One-time: existing funnels get the new stage list (Decision pending -> Confirmed, Won -> Completed, plus Onboarding). People stay where they are."""
    from datetime import datetime
    with store._conn() as cal:
        if cal.execute("SELECT 1 FROM sync_state WHERE key = 'migrated_stages_v2'").fetchone():
            return "already done"
    with _crm() as c:
        for f in c.execute("SELECT funnel_id FROM funnels").fetchall():
            fid = f["funnel_id"]
            c.execute("UPDATE funnel_stages SET name = 'Confirmed' WHERE funnel_id = ? AND name = 'Decision pending'", (fid,))
            c.execute("UPDATE funnel_stages SET name = 'Completed' WHERE funnel_id = ? AND name = 'Won'", (fid,))
            if not c.execute("SELECT 1 FROM funnel_stages WHERE funnel_id = ? AND name = 'Onboarding'", (fid,)).fetchone():
                ps = c.execute("SELECT position FROM funnel_stages WHERE funnel_id = ? AND name = 'Completed'", (fid,)).fetchone()
                if ps:
                    c.execute("UPDATE funnel_stages SET position = position + 1 WHERE funnel_id = ? AND position >= ?", (fid, ps["position"]))
                    c.execute("INSERT INTO funnel_stages (funnel_id, name, position, created_at) VALUES (?, 'Onboarding', ?, ?)", (fid, ps["position"], datetime.now().isoformat()))
    with store._conn() as cal:
        cal.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('migrated_stages_v2', ?)", (store.now_local().isoformat(),))
    return "migrated"


def rename_stage_mapping(old: str, new: str) -> None:
    """A stage was renamed: the statuses that belonged to it follow the new name."""
    with store._conn() as cal:
        cal.execute("UPDATE status_stages SET stage_name = ? WHERE lower(stage_name) = lower(?)", (new, old))


def fix_email_urls() -> int:
    """Email accounts never had a profile link: remove the address that an earlier version also stored there."""
    with _crm() as conn:
        return conn.execute("UPDATE channel_accounts SET url = '' WHERE channel = 'email' AND url != ''").rowcount


SENT_STATUS = {"email": "email sent", "linkedin": "message sent"}      # what "we already wrote to them" means on each channel


def apply_history_status(person_ids: list) -> None:
    """People who were already written to before they entered a funnel: their status on that channel goes from "New" to "email sent" (or
    "message sent" on LinkedIn), dated by the last message they were sent, so the follow-up task counts from then. Statuses that are not "New" are never touched."""
    from . import leads
    for pid in person_ids:
        try:
            with _crm() as conn:
                crm_msgs = [dict(r) for r in conn.execute("SELECT channel, direction, timestamp FROM messages WHERE person_id = ?", (pid,))]
            everything = messages_with_saved_emails(pid, crm_msgs)           # includes the emails saved in the Gmail tab
            for channel, label in SENT_STATUS.items():
                sent = [d for d in (_when(m.get("timestamp")) for m in everything if m.get("channel") == channel and m.get("direction") == "outgoing") if d]
                if not sent:
                    continue
                with store._conn() as cal:
                    lead = cal.execute("SELECT id, status FROM leads WHERE person_id = ? AND channel = ?", (pid, channel)).fetchone()
                if not lead or lead["status"] != "new":
                    continue
                leads.set_status(lead["id"], label)
                with store._conn() as cal:                                  # the status is dated by the real send, not by today
                    cal.execute("UPDATE leads SET status_date = ? WHERE id = ?", (max(sent).date().isoformat(), lead["id"]))
                    row = dict(cal.execute("SELECT * FROM leads WHERE id = ?", (lead["id"],)).fetchone())
                    leads.apply_rules(cal, row)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("could not set the status from the messages already sent to person %s", pid)


FIRST_EMAIL_FOLLOW_UP_DAYS = {True: 3, False: 7}      # in a funnel: 3 days; a contact in no funnel is less urgent: 7
FIRST_EMAIL_RECENT_DAYS = 14                          # an older email found later (e.g. a first sync) starts nothing


def first_email_follow_up(person_id: int, sent_at: str | None = None) -> bool:
    """You emailed a contact who has never written back (from LyteKeepr, Claude or your mail app): their email status becomes
    "email sent" with a follow-up, 3 days if they're in a funnel, else 7. Not when they ever wrote to you, when their email
    status is already past New, or for an email more than two weeks old. A reply clears it as usual. True when it set one."""
    age = 0
    if sent_at:
        try:
            when = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
            age = max(0, (datetime.now(when.tzinfo) - when).days)
            if age > FIRST_EMAIL_RECENT_DAYS:
                return False
        except ValueError:
            pass
    with _crm() as c:
        row = c.execute("SELECT funnel_id FROM people WHERE person_id = ?", (int(person_id),)).fetchone()
        if not row or c.execute("SELECT 1 FROM messages WHERE person_id = ? AND direction = 'incoming' LIMIT 1", (int(person_id),)).fetchone():
            return False
    with store._conn() as cal:
        lead = cal.execute("SELECT status FROM leads WHERE person_id = ? AND channel = 'email'", (int(person_id),)).fetchone()
    if lead is not None and lead["status"] != "new":
        return False
    try:
        days = FIRST_EMAIL_FOLLOW_UP_DAYS[bool(row["funnel_id"])] - age      # counted from the day it was sent; already due = tomorrow
        set_outreach(int(person_id), "email", "email sent", max(1, days))
    except store.ValidationError:
        return False
    return True


def mark_email_sent(person_id: int, follow_up_days: int | None = None) -> None:
    """After an email goes out: a person who is in a funnel gets the status "email sent" on the email channel (only from New: never downgrades).
    follow_up_days (the user's answer for a batch): N > 0 = a follow-up in N days, 0 = none; None = not asked.
    Not asked and a first email to someone who never wrote back: the first-email follow-up (3 or 7 days)."""
    if follow_up_days is None and first_email_follow_up(person_id):
        return
    with _crm() as conn:
        row = conn.execute("SELECT funnel_id FROM people WHERE person_id = ?", (person_id,)).fetchone()
    if not row or not row["funnel_id"]:
        return
    with store._conn() as cal:
        lead = cal.execute("SELECT id, status FROM leads WHERE person_id = ? AND channel = 'email'", (person_id,)).fetchone()
    if lead is None or lead["status"] == "new":
        set_outreach(person_id, "email", "email sent", follow_up_days)
    elif follow_up_days:                                   # already further along: the follow-up the user asked for still comes
        leads.choose_follow_up(lead["id"], int(follow_up_days))


def backfill_from_accounts() -> int:
    """LinkedIn / Instagram records that kept only a handle get their profile link filled in (in the channel record, where links live)."""
    n = 0
    with _crm() as conn:
        for a in conn.execute("SELECT account_id, channel, channel_identifier, url FROM channel_accounts WHERE channel IN ('linkedin', 'instagram') AND IFNULL(url, '') = ''").fetchall():
            link = _account_link(a)
            if link:
                conn.execute("UPDATE channel_accounts SET url = ? WHERE account_id = ?", (link, a["account_id"]))
                n += 1
    return n


# ----------------------------------------------------------------- possible duplicates: found, shown to the user, merged only when they say so
def _dismissed() -> set:
    try:
        return set(json.loads(store.get_setting("dup_dismissed") or "[]"))
    except ValueError:
        return set()


def dismiss_duplicates(ids: list) -> None:
    """"Not the same person": remember it, so this group is not suggested again."""
    key = "-".join(str(i) for i in sorted(int(i) for i in ids))
    d = _dismissed(); d.add(key)
    store.set_setting("dup_dismissed", json.dumps(sorted(d)))


def _sim(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def _name_key(name: str) -> str:
    """A name with capitals, punctuation and word order ignored ("Smith, John" = "john smith")."""
    return " ".join(sorted(re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()))


def _near_pairs(info: dict, accounts: list) -> list:
    """Pairs of different contacts that are probably the same person with a typo: [(id, id, reason)]. Only a suggestion; nothing is merged."""
    out, seen = [], set()
    def put(a, b, why):
        if a != b and (min(a, b), max(a, b)) not in seen:
            seen.add((min(a, b), max(a, b)))
            out.append((min(a, b), max(a, b), why))
    by_domain: dict = {}
    by_social: dict = {}
    for a in accounts:
        ident = (a["channel_identifier"] or "").strip().lower()
        if not ident or ident.startswith("name:") or a["person_id"] not in info:
            continue
        if a["channel"] == "email" and "@" in ident:
            by_domain.setdefault(ident.split("@", 1)[1], []).append((a["person_id"], ident))
        elif a["channel"] in ("linkedin", "instagram") and len(ident) >= 8:
            by_social.setdefault(a["channel"], []).append((a["person_id"], ident))
    for group in by_domain.values():
        for i, (p1, e1) in enumerate(group):
            for p2, e2 in group[i + 1:]:
                l1, l2 = e1.split("@")[0], e2.split("@")[0]
                if l1 != l2 and min(len(l1), len(l2)) >= 5 and _sim(l1, l2) >= 0.86:
                    put(p1, p2, f"a similar email address ({e1} and {e2})")
    for ch, group in by_social.items():
        for i, (p1, s1) in enumerate(group):
            for p2, s2 in group[i + 1:]:
                if s1 != s2 and _sim(s1, s2) >= 0.92:
                    put(p1, p2, f"a similar {'LinkedIn link' if ch == 'linkedin' else 'Instagram handle'} ({s1} and {s2})")
    if len(info) <= 3000:
        keyed = [(pid, _name_key(r["name"]), _norm_company(r["company_name"] or "")) for pid, r in info.items() if len(_name_key(r["name"])) >= 5]
        for i, (p1, n1, c1) in enumerate(keyed):
            for p2, n2, c2 in keyed[i + 1:]:
                if abs(len(n1) - len(n2)) > 3:
                    continue
                ns = _sim(n1, n2)
                if c1 and c2:
                    if ns >= 0.85 and (c1 == c2 or _sim(c1, c2) >= 0.85) and not (n1 == n2 and c1 == c2):
                        put(p1, p2, f'a similar name ("{info[p1]["name"]}" and "{info[p2]["name"]}") in the same company')
                elif 0.92 <= ns < 1.0:
                    put(p1, p2, f'a similar name ("{info[p1]["name"]}" and "{info[p2]["name"]}")')
    return out


def find_duplicate_groups() -> list[dict]:
    """Groups of contacts that look like the same person: same email (any capitalisation), LinkedIn link, Instagram handle, phone number,
    the same name in the same company, or a near match (a typo in the email, handle or name). Nothing is merged here."""
    with _crm() as conn:
        info = {}
        for r in conn.execute("""SELECT p.person_id, p.name, p.phone, p.company_id, p.source_file, p.created_at, p.funnel_id,
                                        IFNULL(length(p.notes), 0) AS n, IFNULL(length(p.raw_notes), 0) AS rn, co.company_name,
                                        (SELECT COUNT(*) FROM messages m WHERE m.person_id = p.person_id) AS msgs
                                 FROM people p LEFT JOIN companies co ON co.company_id = p.company_id"""):
            info[r["person_id"]] = dict(r)
        keys: dict = {}
        def add(kind, key, pid, label):
            if key:
                keys.setdefault((kind, key), {"label": label, "ids": set()})["ids"].add(pid)
        for a in conn.execute("SELECT person_id, channel, channel_identifier FROM channel_accounts"):
            ident = (a["channel_identifier"] or "").strip().lower()
            if not ident or ident.startswith("name:") or a["person_id"] not in info:
                continue
            add(a["channel"], ident, a["person_id"], {"email": "email address", "linkedin": "LinkedIn link", "instagram": "Instagram handle"}.get(a["channel"], a["channel"]) + " " + ident)
            if a["channel"] == "email":
                info[a["person_id"]].setdefault("email", a["channel_identifier"])
        for pid, r in info.items():
            add("phone", _phone_key(r["phone"]), pid, "phone number " + (r["phone"] or ""))
            if (r["name"] or "").strip() and r["company_id"]:
                add("namecompany", ((r["name"] or "").strip().lower(), r["company_id"]), pid, f'the same name ("{r["name"]}") in the same company')
        for a, b, why in _near_pairs(info, [dict(x) for x in conn.execute("SELECT person_id, channel, channel_identifier FROM channel_accounts")]):
            add("near", (a, b), a, why)
            add("near", (a, b), b, why)
    # people linked by any shared key form one group
    parent = {pid: pid for pid in info}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for (_, _), v in keys.items():
        ids = sorted(v["ids"])
        if len(ids) < 2:
            continue
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    comps: dict = {}
    for pid in info:
        comps.setdefault(find(pid), []).append(pid)
    skip = _dismissed()
    out = []
    for ids in comps.values():
        if len(ids) < 2 or "-".join(str(i) for i in sorted(ids)) in skip:
            continue
        why = sorted({lbl for k, v in keys.items() if v["ids"] & set(ids) and len(v["ids"]) > 1 for lbl in [v["label"]]})
        out.append({"ids": sorted(ids), "reasons": why, "people": [
            {"id": i, "name": info[i]["name"] or "", "company": info[i]["company_name"] or "", "email": info[i].get("email", ""),
             "added": "added by hand" if info[i]["source_file"] == "added by hand" else ("imported" if info[i]["source_file"] else "from your email or LinkedIn sync"),
             "created_at": info[i]["created_at"] or "", "messages": info[i]["msgs"], "has_notes": bool(info[i]["n"] or info[i]["rn"]), "in_funnel": bool(info[i]["funnel_id"])}
            for i in sorted(ids)]})
    return out


def merge_contacts(keep_id: int, merge_id: int) -> None:
    """Merge one contact into another without losing anything: the kept contact fills its blanks from the other, the notes are joined,
    messages and channels move over, the calendar statuses follow, and the other contact is removed."""
    if keep_id == merge_id:
        raise store.ValidationError("choose two different contacts")
    FILL = ["job_title", "headline", "location", "industry", "audience", "phone", "telegram", "website", "verified", "date_found"]
    with _crm() as conn:
        keep = conn.execute("SELECT * FROM people WHERE person_id = ?", (keep_id,)).fetchone()
        other = conn.execute("SELECT * FROM people WHERE person_id = ?", (merge_id,)).fetchone()
        if not keep or not other:
            raise store.ValidationError("one of those contacts no longer exists")
        sets = {c: other[c] for c in FILL if not (keep[c] or "").strip() and (other[c] or "").strip()}
        if not (keep["name"] or "").strip() and (other["name"] or "").strip():
            sets["name"] = other["name"]
        for col in ("notes", "raw_notes"):
            a, b = (keep[col] or "").strip(), (other[col] or "").strip()
            if b and b not in a:
                sets[col] = (a + "\n\n— From the merged contact —\n" if a else "") + b
        if not keep["funnel_id"] and other["funnel_id"]:
            sets["funnel_id"], sets["funnel_stage_id"] = other["funnel_id"], other["funnel_stage_id"]
        if sets:
            conn.execute(f"UPDATE people SET {', '.join(k + ' = :' + k for k in sets)} WHERE person_id = :id", {**sets, "id": keep_id})
        crm.merge_people(conn, keep_id, merge_id)
        # the same address / handle can now be there twice on one person: keep one, point the messages at it
        seen: dict = {}
        for a in conn.execute("SELECT account_id, channel, channel_identifier FROM channel_accounts WHERE person_id = ? ORDER BY account_id", (keep_id,)).fetchall():
            k = (a["channel"], (a["channel_identifier"] or "").lower())
            if k in seen:
                conn.execute("UPDATE messages SET account_id = ? WHERE account_id = ?", (seen[k], a["account_id"]))
                conn.execute("DELETE FROM channel_accounts WHERE account_id = ?", (a["account_id"],))
            else:
                seen[k] = a["account_id"]
    with store._conn() as cal:        # statuses and tasks of the merged contact move to the kept one (one status per channel)
        for lead in [dict(r) for r in cal.execute("SELECT id, channel FROM leads WHERE person_id = ?", (merge_id,))]:
            if cal.execute("SELECT 1 FROM leads WHERE person_id = ? AND channel = ?", (keep_id, lead["channel"])).fetchone():
                for ev in cal.execute("SELECT id FROM events WHERE lead_ref = ?", (f"lead:{lead['id']}",)).fetchall():
                    cal.execute("DELETE FROM completions WHERE event_id = ?", (ev["id"],))
                cal.execute("DELETE FROM events WHERE lead_ref = ?", (f"lead:{lead['id']}",))
                cal.execute("DELETE FROM lead_history WHERE lead_id = ?", (lead["id"],))
                cal.execute("DELETE FROM leads WHERE id = ?", (lead["id"],))
            else:
                cal.execute("UPDATE leads SET person_id = ? WHERE id = ?", (keep_id, lead["id"]))
    try:
        from . import interactions
        interactions.merged(keep_id, merge_id)          # notes and logged conversations move over
        sync_lead_identity(keep_id)
        from assistant import email_send_flow
        email_send_flow.repoint_drafts(merge_id, keep_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not finish carrying a merged contact into the calendar and drafts")


# ----------------------------------------------------------------- emails saved in the LyteKeepr Gmail tab, shown on the matching contact
def _when(ts: str):
    """A comparable local-time moment from a stored timestamp (some are stored with a time zone, some without), or None."""
    from datetime import datetime
    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def messages_with_saved_emails(person_id: int, crm_messages: list) -> list:
    """The contact's messages plus the emails saved in the LyteKeepr Gmail tab (the saved index, nothing else) that were sent to or from the
    contact's email address and are not already among their messages. Oldest first."""
    try:
        with _crm() as conn:
            addresses = {r["channel_identifier"].strip().lower() for r in conn.execute(
                "SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email' AND channel_identifier LIKE '%@%'", (person_id,))}
        if not addresses:
            return crm_messages
        already = set()
        with _crm() as conn:
            already = {r[0] for r in conn.execute("SELECT external_message_id FROM messages WHERE person_id = ? AND external_message_id IS NOT NULL", (person_id,))}
        from gmail import storage
        extra = []
        for account_id in storage.list_account_ids():
            for message_id, entry in storage.load_index(account_id).items():
                if message_id in already:
                    continue
                sender = ((entry.get("from") or {}).get("email") or "").strip().lower()
                recipients = {((t or {}).get("email") or "").strip().lower() for t in (entry.get("to") or [])}
                if sender not in addresses and not (recipients & addresses):
                    continue
                incoming = sender in addresses
                full = storage.load_message(account_id, message_id) or {}
                body = (full.get("textBody") or entry.get("snippet") or "").strip()
                subject = entry.get("subject") or ""
                extra.append({
                    "message_id": f"saved-email:{message_id}", "channel": "email",
                    "sender": ((entry.get("from") or {}).get("name") or sender) if incoming else "You",
                    "direction": "incoming" if incoming else "outgoing",
                    "message_text": (f"Subject: {subject}\n\n" if subject else "") + body[:4000],
                    "timestamp": entry.get("receivedAt") or full.get("sentAt") or "",
                })
        if not extra:
            return crm_messages
        merged = list(crm_messages) + extra
        def moment(m):
            d = _when(m.get("timestamp"))
            return d.timestamp() if d else 0.0       # no usable time: first
        merged.sort(key=moment)
        return merged
    except Exception:
        import logging
        logging.getLogger(__name__).exception("could not add the saved emails to a contact")
        return crm_messages
