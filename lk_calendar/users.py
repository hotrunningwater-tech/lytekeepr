"""Accounts. The admin adds people by their Google email; they sign in with Google and are let in only if their email is on this list.

There are no passwords for people here. The admin can also sign in with the app's own password (LYTEKEEPR_PASSWORD), so there is
always a way in. Turning a person off takes effect on their very next click.
"""

import functools
import re

from flask import g, jsonify, request

from . import store

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ADMIN_PSEUDO = {"id": 0, "email": "", "name": "", "role": "admin", "active": True, "access": "all", "share_emails": True}


def _clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise store.ValidationError("enter a valid email address")
    return email


def _public(row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"], "active": bool(row["active"]),
            "access": row["access"], "share_emails": bool(row["share_emails"]), "last_login_at": row["last_login_at"], "created_at": row["created_at"]}


def admin_email() -> str:
    """This workspace's admin (owner): the owner recorded in the directory; for the first workspace, before that is known,
    LYTEKEEPR_ADMIN_EMAIL or the connected Gmail address."""
    import os
    try:
        from profiles.store import get_active_profile_id
        from . import directory
        acct = directory.account_for_profile(get_active_profile_id())
        if acct and acct["owner_email"]:
            return acct["owner_email"].lower()
    except Exception:
        pass
    mail = (os.getenv("LYTEKEEPR_ADMIN_EMAIL") or "").strip().lower()
    if mail:
        return mail
    try:
        from gmail.sync import get_active_account_id
        return (get_active_account_id() or "").strip().lower()
    except Exception:
        return ""


def ensure_admin() -> None:
    """The admin has a row so their email is on the list and can own things. Does nothing until the admin's email is known."""
    mail = admin_email()
    if not mail or not EMAIL.match(mail):
        return
    from profiles.store import get_active_profile_id, home_profile_id
    if get_active_profile_id() == home_profile_id():          # the first workspace is account 1 in the directory, owned by the admin
        from . import directory
        directory.ensure_home_account(mail)
        with store._conn() as conn:                           # the team added before the directory existed belongs to this workspace
            team = [r["email"] for r in conn.execute("SELECT email FROM users WHERE role != 'admin'")]
        for email in team:
            try:
                directory.add_member(email, home_profile_id(), "internal")
            except directory.DirectoryError:
                pass
    with store._conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
            return
        conn.execute("INSERT OR IGNORE INTO users (email, role, active, share_emails, created_at) VALUES (?, 'admin', 1, 1, ?)", (mail, store.now_local().isoformat()))
        conn.execute("UPDATE users SET role = 'admin' WHERE email = ?", (mail,))


def get(user_id: int) -> dict | None:
    with store._conn() as conn:
        if user_id == 0:                      # the admin signed in with the app's own password: the same person as the admin's row
            row = conn.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
            return _public(row) if row else {**ADMIN_PSEUDO, "email": admin_email()}
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row else None


def for_session(user_id: int) -> dict | None:
    """The person a login cookie belongs to, if they may still come in."""
    user = get(user_id)
    return user if user and user["active"] else None


def sign_in_with_google(email: str, verified: bool, name: str = "") -> dict | None:
    """The person this Google account is, if it is on the list and switched on; their sign-in time is recorded. None = not allowed."""
    email = (email or "").strip().lower()
    if not email or not verified:
        return None
    ensure_admin()
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not row["active"]:
            return None
        conn.execute("UPDATE users SET last_login_at = ?, name = CASE WHEN name = '' THEN ? ELSE name END WHERE id = ?",
                     (store.now_local().isoformat(), (name or "")[:100], row["id"]))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    return _public(row)


# What a team member (not the admin) may use. Everything else under /api is refused, so a new feature is private until it is opened on purpose.
# Left out for now: Gmail, LinkedIn, documents, the AI chats (they read the admin's mailbox and messages), the Google Calendar sync and the
# app-wide profile name. Each comes back when it works per person.
USER_API_ALLOWED = ("/api/me", "/api/crm/", "/api/calendar/", "/api/calls/")       # calls: their own Fathom (lk_calendar/calls.py checks who sees what)
USER_API_BLOCKED = ("/api/calendar/chat", "/api/calendar/contact-ai", "/api/calendar/profile", "/api/crm/recent-activity")
# Google sync and meetings are open to team members: they always work with their own Google Calendar (member_google), never the admin's.


def api_allowed_for_user(path: str) -> bool:
    return path.startswith(USER_API_ALLOWED) and not path.startswith(USER_API_BLOCKED)


def is_admin() -> bool:
    """True for the admin, and when nobody is signed in (the login gate is off on this machine)."""
    try:
        user = getattr(g, "user", None)
    except RuntimeError:
        return True
    return user is None or user.get("role") == "admin"


ACCESS_LEVELS = ("funnels", "all")        # "funnels" = only the funnels shared with them (plus contacts they added); "all" = the whole database


def current_id() -> int | None:
    """Who is making this request (their row id), or None outside a request (a sync, a script)."""
    try:
        user = getattr(g, "user", None)
    except RuntimeError:
        return None
    return user.get("id") if user else None


def list_users() -> list[dict]:
    ensure_admin()
    with store._conn() as conn:
        shares: dict = {}
        for r in conn.execute("SELECT user_id, funnel_id FROM funnel_shares"):
            shares.setdefault(r["user_id"], []).append(r["funnel_id"])
        scopes: dict = {}
        for r in conn.execute("SELECT user_id, brand, scope FROM brand_access"):
            scopes.setdefault(r["user_id"], {})[r["brand"]] = r["scope"]
        return [{**_public(r), "funnel_ids": sorted(shares.get(r["id"], [])), "brands": scopes.get(r["id"], {})}
                for r in conn.execute("SELECT * FROM users ORDER BY role = 'admin' DESC, email")]


BRAND_SCOPES = ("all", "funnels")


def brand_access(user_id: int) -> dict:
    """{brand key: 'all' | 'funnels'} for a team member. Empty = brands not chosen for them yet: their shared funnels decide, as before."""
    with store._conn() as conn:
        return {r["brand"]: r["scope"] for r in conn.execute("SELECT brand, scope FROM brand_access WHERE user_id = ?", (user_id,))}


def set_brand_access(user_id: int, scopes: dict) -> dict:
    """Which brands a member may see: 'all' = the whole brand, 'funnels' = only that brand's funnels shared with them. A brand left out is blocked."""
    _must_be_user(user_id)
    keys = set(store.brand_keys())
    clean = {}
    for k, v in (scopes or {}).items():
        if k not in keys:
            raise store.ValidationError(f"no such brand: {k}")
        if v not in BRAND_SCOPES:
            raise store.ValidationError("each brand is 'all' or 'funnels'")
        clean[k] = v
    with store._conn() as conn:
        conn.execute("DELETE FROM brand_access WHERE user_id = ?", (user_id,))
        conn.executemany("INSERT INTO brand_access (user_id, brand, scope) VALUES (?, ?, ?)", [(user_id, k, v) for k, v in clean.items()])
    return clean


def funnels_shared_with(user_id: int) -> set:
    with store._conn() as conn:
        return {r["funnel_id"] for r in conn.execute("SELECT funnel_id FROM funnel_shares WHERE user_id = ?", (user_id,))}


def set_funnels(user_id: int, funnel_ids: list) -> list:
    _must_be_user(user_id)
    ids = sorted({int(i) for i in funnel_ids})
    with store._conn() as conn:
        conn.execute("DELETE FROM funnel_shares WHERE user_id = ?", (user_id,))
        conn.executemany("INSERT INTO funnel_shares (user_id, funnel_id) VALUES (?, ?)", [(user_id, i) for i in ids])
    return ids


def share_funnel(user_id: int, funnel_id: int) -> None:
    with store._conn() as conn:
        conn.execute("INSERT OR IGNORE INTO funnel_shares (user_id, funnel_id) VALUES (?, ?)", (user_id, int(funnel_id)))


def unshare_funnel_everywhere(funnel_id: int) -> None:
    """A funnel was deleted: nobody has it shared any more."""
    with store._conn() as conn:
        conn.execute("DELETE FROM funnel_shares WHERE funnel_id = ?", (int(funnel_id),))


def create_user(email: str, name: str = "", share_emails: bool = False) -> dict:
    email = _clean_email(email)
    from profiles.store import get_active_profile_id
    from . import directory
    with store._conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise store.ValidationError("that email is already on the list")
    try:
        directory.add_member(email, get_active_profile_id(), "internal")       # one email, one account: refused if it belongs elsewhere
    except directory.DirectoryError as exc:
        raise store.ValidationError(str(exc))
    with store._conn() as conn:
        cur = conn.execute("INSERT INTO users (email, name, role, active, share_emails, created_at) VALUES (?, ?, 'user', 1, ?, ?)",
                           (email, (name or "").strip()[:100], 1 if share_emails else 0, store.now_local().isoformat()))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _public(row)


def _must_be_user(user_id: int) -> dict:
    row = get(user_id)
    if not row:
        raise store.ValidationError("no such person")
    if row["role"] == "admin":
        raise store.ValidationError("the admin cannot be changed here")
    return row


def set_active(user_id: int, active: bool) -> dict:
    row = _must_be_user(user_id)
    with store._conn() as conn:
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
    return {**row, "active": bool(active)}


def set_access(user_id: int, access: str) -> dict:
    if access not in ACCESS_LEVELS:
        raise store.ValidationError("access must be funnels or all")
    row = _must_be_user(user_id)
    with store._conn() as conn:
        conn.execute("UPDATE users SET access = ? WHERE id = ?", (access, user_id))
    return {**row, "access": access}


def set_share_emails(user_id: int, share: bool) -> dict:
    row = _must_be_user(user_id)
    with store._conn() as conn:
        conn.execute("UPDATE users SET share_emails = ? WHERE id = ?", (1 if share else 0, user_id))
    return {**row, "share_emails": bool(share)}


def remove_user(user_id: int) -> None:
    row = _must_be_user(user_id)
    with store._conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM brand_access WHERE user_id = ?", (user_id,))
    from profiles.store import get_active_profile_id
    from . import directory
    directory.remove_member(row["email"], get_active_profile_id())


# ------------------------------------------------------------------ routes
def _admin_only():
    user = getattr(g, "user", None)
    if user is not None and user.get("role") != "admin":
        return jsonify({"error": "only the admin can do this"}), 403
    return None


def _guarded(fn):
    """A rejected input (bad email, unknown person...) becomes a plain 400 with the reason, for these routes only."""
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        denied = _admin_only() if fn.__name__.startswith("team_") else None
        if denied:
            return denied
        try:
            return fn(*args, **kwargs)
        except store.ValidationError as err:
            return jsonify({"error": str(err)}), 400
    return inner


def register(app):
    @app.get("/api/me")
    @_guarded
    def me():
        user = getattr(g, "user", None) or {**ADMIN_PSEUDO, "email": admin_email()}
        out = {k: user.get(k) for k in ("id", "email", "name", "role")}
        if user.get("role") == "admin":              # the admin's name is the one saved in Settings > Profile
            out["name"] = store.get_setting("sender_name").strip() or out["name"]
        from profiles.store import get_active_profile_id, home_profile_id
        from . import directory
        acct = directory.account_for_profile(get_active_profile_id()) or {}
        out["account_status"] = acct.get("status", "active")
        out["workspace"] = acct.get("name", "")
        out["is_super_admin"] = directory.is_super_admin(out.get("email") or "") and user.get("role") == "admin"
        out["features"] = {"linkedin": get_active_profile_id() == home_profile_id()}       # LinkedIn is the first workspace's only
        # An approved admin with no mailbox yet is taken to Google once, with their own account chosen (see /auth/google/start).
        try:
            from google_oauth.store import has_refresh_token
            out["gmail_connected"] = has_refresh_token()
        except Exception:
            out["gmail_connected"] = False
        out["gmail_connect_now"] = (user.get("role") == "admin" and out["account_status"] == "active" and not out["gmail_connected"]
                                    and not store.get_setting("gmail_autoconnect_offered"))
        return jsonify(out)

    @app.get("/api/team")
    @_guarded
    def team_list():
        return jsonify({"users": list_users()})

    @app.post("/api/team")
    @_guarded
    def team_add():
        body = request.get_json(silent=True) or {}
        return jsonify({"user": create_user(body.get("email", ""), body.get("name", ""), bool(body.get("share_emails")))})

    @app.get("/api/team/funnels")
    @_guarded
    def team_funnels():
        from crm import db as crm_db
        crm_db.init_db()
        conn = crm_db.get_connection()
        try:
            return jsonify({"funnels": [{"funnel_id": f["funnel_id"], "name": f["name"], "people_count": f["people_count"], "brand": f["brand"] or ""}
                                        for f in crm_db.list_funnels(conn)]})
        finally:
            conn.close()

    @app.post("/api/team/<int:user_id>/access")
    @_guarded
    def team_access(user_id):
        body = request.get_json(silent=True) or {}
        return jsonify({"user": set_access(user_id, str(body.get("access", "")))})

    @app.post("/api/team/<int:user_id>/brands")
    @_guarded
    def team_set_brands(user_id):
        body = request.get_json(silent=True) or {}
        return jsonify({"brands": set_brand_access(user_id, body.get("brands") or {})})

    @app.post("/api/team/<int:user_id>/funnels")
    @_guarded
    def team_set_funnels(user_id):
        body = request.get_json(silent=True) or {}
        return jsonify({"funnel_ids": set_funnels(user_id, body.get("funnel_ids") or [])})

    @app.post("/api/team/<int:user_id>/active")
    @_guarded
    def team_active(user_id):
        body = request.get_json(silent=True) or {}
        return jsonify({"user": set_active(user_id, bool(body.get("active")))})

    @app.post("/api/team/<int:user_id>/share")
    @_guarded
    def team_share(user_id):
        body = request.get_json(silent=True) or {}
        return jsonify({"user": set_share_emails(user_id, bool(body.get("share")))})

    @app.delete("/api/team/<int:user_id>")
    @_guarded
    def team_remove(user_id):
        remove_user(user_id)
        return jsonify({"ok": True})
