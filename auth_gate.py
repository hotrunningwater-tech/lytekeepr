"""Login gate for the whole app.

Two ways in: "Sign in with Google" (only emails the admin added on the Team page are let in) and the admin's own password.
Set LYTEKEEPR_PASSWORD (and LYTEKEEPR_SECRET_KEY) in .env. If no password is set the gate is off, so local dev and existing
deploys keep working until the variable is added. The login cookie says who the person is; turning a person off on the Team
page locks them out on their next click.
"""

import hmac
import os
import time
from collections import defaultdict

from flask import g, jsonify, make_response, redirect, request, send_from_directory
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE = "lk_auth"
MAX_AGE = 30 * 24 * 3600
PUBLIC_PATHS = {"/login", "/logout", "/styles.css", "/auth/google/callback", "/auth/google/login"}   # the callback carries a signed state instead of the login cookie
MAX_FAILURES = 8
LOCKOUT_SECONDS = 15 * 60


# Things that still exist once for the whole app (the LinkedIn session, the LinkedIn lead files, the OCR document service, demo
# and developer tools) belong to the first workspace only; other workspaces are refused until they work per workspace.
HOME_ONLY_API = ("/api/linkedin", "/api/linkedin-leads", "/api/documents", "/api/files", "/api/ocr", "/api/photo-review",
                 "/api/demo", "/api/dev", "/api/diagnostics")
WAITING_ALLOWED_API = ("/api/me",)


def finish_google_login(email: str, verified: bool, name: str = ""):
    """Called by the Google callback. A known email goes into its own workspace; an unknown one gets a new workspace that waits
    for approval; a suspended workspace, a turned-off person or an unverified Google email is sent back to the login page."""
    from profiles.store import use_profile
    from lk_calendar import directory, users
    prefix = _session.get("prefix", "")
    email = (email or "").strip().lower()
    if not email or not verified:
        return redirect(prefix + "/login?e=3")
    users.ensure_admin()                                   # the first workspace is registered before anyone is looked up
    account, _role = directory.lookup(email)
    if account is None:
        try:
            account = directory.sign_up(email, name)
        except directory.DirectoryError:
            return redirect(prefix + "/login?e=6")
        _notify_signup(email, request.host_url.rstrip("/") + prefix)
    if account["status"] == "suspended":
        return redirect(prefix + "/login?e=5")
    with use_profile(account["profile_id"]):
        person = users.sign_in_with_google(email, verified, name)
    if not person:
        return redirect(prefix + "/login?e=3")
    response = make_response(redirect(prefix + "/"))
    _session["set_cookie"](response, person["id"], account["profile_id"])
    return response


def _notify_signup(email: str, app_url: str) -> None:
    """Tell the super admin about a new sign-up, by email from the first workspace's Gmail. Best effort: a failure never blocks the sign-up."""
    import threading

    def send():
        try:
            from email.mime.text import MIMEText
            from gmail import client as gmail_client
            from profiles.store import home_profile_id, use_profile
            from lk_calendar import directory
            with use_profile(home_profile_id()):
                for admin in sorted(directory.super_admins()):
                    mime = MIMEText(f"{email} signed up for LyteKeepr and is waiting for your approval.\n\nApprove them here: {app_url}/superadmin\n")
                    mime["To"] = admin
                    mime["Subject"] = f"New LyteKeepr sign-up: {email}"
                    gmail_client.send_message(mime.as_bytes())
        except Exception:
            import logging
            logging.getLogger(__name__).exception("could not email the super admin about a new sign-up")
    threading.Thread(target=send, name="signup-notice", daemon=True).start()


_session = {}        # filled by install(): the signer, the URL prefix and the cookie writer


def install(app, app_dir):
    from google_oauth.config import _ensure_env_loaded
    _ensure_env_loaded()

    prefix = os.getenv("LK_URL_PREFIX", "").rstrip("/")   # e.g. "/calendar" when nginx serves the app at that path
    password = os.getenv("LYTEKEEPR_PASSWORD", "")
    if not password:
        app.logger.warning("LYTEKEEPR_PASSWORD not set — login gate is OFF")
        return

    secret = os.getenv("LYTEKEEPR_SECRET_KEY") or password
    signer = URLSafeTimedSerializer(secret, salt="lytekeepr-login")
    failures = defaultdict(list)   # client address -> timestamps of recent wrong passwords

    def client_id():
        # Behind nginx the real address arrives in X-Real-IP; only trust it from the local proxy.
        if request.remote_addr in ("127.0.0.1", "::1") and request.headers.get("X-Real-IP"):
            return request.headers["X-Real-IP"]
        return request.remote_addr or "?"

    def locked_out(who):
        now = time.time()
        failures[who] = [t for t in failures[who] if now - t < LOCKOUT_SECONDS]
        return len(failures[who]) >= MAX_FAILURES

    def set_cookie(response, user_id, profile_id=None):
        from profiles.store import home_profile_id
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        payload = {"u": user_id, "w": profile_id or home_profile_id()}          # who, and which workspace they work in
        response.set_cookie(COOKIE, signer.dumps(payload), max_age=MAX_AGE, httponly=True, samesite="Lax", secure=secure, path=prefix or "/")

    _session.update(prefix=prefix, set_cookie=set_cookie)

    def current_user():
        """The signed-in person, or None. The cookie names the person and their workspace; this request is switched into that
        workspace. An old cookie is the admin of the first workspace. A person turned off or removed, or a suspended workspace,
        is no longer let in."""
        from profiles.store import home_profile_id, list_profiles, set_request_profile
        from lk_calendar import directory, users
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        try:
            data = signer.loads(token, max_age=MAX_AGE)
        except BadSignature:
            return None
        user_id = data.get("u") if isinstance(data, dict) else 0
        workspace = (data.get("w") if isinstance(data, dict) else None) or home_profile_id()
        if workspace != home_profile_id() and workspace not in {p["profile_id"] for p in list_profiles()}:
            return None
        set_request_profile(workspace)
        account = directory.account_for_profile(workspace)
        status = account["status"] if account else "active"
        if status == "suspended":
            return None
        g.account_status = status
        try:
            return users.for_session(int(user_id or 0))
        except Exception:
            return None

    @app.before_request
    def require_login():
        from profiles.store import get_active_profile_id, home_profile_id, set_request_profile
        g.profile_token = set_request_profile(home_profile_id())     # until the cookie says otherwise (current_user switches it)
        if request.path in PUBLIC_PATHS:
            return None
        user = current_user()
        if user:
            g.user = user
            path = request.path
            if path.startswith("/api"):
                if g.get("account_status") == "waiting" and not path.startswith(WAITING_ALLOWED_API):
                    return jsonify({"error": "Your account is waiting for approval.", "waiting": True}), 403
                if path.startswith(HOME_ONLY_API) and get_active_profile_id() != home_profile_id():
                    return jsonify({"error": "This is not available in your workspace yet."}), 403
                if user.get("role") != "admin":
                    from lk_calendar import users
                    if not users.api_allowed_for_user(path):
                        return jsonify({"error": "This is not available to team members yet."}), 403
            return None
        if request.method == "GET" and not request.path.startswith("/api"):
            return redirect(prefix + "/login")
        return jsonify({"error": "login required"}), 401

    @app.teardown_request
    def leave_workspace(_exc=None):
        from profiles.store import reset_request_profile
        token = g.pop("profile_token", None)
        if token is not None:
            reset_request_profile(token)

    @app.get("/login")
    def login_page():
        response = send_from_directory(app_dir, "login.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/login")
    def login_submit():
        who = client_id()
        if locked_out(who):
            return redirect(prefix + "/login?e=2")
        supplied = request.form.get("password", "")
        if not hmac.compare_digest(supplied.encode(), password.encode()):
            failures[who].append(time.time())
            time.sleep(1)  # slows down guessing
            return redirect(prefix + "/login?e=1")
        failures.pop(who, None)
        response = make_response(redirect(prefix + "/"))
        set_cookie(response, 0)                      # the admin
        return response

    @app.get("/auth/google/login")
    def google_login_start():
        from google_oauth.errors import GoogleOAuthError
        from google_oauth.flow import build_login_url
        from itsdangerous import URLSafeTimedSerializer
        try:
            state = URLSafeTimedSerializer(os.getenv("LYTEKEEPR_SECRET_KEY") or password or "local-dev-only", salt="google-oauth-state").dumps("google-login")
            return redirect(build_login_url(state))
        except GoogleOAuthError:
            return redirect(prefix + "/login?e=4")

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        response = make_response(redirect(prefix + "/login"))
        response.delete_cookie(COOKIE, path=prefix or "/")
        return response
