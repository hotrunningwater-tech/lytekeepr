"""LinkedIn per workspace: the USER logs in on their own computer and keeps it on; a small helper there (linkedin_helper/agent.py)
reads LinkedIn with the MCP tool and hands the messages to this file's addresses. We never hold a LinkedIn login. Admin only, like
Gmail. One connected computer per workspace.

Connecting: the admin accepts the account risk and gets a one-time code inside a line to paste (30 minutes, one use). The helper swaps
it for its own key. Both are stored only as hashes, in the directory (a key says which workspace it belongs to). What a key can do:
ask what to read, add messages to that workspace. Disconnecting (or a new computer) ends the old key for good; the helper then
removes itself from the computer's schedule. Messages already saved stay."""

import hashlib
import json
import secrets
import time
from datetime import datetime
from pathlib import Path

from . import directory

FIRST_SYNC_DAYS = 92          # 3 months
RETURN_SYNC_DAYS = 7          # the most a returning read looks back
MARGIN_DAYS = 1               # LinkedIn shows "Monday", not a time, so a read also looks a day behind the last one
CODE_MINUTES = 30
CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_BAD_CODES = 10            # per address, per BAD_CODE_SECONDS
BAD_CODE_SECONDS = 15 * 60
MAX_BATCH = 50
MAX_MESSAGES = 200
MAX_TEXT = 20000
UV_VERSION = "0.12.19"
HELPER_DIR = Path(__file__).parent / "linkedin_helper"
ADDON_DIR = Path(__file__).parent / "linkedin_addon"
LINK_MINUTES = 15             # the Claude add-on's Approve page
MAX_LINK_STARTS = 20          # per address, per BAD_CODE_SECONDS
SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_pending (
    link_id TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    computer TEXT NOT NULL DEFAULT '',
    expires REAL NOT NULL,
    key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS linkedin_codes (code_hash TEXT PRIMARY KEY, profile_id TEXT NOT NULL, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS linkedin_links (
    key_hash TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    computer TEXT NOT NULL DEFAULT '',
    connected_at TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT '',
    last_state TEXT NOT NULL DEFAULT '',
    sync_requested INTEGER NOT NULL DEFAULT 0,
    ended_at TEXT NOT NULL DEFAULT ''
);
"""
_bad_codes: dict = {}


def _db():
    conn = directory._conn()
    conn.executescript(SCHEMA)
    return conn


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_code(profile_id: str, user_id: int) -> str:
    code = "".join(secrets.choice(CODE_LETTERS) for _ in range(10))
    with _db() as conn:
        conn.execute("DELETE FROM linkedin_codes WHERE profile_id = ? OR expires < ?", (profile_id, time.time()))
        conn.execute("INSERT INTO linkedin_codes VALUES (?, ?, ?, ?)", (_hash(code), profile_id, user_id, time.time() + CODE_MINUTES * 60))
    return code


def pair(code: str, computer: str) -> str | None:
    """The helper's key for a good code, else None. A code works once; a new computer ends the old one's key."""
    key = secrets.token_urlsafe(32)
    with _db() as conn:
        row = conn.execute("SELECT * FROM linkedin_codes WHERE code_hash = ? AND expires > ?", (_hash(code.strip().upper()), time.time())).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM linkedin_codes WHERE code_hash = ?", (row["code_hash"],))
        conn.execute("UPDATE linkedin_links SET ended_at = ? WHERE profile_id = ? AND ended_at = ''", (_now(), row["profile_id"]))
        conn.execute("INSERT INTO linkedin_links (key_hash, profile_id, user_id, computer, connected_at) VALUES (?, ?, ?, ?, ?)",
                     (_hash(key), row["profile_id"], row["user_id"], computer[:80], _now()))
    return key


# ---- the Claude add-on: it asks for a link, the admin clicks Approve on a LyteKeepr page, the add-on picks up its key
def start_link(computer: str) -> dict:
    link_id, secret = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
    with _db() as conn:
        conn.execute("DELETE FROM linkedin_pending WHERE expires < ?", (time.time(),))
        conn.execute("INSERT INTO linkedin_pending (link_id, secret_hash, computer, expires) VALUES (?, ?, ?, ?)",
                     (link_id, _hash(secret), computer[:80], time.time() + LINK_MINUTES * 60))
    return {"link_id": link_id, "secret": secret}


def pending_link(link_id: str):
    with _db() as conn:
        return conn.execute("SELECT * FROM linkedin_pending WHERE link_id = ? AND expires > ?", (link_id, time.time())).fetchone()


def approve_link(link_id: str, profile_id: str, user_id: int) -> bool:
    """The admin said yes: the key waits in the pending row until the add-on picks it up (once, within LINK_MINUTES)."""
    row = pending_link(link_id)
    if not row or row["key"]:
        return False
    key = secrets.token_urlsafe(32)
    with _db() as conn:
        conn.execute("UPDATE linkedin_links SET ended_at = ? WHERE profile_id = ? AND ended_at = ''", (_now(), profile_id))
        conn.execute("INSERT INTO linkedin_links (key_hash, profile_id, user_id, computer, connected_at) VALUES (?, ?, ?, ?, ?)",
                     (_hash(key), profile_id, user_id, row["computer"], _now()))
        conn.execute("UPDATE linkedin_pending SET key = ? WHERE link_id = ?", (key, link_id))
    return True


def poll_link(link_id: str, secret: str):
    """(status, key): 200 with the key once approved (then the row is gone), 202 while waiting, 410 when unknown or expired."""
    row = pending_link(link_id)
    if not row or not secrets.compare_digest(row["secret_hash"], _hash(secret)):
        return 410, None
    if not row["key"]:
        return 202, None
    with _db() as conn:
        conn.execute("DELETE FROM linkedin_pending WHERE link_id = ?", (link_id,))
    return 200, row["key"]


def addon_file(address: str) -> bytes:
    """The .mcpb file the USER downloads: the add-on, the current helper and this workspace's address."""
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("manifest.json", "pyproject.toml", "server.py"):
            z.write(ADDON_DIR / name, name)
        z.write(HELPER_DIR / "agent.py", "agent.py")
        z.writestr("address.txt", address + "\n")
    return out.getvalue()


def current_link(profile_id: str):
    with _db() as conn:
        return conn.execute("SELECT * FROM linkedin_links WHERE profile_id = ? AND ended_at = ''", (profile_id,)).fetchone()


def link_for_key(key: str):
    if not key:
        return None
    with _db() as conn:
        return conn.execute("SELECT * FROM linkedin_links WHERE key_hash = ?", (_hash(key),)).fetchone()


def disconnect(profile_id: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE linkedin_links SET ended_at = ? WHERE profile_id = ? AND ended_at = ''", (_now(), profile_id))
        conn.execute("DELETE FROM linkedin_codes WHERE profile_id = ?", (profile_id,))


def request_sync(profile_id: str) -> bool:
    with _db() as conn:
        return conn.execute("UPDATE linkedin_links SET sync_requested = 1 WHERE profile_id = ? AND ended_at = ''", (profile_id,)).rowcount > 0


def plan() -> dict:
    """What the helper should read next, for the workspace it belongs to."""
    from integrations.linkedin import storage
    state = storage.load()
    if not state.get("backfilled"):
        return {"first": True, "window_days": FIRST_SYNC_DAYS}
    last = state.get("last_synced_at")
    days = RETURN_SYNC_DAYS
    if last:
        days = min((datetime.now() - datetime.fromisoformat(last)).total_seconds() / 86400 + MARGIN_DAYS, RETURN_SYNC_DAYS)
    return {"first": False, "window_days": round(days, 3)}


def clean_conversations(items) -> list:
    """Only the expected shape and sizes get near the database."""
    out = []
    if not isinstance(items, list):
        return out
    for item in items[:MAX_BATCH]:
        summary = item.get("summary") if isinstance(item, dict) else None
        messages = item.get("messages") if isinstance(item, dict) else None
        if not isinstance(summary, dict) or not isinstance(messages, list) or not isinstance(summary.get("conversation_ref"), str):
            continue
        slug = summary.get("participant_profile_slug")
        clean_summary = {"conversation_ref": summary["conversation_ref"][:500], "participant_name": str(summary.get("participant_name") or "Unknown")[:200],
                         "participant_profile_slug": slug[:200] if isinstance(slug, str) and slug else None, "unread": bool(summary.get("unread"))}
        clean_messages = []
        for m in messages[:MAX_MESSAGES]:
            if not isinstance(m, dict) or not isinstance(m.get("message_ref"), str) or not m["message_ref"]:
                continue
            clean_messages.append({"message_ref": m["message_ref"][:500], "direction": "outgoing" if m.get("direction") == "outgoing" else "incoming",
                                   "sender_name": str(m.get("sender_name") or "")[:200] or None, "text": str(m.get("text") or "")[:MAX_TEXT],
                                   "sent_at": str(m.get("sent_at") or "")[:40]})
        if clean_messages:
            out.append((clean_summary, clean_messages))
    return out


def save_conversations(items) -> int:
    from crm import db as crm_db
    from integrations.linkedin import sync
    crm_db.init_db()
    run_id = crm_db.new_run_id()
    return sum(sync.save_conversation(summary, messages, run_id) for summary, messages in clean_conversations(items))


def finish(key_hash: str, report: dict) -> None:
    """The helper finished a read. Only a good one moves "last synced", so a failed read is covered by the next."""
    from crm import db as crm_db
    from integrations.linkedin import storage
    report = report if isinstance(report, dict) else {}
    ok = report.get("ok") is True
    state_name = report.get("state") if report.get("state") in ("ok", "needs_login", "error") else "error"
    with _db() as conn:
        conn.execute("UPDATE linkedin_links SET last_seen = ?, last_state = ?, sync_requested = 0 WHERE key_hash = ?", (_now(), state_name, key_hash))
    if ok:
        state = storage.load()
        now = datetime.now().isoformat()
        if not state.get("backfilled"):
            state["backfilled"], state["backfilled_at"] = True, now
        state["last_synced_at"] = now
        state["capped"] = bool(report.get("capped"))
        storage.save(state)
        crm_db.init_db()
        crm_db.log_activity(crm_db.new_run_id(), 7, "linkedin_sync", "save_sync_state", detail=f"helper: {int(report.get('conversations') or 0)} conversation(s) read")


def status(profile_id: str) -> dict:
    from crm import db as crm_db
    from integrations.linkedin import storage
    link = current_link(profile_id)
    state = storage.load()
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        conversations, messages = crm_db.count_people(conn, channel="linkedin"), crm_db.count_messages(conn, channel="linkedin")
    finally:
        conn.close()
    from . import linkedin_outbox
    outbox = linkedin_outbox.counts()
    return {"outbox_waiting": outbox["waiting"], "outbox_problems": outbox["problems"], "connected": bool(link), "computer": link["computer"] if link else "", "connected_at": link["connected_at"] if link else "",
            "last_seen": link["last_seen"] if link else "", "state": link["last_state"] if link else "",
            "sync_requested": bool(link and link["sync_requested"]), "first_done": bool(state.get("backfilled")), "last_synced_at": state.get("last_synced_at"),
            "capped": bool(state.get("capped")), "conversation_count": conversations, "message_count": messages}


def helper_fingerprint() -> str:
    """Which helper version is current: the helpers on USERs' computers update themselves to it (agent.update_self)."""
    import hashlib
    return hashlib.sha256((HELPER_DIR / "agent.py").read_bytes()).hexdigest()


def _installer(name: str, address: str) -> str:
    text = (HELPER_DIR / name).read_text(encoding="utf-8")
    return text.replace("__ADDRESS__", address).replace("__UV_VERSION__", UV_VERSION).replace("__PACKAGE__", _package())


def _package() -> str:
    for line in (HELPER_DIR / "agent.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("MCP_PACKAGE"):
            return line.split('"')[1]
    raise RuntimeError("helper package not found")


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · LyteKeepr</title><style>
body{{margin:0;font-family:Inter,-apple-system,Segoe UI,sans-serif;background:#b3c7cd;color:#29434e;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}}
main{{background:#f3f7f9;border-radius:22px;padding:32px;max-width:520px;width:100%;box-sizing:border-box}}
h1{{font-size:22px;margin:0 0 12px}} p{{line-height:1.5}} .risk{{color:#657d86;font-size:14px}}
label{{display:block;margin:16px 0}} a.button{{display:inline-block;text-decoration:none}} button,a.button{{background:#29434e;color:#fff;border:0;border-radius:999px;padding:12px 28px;font-size:15px;font-weight:600;cursor:pointer}}
</style></head><body><main><div style="color:#e98b74;font-weight:700;margin-bottom:8px">LyteKeepr</div><h1>{title}</h1>{body}</main></body></html>"""


def _page(title: str, body: str):
    from flask import Response
    return Response(_PAGE.format(title=title, body=body), mimetype="text/html", headers={"Cache-Control": "no-store"})


def register(app):
    import html
    import os
    from flask import Response, g, jsonify, redirect, request
    import auth_gate
    from profiles.store import get_active_profile_id, use_profile
    from .claude_connector import _may_sign_in

    prefix = os.getenv("LK_URL_PREFIX", "").rstrip("/")
    auth_gate.PUBLIC_PATHS.add("/linkedin/approve")      # checks the sign-in itself, so it can come back here after it; /linkedin-helper/... is public by prefix

    def address() -> str:
        scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
        return f"{scheme}://{request.host}{prefix}"

    def who() -> str:
        if request.remote_addr in ("127.0.0.1", "::1") and request.headers.get("X-Real-IP"):
            return request.headers["X-Real-IP"]
        return request.remote_addr or "?"

    def with_key(job):
        """Run job(link) inside the key's own workspace. 410 tells the helper to stop for good; 403 only for now."""
        auth = request.headers.get("Authorization", "")
        link = link_for_key(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if not link:
            return jsonify({"error": "not connected"}), 401
        if link["ended_at"]:
            return jsonify({"error": "disconnected"}), 410
        if not _may_sign_in(link["user_id"], link["profile_id"]):
            return jsonify({"error": "not available"}), 403
        with _db() as conn:
            conn.execute("UPDATE linkedin_links SET last_seen = ? WHERE key_hash = ?", (_now(), link["key_hash"]))
        with use_profile(link["profile_id"]):
            return job(link)

    @app.get("/linkedin-helper/agent.py")
    def linkedin_helper_file():
        return Response((HELPER_DIR / "agent.py").read_bytes(), mimetype="text/plain")

    @app.get("/linkedin-helper/install.sh")
    def linkedin_helper_install_mac():
        return Response(_installer("install.sh", address()), mimetype="text/plain")

    @app.get("/linkedin-helper/install.ps1")
    def linkedin_helper_install_windows():
        return Response(_installer("install.ps1", address()), mimetype="text/plain")

    @app.post("/linkedin-helper/pair")
    def linkedin_helper_pair():
        ip, now = who(), time.time()
        recent = [t for t in _bad_codes.get(ip, []) if now - t < BAD_CODE_SECONDS]
        if len(recent) >= MAX_BAD_CODES:
            return jsonify({"error": "too many tries"}), 403
        body = request.get_json(silent=True) or {}
        key = pair(str(body.get("code") or ""), str(body.get("computer") or ""))
        if not key:
            _bad_codes[ip] = recent + [now]
            return jsonify({"error": "bad code"}), 403
        return jsonify({"key": key})

    @app.post("/linkedin-helper/link/start")
    def linkedin_helper_link_start():
        ip, now = who(), time.time()
        recent = [t for t in _bad_codes.get("start:" + ip, []) if now - t < BAD_CODE_SECONDS]
        if len(recent) >= MAX_LINK_STARTS:
            return jsonify({"error": "too many tries"}), 403
        _bad_codes["start:" + ip] = recent + [now]
        out = start_link(str((request.get_json(silent=True) or {}).get("computer") or ""))
        return jsonify(dict(out, url=f"{address()}/linkedin/approve?l={out['link_id']}"))

    @app.post("/linkedin-helper/link/poll")
    def linkedin_helper_link_poll():
        body = request.get_json(silent=True) or {}
        status, key = poll_link(str(body.get("link_id") or ""), str(body.get("secret") or ""))
        return jsonify({"key": key} if key else {"pending": status == 202}), status

    @app.get("/linkedin-helper/status")
    def linkedin_helper_status():
        return with_key(lambda link: jsonify(status(link["profile_id"])))

    @app.get("/linkedin/approve")
    def linkedin_approve_page():
        user = auth_gate._session["current_user"]() if "current_user" in auth_gate._session else {"id": 0, "role": "admin"}
        if not user:                              # sign in first, then come straight back here
            response = redirect(prefix + "/login")
            response.set_cookie("lk_next", prefix + request.full_path, max_age=900, httponly=True, samesite="Lax", path="/",
                                secure=request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure)
            return response
        row = pending_link(request.args.get("l", ""))
        if not row:
            return _page("This link has expired", "<p>In Claude, say <b>sync my LinkedIn</b> to get a new one.</p>")
        if row["key"]:
            return _page("LinkedIn is connected", "<p>You can close this page and go back to Claude.</p>")
        if user.get("role") != "admin":
            return _page("Ask your admin", "<p>Only your workspace's admin can connect LinkedIn.</p>")
        computer = html.escape(row["computer"] or "your computer")
        return _page("Connect LinkedIn to LyteKeepr", f"""
<p>Claude on <b>{computer}</b> wants to bring your LinkedIn messages into LyteKeepr. Your LinkedIn login stays on that computer.</p>
<p class="risk">This uses a tool that is not an official LinkedIn product. LinkedIn does not allow automated tools, so it could restrict your account.</p>
<form method="post" action="{prefix}/linkedin/approve">
  <input type="hidden" name="l" value="{html.escape(row['link_id'])}">
  <label><input type="checkbox" name="risk" value="yes" required> I understand the risk</label>
  <button type="submit">Approve</button>
</form>""")

    @app.post("/linkedin/approve")
    def linkedin_approve_submit():
        user = auth_gate._session["current_user"]() if "current_user" in auth_gate._session else {"id": 0, "role": "admin"}
        if not user or user.get("role") != "admin":
            return _page("Ask your admin", "<p>Only your workspace's admin can connect LinkedIn.</p>"), 403
        if request.form.get("risk") != "yes":
            return _page("Please tick the box", "<p>Go back and tick that you understand the risk.</p>"), 400
        if not approve_link(request.form.get("l", ""), get_active_profile_id(), int(user.get("id") or 0)):
            return _page("This link has expired", "<p>In Claude, say <b>sync my LinkedIn</b> to get a new one.</p>"), 410
        home = prefix + "/"
        return _page("LinkedIn is connected", "<p>A LinkedIn window may open on your computer: log in there if it asks. "
                     "Your LinkedIn messages start coming into LyteKeepr within a few minutes.</p>"
                     f'<p><a class="button" href="{home}">Back to LyteKeepr</a></p>'
                     f'<script>setTimeout(function () {{ location.href = {json.dumps(home)}; }}, 4000);</script>')

    @app.get("/api/linkedin/addon")
    def linkedin_addon_download():
        return Response(addon_file(address()), mimetype="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="LyteKeepr LinkedIn.mcpb"'})

    @app.get("/linkedin-helper/plan")
    def linkedin_helper_plan():
        def job(link):
            from . import linkedin_outbox
            return jsonify(dict(plan(), sync_now=bool(link["sync_requested"]), helper=helper_fingerprint(), outbox=linkedin_outbox.counts()["waiting"]))
        return with_key(job)

    @app.get("/linkedin-helper/outbox")
    def linkedin_helper_outbox():
        from . import linkedin_outbox
        return with_key(lambda link: jsonify({"messages": linkedin_outbox.claim()}))

    @app.post("/linkedin-helper/outbox/<int:outbox_id>")
    def linkedin_helper_outbox_done(outbox_id):
        from . import linkedin_outbox
        body = request.get_json(silent=True) or {}
        return with_key(lambda link: jsonify({"ok": linkedin_outbox.finish(outbox_id, str(body.get("outcome") or ""), str(body.get("detail") or ""))}))

    @app.post("/linkedin-helper/conversations")
    def linkedin_helper_conversations():
        body = request.get_json(silent=True) or {}
        return with_key(lambda link: jsonify({"new_messages": save_conversations(body.get("conversations"))}))

    @app.post("/linkedin-helper/done")
    def linkedin_helper_done():
        body = request.get_json(silent=True) or {}
        def job(link):
            finish(link["key_hash"], body)
            return jsonify({"ok": True})
        return with_key(job)

    # ---- what the admin uses, on the LinkedIn tab
    @app.get("/api/linkedin/status")
    def linkedin_status():
        return jsonify(status(get_active_profile_id()))

    @app.post("/api/linkedin/link/code")
    def linkedin_link_code():
        if (request.get_json(silent=True) or {}).get("accepted_risk") is not True:
            return jsonify({"error": "Please accept the account risk first."}), 400
        code = new_code(get_active_profile_id(), int((g.get("user") or {}).get("id") or 0))
        base = address()
        return jsonify({"code": code, "minutes": CODE_MINUTES, "mac": f"curl -fsSL {base}/linkedin-helper/install.sh | sh -s -- {code}",
                        "windows": f'& ([scriptblock]::Create((irm {base}/linkedin-helper/install.ps1))) "{code}"'})

    @app.post("/api/linkedin/link/disconnect")
    def linkedin_link_disconnect():
        disconnect(get_active_profile_id())
        return jsonify({"ok": True})

    @app.post("/api/linkedin/sync/run")
    def linkedin_sync_run():
        if not request_sync(get_active_profile_id()):
            return jsonify({"error": "LinkedIn is not connected."}), 400
        return jsonify({"ok": True})
