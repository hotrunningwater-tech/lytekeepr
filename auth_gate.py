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


_session = {}        # filled by install(): the signer, the URL prefix and the cookie writer


def finish_google_login(email: str, verified: bool, name: str = ""):
    """Called by the Google callback: let the person in (cookie + redirect home) if their email is on the list, else back to the login page."""
    from lk_calendar import users
    prefix = _session.get("prefix", "")
    person = users.sign_in_with_google(email, verified, name)
    if not person:
        return redirect(prefix + "/login?e=3")
    response = make_response(redirect(prefix + "/"))
    _session["set_cookie"](response, person["id"])
    return response


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

    def set_cookie(response, user_id):
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        response.set_cookie(COOKIE, signer.dumps({"u": user_id}), max_age=MAX_AGE, httponly=True, samesite="Lax", secure=secure, path=prefix or "/")

    _session.update(prefix=prefix, set_cookie=set_cookie)

    def current_user():
        """The signed-in person, or None. An old cookie ("ok") is the admin; a person who was turned off or removed is no longer let in."""
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        try:
            data = signer.loads(token, max_age=MAX_AGE)
        except BadSignature:
            return None
        user_id = data.get("u") if isinstance(data, dict) else 0
        try:
            from lk_calendar import users
            return users.for_session(int(user_id or 0))
        except Exception:
            return {"id": 0, "email": "", "name": "", "role": "admin"} if user_id == 0 else None

    @app.before_request
    def require_login():
        from profiles.store import home_profile_id, set_request_profile
        g.profile_token = set_request_profile(home_profile_id())     # the workspace this request works in (per person in a later step)
        if request.path in PUBLIC_PATHS:
            return None
        user = current_user()
        if user:
            g.user = user
            if user.get("role") != "admin" and request.path.startswith("/api"):
                from lk_calendar import users
                if not users.api_allowed_for_user(request.path):
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
