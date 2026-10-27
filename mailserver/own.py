"""The workspace's own mail server as its mailbox (instead of Gmail): IMAP to read the Inbox and Sent folders, SMTP to send (for
example through Brevo), and a copy of every sent email saved in Sent so the user's mail app shows it too.

It fills the same saved copy (gmail.storage) in the same format as the Gmail sync, so everything that reads emails (contacts'
history, search, Who is waiting, the scheduled run, drafts, funnel emails, the phone's Email tab) works unchanged.
- Threads come from the email headers (References / In-Reply-To / Message-ID); each email keeps its Message-ID for replies.
- Newsletters, mailing lists and automated mail (List-Unsubscribe, List-Id, Precedence: bulk, Auto-Submitted) are kept out the
  way Gmail's Promotions tab is.
- First sync: the last 90 days; after that only new emails (the server's UIDs are the bookmark, per folder).
- The connection lives per workspace in its "mail" folder; passwords are encrypted with the server's secret key.
"""

import base64
import email
import hashlib
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime

from gmail import storage
from profiles.store import profile_component_dir

log = logging.getLogger(__name__)
FIRST_SYNC_DAYS = 90
SENT_NAMES = ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages", "INBOX.Sent Items")
FIELDS = ("address", "from_name", "imap_host", "imap_port", "imap_user", "smtp_host", "smtp_port", "smtp_user")


class MailError(Exception):
    pass


# ------------------------------------------------------------------ the connection (per workspace, passwords encrypted)
def _path():
    return profile_component_dir("mail") / "own-server.json"


def _fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get("LYTEKEEPR_SECRET_KEY", "") or "lytekeepr"
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("lk-mail:" + secret).encode()).digest()))


def load() -> dict | None:
    try:
        cfg = json.loads(_path().read_text())
    except (OSError, ValueError):
        return None
    for k in ("imap_password", "smtp_password"):
        if cfg.get(k):
            try:
                cfg[k] = _fernet().decrypt(cfg[k].encode()).decode()
            except Exception:
                cfg[k] = ""
    return cfg


def is_enabled() -> bool:
    cfg = load()
    return bool(cfg and cfg.get("enabled") and cfg.get("address"))


def address() -> str:
    cfg = load() or {}
    return (cfg.get("address") or "").strip().lower()


def public() -> dict:
    """The connection as Settings shows it: never the passwords, only whether they are set."""
    cfg = load()
    if not cfg:
        return {"connected": False}
    return {"connected": bool(cfg.get("enabled")), **{k: cfg.get(k, "") for k in FIELDS},
            "imap_password_set": bool(cfg.get("imap_password")), "smtp_password_set": bool(cfg.get("smtp_password")),
            "sent_folder": cfg.get("sent_folder", ""), "last_sync": (storage.load_sync_state(cfg["address"]) or {}).get("lastScanAt", "") if cfg.get("address") else ""}


def save(data: dict, enabled: bool = True) -> dict:
    """Keep the connection; a blank password keeps the one already saved."""
    old = load() or {}
    cfg = {k: str(data.get(k, old.get(k, "")) or "").strip() for k in FIELDS}
    cfg["address"] = cfg["address"].lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", cfg["address"]):
        raise MailError("Your email address doesn't look right.")
    for k in ("imap_host", "imap_user", "smtp_host", "smtp_user"):
        if not cfg[k]:
            raise MailError(f"Fill in {k.replace('_', ' ').replace('imap', 'IMAP').replace('smtp', 'SMTP')}.")
    for k, default in (("imap_port", "993"), ("smtp_port", "587")):
        cfg[k] = cfg[k] or default
        if not cfg[k].isdigit():
            raise MailError(f"{k.replace('_', ' ').upper()} must be a number.")
    for k in ("imap_password", "smtp_password"):
        raw = str(data.get(k) or "") or old.get(k, "")
        if not raw:
            raise MailError(f"Fill in the {k.split('_')[0].upper()} password.")
        cfg[k] = _fernet().encrypt(raw.encode()).decode()
    cfg["sent_folder"] = old.get("sent_folder", "") if old.get("imap_host") == cfg["imap_host"] else ""
    cfg["enabled"] = bool(enabled)
    _path().write_text(json.dumps(cfg, indent=2))
    try:
        _path().chmod(0o600)
    except OSError:
        pass
    return public()


def disconnect() -> dict:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
    return {"connected": False}


# ------------------------------------------------------------------ talking to the server
def _imap(cfg):
    port = int(cfg.get("imap_port") or 993)
    try:
        if port == 993:
            m = imaplib.IMAP4_SSL(cfg["imap_host"], port, ssl_context=ssl.create_default_context(), timeout=30)
        else:
            m = imaplib.IMAP4(cfg["imap_host"], port, timeout=30)
            m.starttls(ssl_context=ssl.create_default_context())
        m.login(cfg["imap_user"], cfg["imap_password"])
        return m
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(f"Couldn't log in to the mail server (IMAP {cfg.get('imap_host')}:{port}): {exc}") from exc


def _smtp(cfg):
    port = int(cfg.get("smtp_port") or 587)
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ssl.create_default_context(), timeout=30)
        else:
            s = smtplib.SMTP(cfg["smtp_host"], port, timeout=30)
            s.starttls(context=ssl.create_default_context())
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        return s
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"Couldn't log in to the sending server (SMTP {cfg.get('smtp_host')}:{port}): {exc}") from exc


def _q(folder: str) -> str:
    return '"' + folder.replace('"', '\\"') + '"'


def _sent_folder(m, cfg) -> str:
    if cfg.get("sent_folder"):
        return cfg["sent_folder"]
    typ, rows = m.list()
    names = []
    for row in rows or []:
        line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
        name = line.rsplit(' "', 1)[-1].rstrip('"') if '"' in line else line.split()[-1]
        names.append(name)
        if "\\Sent" in line:
            return name
    for n in SENT_NAMES:
        if n in names:
            return n
    return "Sent"


# ------------------------------------------------------------------ an email in LyteKeepr's format
def _dec(value) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _addrs(value) -> list:
    return [{"name": _dec(n), "email": e.strip().lower()} for n, e in getaddresses([_dec(value)]) if e and "@" in e]


def _hid(text: str) -> str:
    return "m" + hashlib.sha1((text or "").strip().encode()).hexdigest()[:15]


def _noise(msg) -> bool:
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    return bool(auto) and auto != "no"


def normalize(raw: bytes, label: str, account: str) -> dict:
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip() or f"<{hashlib.sha1(raw).hexdigest()[:20]}@lytekeepr.local>"
    refs = (msg.get("References") or "").split()
    root = refs[0] if refs else ((msg.get("In-Reply-To") or "").strip() or message_id)
    try:
        sent = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat()
    except Exception:
        sent = datetime.now(timezone.utc).isoformat()
    text, html, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        if "attachment" in disp or part.get_filename():
            payload = part.get_payload(decode=True) or b""
            attachments.append({"filename": _dec(part.get_filename() or "attachment"), "mimeType": ctype, "size": len(payload)})
            continue
        try:
            body = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        (text if ctype == "text/plain" else html if ctype == "text/html" else []).append(body)
    text_body = "\n".join(text).strip()
    if not text_body and html:
        text_body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", "\n".join(html))).strip()
    labels = [label] + (["CATEGORY_PROMOTIONS"] if label == "INBOX" and _noise(msg) else [])
    return {"id": _hid(message_id), "threadId": _hid(root), "accountId": account, "historyId": "", "messageIdHeader": message_id,
            "from": (_addrs(msg.get("From")) or [{"name": "", "email": ""}])[0], "to": _addrs(msg.get("To")), "cc": _addrs(msg.get("Cc")),
            "bcc": [], "subject": _dec(msg.get("Subject")), "receivedAt": sent, "sentAt": sent,
            "snippet": re.sub(r"\s+", " ", text_body)[:200], "textBody": text_body, "htmlBody": "\n".join(html).strip(),
            "labelIds": labels, "isUnread": False, "isStarred": False, "hasAttachments": bool(attachments), "attachments": attachments,
            "syncedAt": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ syncing (the same result as the Gmail scan)
def scan(since_date: str | None = None) -> dict:
    cfg = load()
    if not cfg or not cfg.get("enabled"):
        raise MailError("No mail server is connected.")
    account = cfg["address"].lower()
    state = storage.load_sync_state(account) or {}
    cursors = state.get("imap") or {}
    scan_state = storage.load_scan_state(account)
    known = set(scan_state) | set(storage.load_index(account))
    since = date.fromisoformat(since_date) if since_date else date.today() - timedelta(days=FIRST_SYNC_DAYS)
    new_ids, scanned, now = [], 0, datetime.now(timezone.utc).isoformat()
    m = _imap(cfg)
    try:
        sent_name = _sent_folder(m, cfg)
        if sent_name != cfg.get("sent_folder"):
            raw = json.loads(_path().read_text())
            raw["sent_folder"] = sent_name
            _path().write_text(json.dumps(raw, indent=2))
        for folder, label in (("INBOX", "INBOX"), (sent_name, "SENT")):
            typ, _ = m.select(_q(folder), readonly=True)
            if typ != "OK":
                continue
            uv = (m.response("UIDVALIDITY")[1] or [b"0"])[0]
            uv = uv.decode() if isinstance(uv, bytes) else str(uv)
            cur = cursors.get(label) or {}
            if cur.get("uidvalidity") == uv and not since_date:
                typ, data = m.uid("search", None, f"UID {int(cur.get('last_uid', 0)) + 1}:*")
            else:
                typ, data = m.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
            uids = [int(u) for u in (data[0].split() if data and data[0] else [])]
            uids = [u for u in uids if u > int(cur.get("last_uid", 0) if cur.get("uidvalidity") == uv else 0)]
            last = int(cur.get("last_uid", 0)) if cur.get("uidvalidity") == uv else 0
            for uid in sorted(uids):
                typ, parts = m.uid("fetch", str(uid), "(RFC822)")
                raw_bytes = next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None)
                last = max(last, uid)
                if not raw_bytes:
                    continue
                scanned += 1
                msg = normalize(raw_bytes, label, account)
                if msg["id"] in known:
                    continue
                known.add(msg["id"])
                people = {msg["from"].get("email", "")} | {x["email"] for x in msg["to"] + msg["cc"]}
                if people <= {account, ""}:                 # only you on it (a test, a note to self): not a contact's email
                    continue
                display = {"from": msg["from"], "subject": msg["subject"], "receivedAt": msg["receivedAt"], "snippet": msg["snippet"]}
                if "CATEGORY_PROMOTIONS" in msg["labelIds"]:
                    storage.upsert_scan_entry(account, msg["id"], {"status": "not_relevant", "isRelevant": False, "lastScannedAt": now,
                                                                    "reason": "Newsletter, mailing list or automated mail: skipped without review.", **display})
                    continue
                storage.save_pending(account, msg)
                storage.upsert_scan_entry(account, msg["id"], {"status": "pending_review", "isRelevant": True, "reason": "", "lastScannedAt": now, **display})
                new_ids.append(msg["id"])
            cursors[label] = {"uidvalidity": uv, "last_uid": last}
    finally:
        try:
            m.logout()
        except Exception:
            pass
    new_state = {"accountId": account, "accountEmail": account, "lastScanAt": now, "status": "success", "provider": "own_server",
                 "messageCount": len(storage.load_index(account)), "imap": cursors}
    storage.save_sync_state(account, new_state)
    return {"account_id": account, "scanned": scanned, "new_candidates": len(new_ids), "new_candidate_ids": new_ids,
            "total_pending": len(storage.get_pending_review_ids(account)), "deleted": 0, "fell_back_to_full_scan": False,
            "reason": None, "state": new_state}


# ------------------------------------------------------------------ sending
def send(to: str, subject: str, body: str, in_reply_to: str | None = None) -> dict:
    """Send one email (SMTP), then keep a copy in Sent (IMAP). Returns {"id", "threadId", "messageIdHeader"} like a Gmail send."""
    cfg = load()
    if not cfg or not cfg.get("enabled"):
        raise MailError("No mail server is connected.")
    msg = MIMEText(body or "", "plain", "utf-8")
    msg["From"] = formataddr((cfg.get("from_name") or "", cfg["address"]))
    msg["To"] = to
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["address"].split("@", 1)[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    s = _smtp(cfg)
    try:
        s.send_message(msg)
    except smtplib.SMTPException as exc:
        raise MailError(f"The sending server refused it: {exc}") from exc
    finally:
        try:
            s.quit()
        except Exception:
            pass
    try:                                              # the copy in Sent: a failure here never undoes the send
        m = _imap(cfg)
        try:
            m.append(_q(_sent_folder(m, cfg)), "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        finally:
            m.logout()
    except Exception:
        log.exception("sent, but could not save a copy in the Sent folder")
    return {"id": _hid(msg["Message-ID"]), "threadId": _hid(in_reply_to or msg["Message-ID"]), "messageIdHeader": msg["Message-ID"]}


def message_id_header(mid: str) -> str:
    """The Message-ID of a saved email (to reply in its thread)."""
    account = address()
    full = storage.load_message(account, mid) or storage.load_pending(account, mid) or {}
    return full.get("messageIdHeader") or ""


def test(send_to_self: bool = True) -> dict:
    """Log in to both servers; optionally send a test email to yourself."""
    cfg = load()
    if not cfg:
        raise MailError("Save the connection first.")
    m = _imap(cfg)
    try:
        sent_name = _sent_folder(m, cfg)
    finally:
        m.logout()
    s = _smtp(cfg)
    s.quit()
    out = {"imap": "ok", "smtp": "ok", "sent_folder": sent_name}
    if send_to_self:
        send(cfg["address"], "LyteKeepr test", "This is a test email from LyteKeepr: your mail server is connected.")
        out["test_email"] = f"sent to {cfg['address']}"
    return out


def register(app):
    from flask import jsonify, request

    def admin_only(fn, *a):
        from lk_calendar import users
        if not users.is_admin():
            return jsonify({"error": "Only the admin connects the mailbox."}), 403
        try:
            return jsonify(fn(*a))
        except MailError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/mail/own")
    def own_mail_get():
        return admin_only(public)

    @app.put("/api/mail/own")
    def own_mail_save():
        return admin_only(save, request.get_json(silent=True) or {})

    @app.delete("/api/mail/own")
    def own_mail_disconnect():
        return admin_only(disconnect)

    @app.post("/api/mail/own/test")
    def own_mail_test():
        return admin_only(test, bool((request.get_json(silent=True) or {}).get("send", True)))
