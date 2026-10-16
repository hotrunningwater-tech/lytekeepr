"""The owner console (for the LyteKeepr owner; "super admin" in the code). For now: every account, and approving or suspending it. Grows into the full dashboard later.

Only a super admin (see directory.super_admins) who is signed in as an admin gets in; everyone else gets a plain refusal.
"""

from flask import g, jsonify, request, send_from_directory

from . import directory


def _allowed() -> bool:
    user = getattr(g, "user", None)
    return bool(user) and user.get("role") == "admin" and directory.is_super_admin(user.get("email") or "")


def register(app, app_dir):
    @app.get("/owner")
    @app.get("/superadmin")
    def superadmin_page():
        if not _allowed():
            return "This page is only for the LyteKeepr owner.", 403
        response = send_from_directory(app_dir, "superadmin.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/superadmin/accounts")
    def superadmin_accounts():
        if not _allowed():
            return jsonify({"error": "only the LyteKeepr owner can see this"}), 403
        return jsonify({"accounts": directory.list_accounts()})

    @app.post("/api/superadmin/accounts/<int:account_id>/status")
    def superadmin_status(account_id):
        if not _allowed():
            return jsonify({"error": "only the LyteKeepr owner can do this"}), 403
        try:
            account = directory.set_status(account_id, str((request.get_json(silent=True) or {}).get("status", "")))
        except directory.DirectoryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"account": account})
