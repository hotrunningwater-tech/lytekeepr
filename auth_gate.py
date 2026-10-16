"""Single-password login gate for the whole app.

Set LYTEKEEPR_PASSWORD (and LYTEKEEPR_SECRET_KEY) in .env. If no password
is set the gate is off, so local dev and existing deploys keep working
until the variable is added.
"""

import hmac
import os
import time
from collections import defaultdict

from flask import jsonify, make_response, redirect, request, send_from_directory
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE = "lk_auth"
MAX_AGE = 30 * 24 * 3600
PUBLIC_PATHS = {"/login", "/logout", "/styles.css", "/auth/google/callback"}   # the callback carries a signed state instead of the login cookie
MAX_FAILURES = 8
LOCKOUT_SECONDS = 15 * 60


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

    def is_logged_in():
        token = request.cookies.get(COOKIE)
        if not token:
            return False
        try:
            signer.loads(token, max_age=MAX_AGE)
            return True
        except BadSignature:
            return False

    @app.before_request
    def require_login():
        if request.path in PUBLIC_PATHS or is_logged_in():
            return None
        if request.method == "GET" and not request.path.startswith("/api"):
            return redirect(prefix + "/login")
        return jsonify({"error": "login required"}), 401

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
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        response.set_cookie(
            COOKIE, signer.dumps("ok"), max_age=MAX_AGE,
            httponly=True, samesite="Lax", secure=secure, path=prefix or "/",
        )
        return response

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        response = make_response(redirect(prefix + "/login"))
        response.delete_cookie(COOKIE, path=prefix or "/")
        return response
