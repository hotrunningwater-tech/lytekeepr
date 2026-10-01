"""
LyteKeepr — local dev server.

Serves the existing static UI (index.html, styles.css, chat.js) and the
assistant chat API. All OpenAI calls happen server-side, in
assistant.service.get_reply() — the browser only ever talks to
/api/chat on this server.

Run with:
    .venv/bin/python server.py

Then open http://127.0.0.1:5052 in a browser.
"""

import io
import logging
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_file, send_from_directory

from assistant.delete_flow import confirm_pending_delete, delete_scanned_document
from assistant.linkedin_send_flow import clear_pending_send, confirm_pending_send, load_pending_send
from assistant import email_send_flow
from assistant import followup_engine
from assistant.service import OpenAIServiceError, get_reply
from demo_state import SnapshotError, capture_snapshot, get_snapshot_manager_status, list_snapshots, reset_to_baseline, restore_snapshot
from demo_state.recordings import list_demo_recordings, save_demo_recording
from demo_state.redactions import render_redacted_recording
from services.ocr.photo_review import finalize_photo_review, get_asset_path, regenerate_preview, start_photo_review
from services.ocr.adapter import get_original_file_path, list_recent_scans, rename_saved_document, scan_upload
from google_oauth.errors import GoogleOAuthError
from google_oauth.flow import build_authorization_url, exchange_code_for_tokens
from google_oauth.store import (
    account_has_refresh_token,
    clear_legacy_tokens,
    has_refresh_token,
    list_token_account_ids,
    save_tokens,
)
from gmail import client as gmail_client
from gmail.config import GmailSyncConfig
from gmail.attachment_rules import is_photo_reviewable_attachment
from gmail.drawer_format import get_message_detail, list_drawer_records
from gmail.errors import GmailSyncError
from gmail.message_access import load_full_message
from gmail.storage import load_message as load_saved_message, load_sync_state
from gmail.sync import get_active_account_id, get_pending_candidates, save_candidates, scan_for_candidates, set_active_account_id
from integrations.linkedin import client as linkedin_client
from integrations.linkedin import sync as linkedin_sync
from integrations.linkedin import storage as linkedin_sync_storage
from crm import db as crm_db
from doc_registry.registry import compute_counts, dismiss_entries
from doc_registry.photo_review_state import set_pending_review
from doc_registry.scheduler import maybe_run_daily_check, run_check_now
from doc_registry import queue_storage
from doc_registry.scan_runner import run_queued_scans
from assistant.chronology_extractor import extract_events_from_document, extract_events_from_email_batch
from chronology import storage as chronology_storage
from chronology.pipeline import run_extraction
from gmail import storage as gmail_storage
from doc_registry.case_documents import read_case_document
from profiles import (
    create_profile,
    ensure_profile_bootstrap,
    get_active_profile,
    get_active_profile_id,
    get_attached_gmail_account_ids,
    list_profiles as list_local_profiles,
    profile_counts,
    rename_profile,
    reset_profile_local_state,
    seed_default_profiles,
    set_active_profile_id,
)

APP_DIR = Path(__file__).resolve().parent
ALLOWED_ROLES = {"user", "assistant"}
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024  # local demo uploads and recordings


@app.get("/")
def index():
    # send_from_directory adds ETag/Last-Modified by default, which lets
    # the browser skip re-fetching even on a manual reload — exactly the
    # kind of stale-page confusion that wasted real debugging time here.
    # This is the one page that must never be cached.
    response = send_from_directory(APP_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/styles.css")
def styles():
    return send_from_directory(APP_DIR, "styles.css")


@app.get("/chat.js")
def chat_js():
    return send_from_directory(APP_DIR, "chat.js")


@app.get("/crop-lock.js")
def crop_lock_js():
    return send_from_directory(APP_DIR, "crop-lock.js", mimetype="text/javascript")


@app.get("/email/<path:subpath>")
def email_assets(subpath):
    # Static ES modules for the email feature (see experiment DLYTE/email/).
    # Force the JS mimetype explicitly — browsers reject <script type="module">
    # if the server's guessed Content-Type isn't a JS type.
    mimetype = "text/javascript" if subpath.endswith(".js") else None
    return send_from_directory(APP_DIR / "email", subpath, mimetype=mimetype)


@app.get("/case-overview/<path:subpath>")
def case_overview_assets(subpath):
    mimetype = "text/javascript" if subpath.endswith(".js") else None
    return send_from_directory(APP_DIR / "case-overview", subpath, mimetype=mimetype)


@app.get("/demo/<path:subpath>")
def demo_assets(subpath):
    mimetype = "text/javascript" if subpath.endswith(".js") else None
    return send_from_directory(APP_DIR / "demo", subpath, mimetype=mimetype)


def _validate_messages(payload) -> list[dict] | None:
    if not isinstance(payload, list) or not payload:
        return None
    cleaned = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        role = item.get("role")
        content = item.get("content")
        if role not in ALLOWED_ROLES or not isinstance(content, str) or not content.strip():
            return None
        cleaned.append({"role": role, "content": content})
    return cleaned


def _is_local_demo_request() -> bool:
    host = request.host.split(":", 1)[0].lower()
    remote = (request.remote_addr or "").lower()
    if request.headers.get("X-DLYTE-Demo-Key") != "local-reset":
        return False
    return host in {"127.0.0.1", "localhost"} or remote in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _is_local_browser_request() -> bool:
    host = request.host.split(":", 1)[0].lower()
    remote = (request.remote_addr or "").lower()
    return host in {"127.0.0.1", "localhost"} or remote in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _build_photo_naming_response(result: dict) -> dict:
    return {
        "job_id": result["job_id"],
        "status": result["status"],
        "filename": result["original_filename"],
        "suggested_filename": result.get("suggested_filename", result["original_filename"]),
        "naming_source": result.get("naming_source", "original"),
        "naming_reason": result.get("naming_reason", ""),
        "naming_details": result.get("naming_details"),
        "page_count": result["page_count"],
        "full_text": result["full_text"],
        "pages": [{"page_number": p["page_number"], "text": p["text"]} for p in result["pages"]],
        "warnings": result.get("warnings", []),
        "source_images_preserved": result.get("source_images_preserved", False),
    }


def _normalize_saved_document_for_chat(document: dict | None) -> dict | None:
    if not isinstance(document, dict):
        return None
    return {
        "filename": document.get("filename") or "saved document",
        "page_count": int(document.get("pageCount") or 0),
        "full_text": document.get("fullText") or "",
        "pages": [
            {
                "page_number": page.get("pageNumber"),
                "text": page.get("text", "") or "",
            }
            for page in (document.get("pages") or [])
            if isinstance(page, dict)
        ],
    }


def _load_saved_preview_chat_document(job_id: str | None) -> dict | None:
    if not isinstance(job_id, str) or not job_id.strip():
        return None
    try:
        saved_document = read_case_document(job_id=job_id.strip())
    except FileNotFoundError:
        logger.warning("Could not load saved preview document for chat context: %s", job_id)
        return None
    return _normalize_saved_document_for_chat(saved_document)


def _resolve_chat_document(upload_document: dict | None, active_preview_job_id: str | None) -> dict | None:
    preview_document = _load_saved_preview_chat_document(active_preview_job_id)
    if preview_document:
        return preview_document
    if isinstance(upload_document, dict):
        return upload_document
    return None


def _gmail_account_summaries(active_account_id: str | None) -> list[dict]:
    attached_ids = set(get_attached_gmail_account_ids())
    local_ids = set(gmail_storage.list_account_ids_with_state())
    known_account_ids = sorted(attached_ids | local_ids)
    summaries = []
    for account_id in known_account_ids:
        state = gmail_storage.load_sync_state(account_id)
        summaries.append(
            {
                "account_id": account_id,
                "is_active": account_id == active_account_id,
                "has_token": account_has_refresh_token(account_id),
                "has_local_state": bool(state),
                "message_count": state.get("messageCount", 0),
                "last_sync_at": state.get("lastScanAt"),
            }
        )
    return summaries


def _decorate_chronology_event(event: dict) -> dict:
    decorated = dict(event)
    sources = []
    primary_email_source = None

    account_id = get_active_account_id()
    for source in event.get("sources") or []:
        source_copy = dict(source)
        if source_copy.get("sourceType") == "email" and account_id:
            message = load_saved_message(account_id, source_copy.get("sourceId", ""))
            if message:
                from_obj = message.get("from") or {}
                sender_name = from_obj.get("name") or from_obj.get("email") or "Unknown sender"
                sender_email = from_obj.get("email", "")
                source_copy["senderName"] = sender_name
                source_copy["senderEmail"] = sender_email
                source_copy["receivedAt"] = message.get("receivedAt", "")
                if primary_email_source is None:
                    primary_email_source = {
                        "senderName": sender_name,
                        "senderEmail": sender_email,
                        "receivedAt": message.get("receivedAt", ""),
                        "subject": message.get("subject", ""),
                    }
        sources.append(source_copy)

    decorated["sources"] = sources
    decorated["primaryEmailSource"] = primary_email_source
    return decorated


@app.post("/api/chat")
def api_chat():
    body = request.get_json(silent=True) or {}
    messages = _validate_messages(body.get("messages"))
    if messages is None:
        return jsonify({"error": "Request must include a non-empty 'messages' list of {role, content}."}), 400

    latest_message = messages[-1]["content"].strip()
    if latest_message == "CONFIRM":
        result = confirm_pending_delete()
        return jsonify({"reply": result.get("message", "Delete request processed."), "deleteResult": result})

    document = _resolve_chat_document(
        body.get("document"),
        body.get("active_preview_job_id"),
    )

    account_id = get_active_account_id()
    pending_email_count = len(get_pending_candidates(account_id)) if account_id else 0

    try:
        reply = get_reply(messages, document=document, pending_email_count=pending_email_count)
    except OpenAIServiceError as exc:
        return jsonify({"error": str(exc)}), 502

    response = {"reply": reply}
    pending_send = load_pending_send()
    if pending_send:
        response["pendingLinkedinSend"] = {
            "person_name": pending_send.get("person_name"),
            "message_text": pending_send.get("message_text"),
        }
    pending_email_send = email_send_flow.load_pending_send()
    if pending_email_send:
        response["pendingEmailSend"] = {
            "person_name": pending_email_send.get("person_name"),
            "to_email": pending_email_send.get("to_email"),
            "subject": pending_email_send.get("subject"),
            "body": pending_email_send.get("body"),
        }
    return jsonify(response)


@app.get("/api/linkedin/send-pending")
def linkedin_send_pending_get():
    # Polled by the UI so a follow-up draft the scheduler generated on its
    # own (nobody was actively chatting when it fired) still surfaces the
    # Send now button, not just ones created mid-conversation.
    pending = load_pending_send()
    if not pending:
        return jsonify({"pending": None})
    return jsonify({
        "pending": {
            "person_name": pending.get("person_name"),
            "message_text": pending.get("message_text"),
        }
    })


@app.post("/api/linkedin/send-pending/confirm")
def linkedin_send_pending_confirm():
    return jsonify(confirm_pending_send())


@app.post("/api/linkedin/send-pending/cancel")
def linkedin_send_pending_cancel():
    clear_pending_send()
    return jsonify({"ok": True})


@app.post("/api/email/send-pending/confirm")
def email_send_pending_confirm():
    return jsonify(email_send_flow.confirm_pending_send())


@app.post("/api/email/send-pending/cancel")
def email_send_pending_cancel():
    email_send_flow.clear_pending_send()
    return jsonify({"ok": True})


@app.get("/auth/google/start")
def google_oauth_start():
    # Kicks off the flow: browser navigates here, we 302 straight to
    # Google's consent screen. All client_id/secret handling stays server-side.
    try:
        url = build_authorization_url()
    except GoogleOAuthError as exc:
        return redirect(f"/?gmail=error&reason={urllib.parse.quote(str(exc))}")
    return redirect(url)


@app.get("/auth/google/callback")
def google_oauth_callback():
    error = request.args.get("error")
    if error:
        return redirect(f"/?gmail=error&reason={urllib.parse.quote(error)}")

    code = request.args.get("code")
    if not code:
        return redirect("/?gmail=error&reason=missing_code")

    try:
        tokens = exchange_code_for_tokens(code)
        profile = gmail_client.get_profile(tokens.get("access_token"))
    except GoogleOAuthError as exc:
        return redirect(f"/?gmail=error&reason={urllib.parse.quote(str(exc))}")

    account_id = profile.get("emailAddress")
    if not account_id:
        return redirect("/?gmail=error&reason=missing_account_email")

    save_tokens(tokens, account_id=account_id)
    set_active_account_id(account_id)
    clear_legacy_tokens()

    return redirect("/?gmail=connected")


@app.get("/api/google/status")
def google_oauth_status():
    return jsonify({"connected": has_refresh_token()})


@app.get("/api/gmail/status")
def gmail_status():
    ensure_profile_bootstrap()
    account_id = get_active_account_id()
    active_profile = get_active_profile()
    active_profile_id = active_profile.get("profile_id")
    active_shell_variant = active_profile.get("shell_variant", "default")
    known_accounts = _gmail_account_summaries(account_id)
    default_sync_since_date = GmailSyncConfig.from_env().sync_since_date
    if not account_id:
        return jsonify(
            {
                "connected": has_refresh_token(),
                "synced": False,
                "message_count": 0,
                "last_sync_at": None,
                "account_email": None,
                "pending_count": 0,
                "active_account_id": None,
                "active_profile_id": active_profile_id,
                "active_shell_variant": active_shell_variant,
                "known_accounts": known_accounts,
                "default_sync_since_date": default_sync_since_date,
            }
        )
    state = load_sync_state(account_id)
    return jsonify(
        {
            "connected": has_refresh_token(),
            "synced": bool(state),
            "message_count": state.get("messageCount", 0),
            "last_sync_at": state.get("lastScanAt"),
            "account_email": state.get("accountEmail") or account_id,
            "pending_count": len(get_pending_candidates(account_id)),
            "active_account_id": account_id,
            "active_profile_id": active_profile_id,
            "active_shell_variant": active_shell_variant,
            "known_accounts": known_accounts,
            "default_sync_since_date": default_sync_since_date,
        }
    )


@app.get("/api/crm/overview")
def crm_overview():
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        return jsonify(crm_db.get_overview(conn))
    finally:
        conn.close()


@app.get("/api/crm/recent-activity")
def crm_recent_activity():
    crm_db.init_db()
    limit = request.args.get("limit", default=5, type=int)
    conn = crm_db.get_connection()
    try:
        return jsonify({"activity": crm_db.list_recent_activity(conn, limit=limit)})
    finally:
        conn.close()


@app.get("/api/crm/people/<int:person_id>")
def crm_person_detail(person_id):
    conn = crm_db.get_connection()
    try:
        detail = crm_db.get_person_detail(conn, person_id)
    finally:
        conn.close()
    if detail is None:
        return jsonify({"error": "No such person."}), 404
    return jsonify(detail)


@app.get("/api/crm/funnels")
def crm_list_funnels():
    conn = crm_db.get_connection()
    try:
        return jsonify({"funnels": crm_db.list_funnels(conn)})
    finally:
        conn.close()


@app.post("/api/crm/funnels")
def crm_create_funnel():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Funnel name is required."}), 400
    conn = crm_db.get_connection()
    try:
        funnel_id = crm_db.create_funnel(conn, name)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "funnel_id": funnel_id})


@app.post("/api/crm/funnels/<int:funnel_id>/delete")
def crm_delete_funnel(funnel_id):
    conn = crm_db.get_connection()
    try:
        crm_db.delete_funnel(conn, funnel_id)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/crm/funnels/move")
def crm_move_to_funnel():
    body = request.get_json(silent=True) or {}
    person_ids = body.get("person_ids")
    funnel_id = body.get("funnel_id")
    if not isinstance(person_ids, list) or not person_ids:
        return jsonify({"error": "person_ids (non-empty list) is required."}), 400
    conn = crm_db.get_connection()
    try:
        crm_db.move_people_to_funnel(conn, person_ids, funnel_id)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.get("/api/crm/funnels/<int:funnel_id>/board")
def crm_funnel_board(funnel_id):
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        board = crm_db.get_funnel_board(conn, funnel_id)
    finally:
        conn.close()
    if board is None:
        return jsonify({"error": "No such funnel."}), 404
    return jsonify(board)


@app.post("/api/crm/funnels/stages/move")
def crm_move_to_stage():
    body = request.get_json(silent=True) or {}
    person_id = body.get("person_id")
    stage_id = body.get("stage_id")
    if not person_id or not stage_id:
        return jsonify({"error": "person_id and stage_id are required."}), 400
    conn = crm_db.get_connection()
    try:
        crm_db.move_person_to_stage(conn, person_id, stage_id)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/crm/people/<int:person_id>/delete")
def crm_delete_person(person_id):
    conn = crm_db.get_connection()
    try:
        crm_db.delete_person(conn, person_id)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/crm/people/<int:person_id>/update")
def crm_update_person(person_id):
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    company_name = (body.get("company_name") or "").strip()
    job_title = (body.get("job_title") or "").strip()
    notes = body.get("notes")
    conn = crm_db.get_connection()
    try:
        crm_db.update_person(conn, person_id, name, company_name, job_title, notes)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/dev/reset-contact-for-testing")
def dev_reset_contact_for_testing():
    """Dev/test-only — never called from normal app flows. See
    crm_db.dev_reset_contact_for_testing() for exactly what this does and
    why it exists instead of just using delete_person()."""
    body = request.get_json(silent=True) or {}
    person_id = body.get("person_id")
    channel = body.get("channel")
    if not person_id or not channel:
        return jsonify({"error": "person_id and channel are required."}), 400
    conn = crm_db.get_connection()
    try:
        crm_db.dev_reset_contact_for_testing(conn, person_id, channel)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/crm/people/merge")
def crm_merge_people():
    body = request.get_json(silent=True) or {}
    keep_person_id = body.get("keep_person_id")
    merge_person_id = body.get("merge_person_id")
    if not isinstance(keep_person_id, int) or not isinstance(merge_person_id, int):
        return jsonify({"error": "keep_person_id and merge_person_id (integers) are required."}), 400
    conn = crm_db.get_connection()
    try:
        crm_db.merge_people(conn, keep_person_id, merge_person_id)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.get("/api/notifications/count")
def notifications_count():
    conn = crm_db.get_connection()
    try:
        return jsonify(crm_db.get_notification_counts(conn))
    finally:
        conn.close()


@app.get("/api/diagnostics")
def diagnostics():
    """The real step-by-step trace of recent operations (sync, sends,
    follow-up ticks) — see each component module's docstring for its
    documented step sequence. ?component=linkedin_sync to filter,
    ?runs=N for how many recent operations (default 10)."""
    component = request.args.get("component")
    runs = int(request.args.get("runs", 10))
    conn = crm_db.get_connection()
    try:
        return jsonify({"entries": crm_db.list_activity_log(conn, component=component, runs=runs)})
    finally:
        conn.close()


@app.get("/api/followups")
def followups_list():
    conn = crm_db.get_connection()
    try:
        return jsonify({"follow_ups": crm_db.list_all_follow_ups(conn)})
    finally:
        conn.close()


@app.post("/api/followups/<int:follow_up_id>/stop")
def followups_stop(follow_up_id):
    conn = crm_db.get_connection()
    try:
        row = conn.execute(
            "SELECT p.name AS person_name FROM follow_ups f "
            "JOIN people p ON p.person_id = f.person_id WHERE f.follow_up_id = ?",
            (follow_up_id,),
        ).fetchone()
        crm_db.end_follow_up(conn, follow_up_id, "stopped", "Skipped by user from Home.")
        conn.commit()
    finally:
        conn.close()

    # Skipping the sequence must also drop any draft already staged for it —
    # otherwise "Skip" here still leaves a live Send now button in chat/
    # LinkedIn for the exact message the user just said not to send.
    if row is not None:
        pending = load_pending_send()
        if pending and pending.get("person_name") == row["person_name"]:
            clear_pending_send()

    return jsonify({"ok": True})


@app.get("/api/linkedin/status")
def linkedin_status():
    # Starts the local LinkedIn MCP server and checks session status. If no
    # session is saved yet, the server itself opens a real login browser —
    # only reachable by explicitly clicking the LinkedIn tab in the UI.
    return jsonify(linkedin_client.get_status())


@app.get("/api/linkedin/messages")
def linkedin_messages():
    return jsonify(linkedin_client.search_messages())


@app.get("/api/linkedin/conversation")
def linkedin_conversation():
    ref = request.args.get("ref", "")
    if not ref:
        return jsonify({"ok": False, "error": "missing ref"}), 400
    return jsonify(linkedin_client.get_conversation(ref))


@app.get("/api/linkedin/crm/conversations")
def linkedin_crm_conversations():
    """What's actually saved in the CRM right now — reflects reset/backfill
    state, unlike /api/linkedin/messages which always live-searches LinkedIn.
    ?status=pending_review switches to the Inbox tab (unvetted, inbound-only
    senders); default is the normal trusted/main view."""
    status = request.args.get("status", "trusted")
    if status not in {"trusted", "pending_review"}:
        return jsonify({"error": "status must be 'trusted' or 'pending_review'."}), 400
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        conversations = crm_db.list_conversations(conn, "linkedin", contact_status=status)
    finally:
        conn.close()
    return jsonify({"ok": True, "conversations": conversations})


@app.post("/api/linkedin/crm/contacts/<int:account_id>/approve")
def linkedin_crm_contact_approve(account_id):
    conn = crm_db.get_connection()
    try:
        crm_db.set_contact_status(conn, account_id, "trusted")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.get("/api/linkedin/crm/conversation/<int:person_id>")
def linkedin_crm_conversation(person_id):
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        messages = crm_db.list_messages_for_person(conn, "linkedin", person_id)
    finally:
        conn.close()
    return jsonify({"ok": True, "messages": messages})


@app.get("/api/linkedin/sync/status")
def linkedin_sync_status():
    state = linkedin_sync_storage.load()
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        conversation_count = crm_db.count_people(conn, channel="linkedin")
        message_count = crm_db.count_messages(conn, channel="linkedin")
    finally:
        conn.close()
    return jsonify(
        {
            "backfilled": state.get("backfilled", False),
            "backfilled_at": state.get("backfilled_at"),
            "last_synced_at": state.get("last_synced_at"),
            "conversation_count": conversation_count,
            "message_count": message_count,
            "current_activity": linkedin_sync.get_current_activity(),
        }
    )


@app.post("/api/linkedin/backfill")
def linkedin_backfill():
    days = request.get_json(silent=True) or {}
    return jsonify(linkedin_sync.run_backfill(max_age_days=days.get("days", 7)))


@app.post("/api/linkedin/reset")
def linkedin_reset():
    return jsonify(linkedin_sync.reset())


@app.post("/api/linkedin/sync/run")
def linkedin_sync_run():
    body = request.get_json(silent=True) or {}
    return jsonify(linkedin_sync.run_incremental_sync(max_age_days=body.get("days", 7)))


@app.post("/api/gmail/accounts/activate")
def gmail_activate_account():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    known_account_ids = set(gmail_storage.list_account_ids_with_state()) | set(list_token_account_ids())
    if not isinstance(account_id, str) or not account_id or account_id not in known_account_ids:
        return jsonify({"error": "Unknown Gmail account profile."}), 404

    set_active_account_id(account_id)
    state = load_sync_state(account_id)
    return jsonify(
        {
            "connected": has_refresh_token(),
            "synced": bool(state),
            "message_count": state.get("messageCount", 0),
            "last_sync_at": state.get("lastScanAt"),
            "account_email": state.get("accountEmail") or account_id,
            "pending_count": len(get_pending_candidates(account_id)),
            "active_account_id": account_id,
            "active_profile_id": get_active_profile_id(),
            "known_accounts": _gmail_account_summaries(account_id),
        }
    )


def _profile_summary(profile: dict) -> dict:
    profile_id = profile.get("profile_id", "")
    return {
        **profile,
        "counts": profile_counts(profile_id),
    }


@app.get("/api/demo/profiles")
def demo_profiles_list():
    if not _is_local_demo_request():
        abort(404)
    ensure_profile_bootstrap()
    return jsonify(
        {
            "active_profile_id": get_active_profile_id(),
            "profiles": [_profile_summary(profile) for profile in list_local_profiles()],
        }
    )


@app.post("/api/demo/profiles")
def demo_profiles_create():
    if not _is_local_demo_request():
        abort(404)
    body = request.get_json(silent=True) or {}
    display_name = body.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        return jsonify({"error": "display_name is required."}), 400
    profile_id = body.get("profile_id")
    try:
        profile = create_profile(display_name.strip(), profile_id=profile_id if isinstance(profile_id, str) else None, is_demo_profile=True)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"profile": _profile_summary(profile)})


@app.post("/api/demo/profiles/seed-defaults")
def demo_profiles_seed_defaults():
    if not _is_local_demo_request():
        abort(404)
    profiles = seed_default_profiles()
    return jsonify({"profiles": [_profile_summary(profile) for profile in profiles]})


@app.post("/api/demo/profiles/<profile_id>/activate")
def demo_profiles_activate(profile_id):
    if not _is_local_demo_request():
        abort(404)
    try:
        profile = set_active_profile_id(profile_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"profile": _profile_summary(profile), "active_profile_id": profile.get("profile_id")})


@app.post("/api/demo/profiles/<profile_id>/rename")
def demo_profiles_rename(profile_id):
    if not _is_local_demo_request():
        abort(404)
    body = request.get_json(silent=True) or {}
    display_name = body.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        return jsonify({"error": "display_name is required."}), 400
    try:
        profile = rename_profile(profile_id, display_name.strip())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"profile": _profile_summary(profile)})


@app.post("/api/demo/profiles/<profile_id>/reset")
def demo_profiles_reset(profile_id):
    if not _is_local_demo_request():
        abort(404)
    try:
        profile = reset_profile_local_state(profile_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"profile": _profile_summary(profile), "reset": True})


@app.get("/api/gmail/messages")
def gmail_messages():
    return jsonify({"messages": list_drawer_records()})


@app.get("/api/gmail/messages/<message_id>")
def gmail_message_detail(message_id):
    # On-demand full fetch for a message that was never saved (so we only
    # ever had lightweight scan-state info for it) — live from Gmail,
    # not persisted unless the user later approves it via save-candidates.
    record = get_message_detail(message_id)
    if not record:
        return jsonify({"error": "Message not found."}), 404
    return jsonify(record)


@app.post("/api/gmail/scan")
def gmail_scan():
    # Scans for new/changed Gmail messages (full 90-day window on first
    # run, historyId-based diff afterward — see gmail.sync.scan_for_candidates),
    # classifies each new one for likely case relevance via the OpenAI
    # classifier, and stores only a lightweight scan-state entry for each.
    # Nothing is permanently saved here — new messages become pending
    # candidates for the user to approve via /api/gmail/save-candidates.
    body = request.get_json(silent=True) or {}
    since_date = (body.get("since") or "").strip() or None
    try:
        result = scan_for_candidates(since_date=since_date)
        result["candidates"] = get_pending_candidates(result["account_id"])
    except ValueError:
        return jsonify({"error": f"Invalid date: {since_date!r}. Use YYYY-MM-DD."}), 400
    except (GmailSyncError, GoogleOAuthError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.get("/api/gmail/scan/progress")
def gmail_scan_progress():
    account_id = get_active_account_id()
    if not account_id:
        return jsonify({"phase": "idle"})
    return jsonify(gmail_storage.load_scan_progress(account_id))


@app.get("/api/gmail/candidates")
def gmail_candidates():
    account_id = get_active_account_id()
    if not account_id:
        return jsonify({"candidates": []})
    return jsonify({"candidates": get_pending_candidates(account_id)})


@app.post("/api/gmail/save-candidates")
def gmail_save_candidates():
    account_id = get_active_account_id()
    if not account_id:
        return jsonify({"error": "No Gmail account has been scanned yet."}), 400

    body = request.get_json(silent=True) or {}
    approved_ids = body.get("ids")
    if not isinstance(approved_ids, list):
        return jsonify({"error": "Request must include an 'ids' list (use every pending candidate id to Save All)."}), 400
    dismissed_ids = body.get("dismissed_ids")
    if dismissed_ids is not None and not isinstance(dismissed_ids, list):
        return jsonify({"error": "'dismissed_ids', if given, must be a list."}), 400

    result = save_candidates(account_id, approved_ids, dismissed_ids)

    # save_candidates() only persists the message under gmail/storage —
    # it doesn't know about the CRM. Without this, a message approved
    # through the drawer's real Save flow (this endpoint) never became a
    # CRM contact, unlike /api/gmail/save-message which already did this.
    from gmail import storage as gmail_storage

    crm_db.init_db()
    crm_conn = crm_db.get_connection()
    try:
        for message_id in approved_ids:
            message = gmail_storage.load_message(account_id, message_id)
            if message:
                _crm_write_email(crm_conn, message)
        crm_conn.commit()
    finally:
        crm_conn.close()

    return jsonify(result)


@app.get("/api/documents/registry")
def documents_registry():
    # Runs the daily cross-reference check if >=24h have passed since the
    # last one (see doc_registry.scheduler); otherwise returns the existing
    # registry as-is. Full rebuild each run, keyed by stable ids — re-running
    # never creates duplicates.
    entries = maybe_run_daily_check()
    return jsonify({"entries": list(entries.values()), "counts": compute_counts(entries)})


@app.post("/api/documents/recheck")
def documents_recheck():
    entries = run_check_now()
    return jsonify({"entries": list(entries.values()), "counts": compute_counts(entries)})


@app.post("/api/documents/dismiss")
def documents_dismiss():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "Request must include a non-empty 'ids' list."}), 400
    entries = dismiss_entries(ids)
    return jsonify({"entries": list(entries.values()), "counts": compute_counts(entries)})


@app.get("/api/documents/queue")
def documents_queue_get():
    return jsonify({"queue": queue_storage.load_queue()})


@app.post("/api/documents/queue")
def documents_queue_add():
    # Confirms specific registry entries for scanning — this is the only
    # step that requires the user's explicit action; nothing is downloaded
    # yet, just queued.
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list):
        return jsonify({"error": "Request must include an 'ids' list."}), 400

    entries = maybe_run_daily_check()
    selected = [entries[entry_id] for entry_id in ids if entry_id in entries]
    queue = queue_storage.add_to_queue(selected)
    return jsonify({"queue": queue})


@app.post("/api/documents/run-scan")
def documents_run_scan():
    # The only route that actually downloads attachment bytes from Gmail —
    # only ever runs against what the user explicitly queued.
    result = run_queued_scans()
    return jsonify(result)


@app.get("/api/documents/ocr-text/<job_id>")
def documents_ocr_text(job_id):
    from doc_registry.config import get_ocr_storage_root

    path = get_ocr_storage_root() / "jobs" / job_id / "results" / "extracted-text.txt"
    if not path.is_file():
        return jsonify({"error": "Not found."}), 404
    return jsonify({"text": path.read_text()})


@app.post("/api/chronology/extract")
def chronology_extract():
    # Only processes sources not already in extraction-state.json — see
    # chronology.pipeline.run_extraction. Safe to call repeatedly.
    result = run_extraction(extract_events_from_email_batch, extract_events_from_document)
    return jsonify(result)


@app.get("/api/chronology/events")
def chronology_events():
    events = chronology_storage.load_events()
    return jsonify({"events": [_decorate_chronology_event(event) for event in events.values()]})


@app.get("/api/chronology/review-queue")
def chronology_review_queue_get():
    queue = chronology_storage.load_review_queue()
    return jsonify({"queue": [_decorate_chronology_event(event) for event in queue.values()]})


@app.post("/api/chronology/review/<event_id>/approve")
def chronology_approve(event_id):
    queue = chronology_storage.load_review_queue()
    event = queue.pop(event_id, None)
    if not event:
        return jsonify({"error": "Not found."}), 404
    event["status"] = "confirmed"
    event["reviewReason"] = ""
    events = chronology_storage.load_events()
    events[event_id] = event
    chronology_storage.save_events(events)
    chronology_storage.save_review_queue(queue)
    return jsonify({"event": event})


@app.post("/api/chronology/review/<event_id>/reject")
def chronology_reject(event_id):
    queue = chronology_storage.load_review_queue()
    if event_id not in queue:
        return jsonify({"error": "Not found."}), 404
    del queue[event_id]
    chronology_storage.save_review_queue(queue)
    return jsonify({"status": "rejected"})


@app.post("/api/chronology/review/<event_id>/edit")
def chronology_edit(event_id):
    body = request.get_json(silent=True) or {}
    queue = chronology_storage.load_review_queue()
    event = queue.get(event_id)
    if not event:
        return jsonify({"error": "Not found."}), 404

    editable_fields = {
        "title", "description", "category", "eventDate", "dateRangeFrom",
        "dateRangeTo", "recordedAt", "involvedParties", "natureOfClaim", "importance",
    }
    for key, value in body.items():
        if key in editable_fields:
            event[key] = value
    chronology_storage.save_review_queue(queue)
    return jsonify({"event": event})


@app.post("/api/chronology/<event_id>/edit")
def chronology_edit_any(event_id):
    body = request.get_json(silent=True) or {}
    editable_fields = {
        "title", "description", "category", "eventDate", "dateRangeFrom",
        "dateRangeTo", "recordedAt", "involvedParties", "natureOfClaim", "importance",
    }

    events = chronology_storage.load_events()
    event = events.get(event_id)
    if event is not None:
        for key, value in body.items():
            if key in editable_fields:
                event[key] = value
        event["updatedAt"] = datetime.now(timezone.utc).isoformat()
        chronology_storage.save_events(events)
        return jsonify({"event": event})

    queue = chronology_storage.load_review_queue()
    event = queue.get(event_id)
    if event is not None:
        for key, value in body.items():
            if key in editable_fields:
                event[key] = value
        event["updatedAt"] = datetime.now(timezone.utc).isoformat()
        chronology_storage.save_review_queue(queue)
        return jsonify({"event": event})

    return jsonify({"error": "Not found."}), 404


@app.post("/api/chronology/<event_id>/delete")
def chronology_delete_any(event_id):
    events = chronology_storage.load_events()
    if event_id in events:
        del events[event_id]
        chronology_storage.save_events(events)
        return jsonify({"status": "deleted", "scope": "confirmed"})

    queue = chronology_storage.load_review_queue()
    if event_id in queue:
        del queue[event_id]
        chronology_storage.save_review_queue(queue)
        return jsonify({"status": "deleted", "scope": "review"})

    return jsonify({"error": "Not found."}), 404


@app.post("/api/chronology/review/<event_id>/merge")
def chronology_merge(event_id):
    body = request.get_json(silent=True) or {}
    target_id = body.get("mergeIntoId")
    queue = chronology_storage.load_review_queue()
    event = queue.get(event_id)
    if not event or not target_id:
        return jsonify({"error": "Missing event or target."}), 400

    events = chronology_storage.load_events()
    target = events.get(target_id) or queue.get(target_id)
    if not target:
        return jsonify({"error": "Merge target not found."}), 404

    existing_source_keys = {(s["sourceType"], s["sourceId"]) for s in target["sources"]}
    for s in event["sources"]:
        if (s["sourceType"], s["sourceId"]) not in existing_source_keys:
            target["sources"].append(s)

    del queue[event_id]
    chronology_storage.save_events(events)
    chronology_storage.save_review_queue(queue)
    return jsonify({"event": target})


@app.get("/api/chronology/overview")
def chronology_overview():
    events = list(chronology_storage.load_events().values())
    queue = chronology_storage.load_review_queue()
    today = datetime.now(timezone.utc).date().isoformat()

    upcoming = sorted(
        (e for e in events if e.get("eventDate") and e["eventDate"][:10] >= today and e["category"] == "court_procedural"),
        key=lambda e: e["eventDate"],
    )[:5]
    deadlines = sorted(
        (e for e in events if e.get("eventDate") and e["eventDate"][:10] >= today and e["category"] == "deadline_request"),
        key=lambda e: e["eventDate"],
    )[:5]
    recent = sorted(events, key=lambda e: e.get("createdAt", ""), reverse=True)[:5]

    return jsonify({"upcoming": upcoming, "deadlines": deadlines, "recent": recent, "reviewCount": len(queue)})


@app.get("/api/demo/snapshots")
def demo_snapshot_list():
    if not _is_local_demo_request():
        abort(404)
    return jsonify({"snapshots": list_snapshots(), "manager": get_snapshot_manager_status()})


@app.post("/api/demo/snapshots")
def demo_snapshot_capture():
    if not _is_local_demo_request():
        abort(404)
    body = request.get_json(silent=True) or {}
    try:
        snapshot = capture_snapshot(
            body.get("name"),
            body.get("description", ""),
            is_baseline=bool(body.get("isBaseline")),
        )
    except SnapshotError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"snapshot": snapshot})


@app.post("/api/demo/snapshots/<name>/restore")
def demo_snapshot_restore(name):
    if not _is_local_demo_request():
        abort(404)
    try:
        result = restore_snapshot(name)
    except SnapshotError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/demo/reset")
def demo_reset_to_baseline():
    if not _is_local_demo_request():
        abort(404)
    try:
        result = reset_to_baseline()
    except SnapshotError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/demo/recordings")
def demo_recording_save():
    if not _is_local_demo_request():
        abort(404)

    upload = request.files.get("recording")
    if not upload:
        return jsonify({"error": "recording file is required."}), 400

    filename = request.form.get("filename", "")
    try:
        saved = save_demo_recording(filename, upload.read())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(saved)


@app.get("/api/demo/recordings")
def demo_recording_list():
    if not _is_local_demo_request():
        abort(404)
    return jsonify({"recordings": list_demo_recordings()})


@app.post("/api/demo/recordings/redact")
def demo_recording_redact():
    if not _is_local_demo_request():
        abort(404)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON payload is required."}), 400

    try:
        saved = render_redacted_recording(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except subprocess.CalledProcessError as exc:
        logger.exception("ffmpeg redaction export failed")
        message = exc.stderr.decode("utf-8", errors="ignore").strip() if exc.stderr else "ffmpeg export failed."
        return jsonify({"error": message or "ffmpeg export failed."}), 500
    return jsonify(saved)


@app.get("/api/demo/recordings/<path:filename>")
def demo_recording_open(filename):
    if not _is_local_browser_request():
        abort(404)
    safe_name = Path(filename).name
    path = APP_DIR / "demo_state" / "recordings" / safe_name
    if not path.is_file():
        abort(404)
    mime_type = "video/mp4" if path.suffix.lower() == ".mp4" else "video/webm"
    return send_file(path, mimetype=mime_type, download_name=safe_name)


@app.get("/api/files")
def api_files():
    raw_limit = request.args.get("limit")
    limit = 5
    if raw_limit:
      try:
          limit = None if raw_limit == "all" else max(1, int(raw_limit))
      except ValueError:
          limit = 5
    return jsonify({"files": list_recent_scans(limit=limit)})


@app.get("/api/files/<job_id>/original")
def api_file_original(job_id):
    path = get_original_file_path(job_id)
    if not path or not path.is_file():
        abort(404)
    return send_file(path, download_name=path.name)


@app.get("/api/gmail/messages/<message_id>/attachments/<attachment_id>/raw")
def gmail_attachment_raw(message_id, attachment_id):
    # Read-only preview of a raw, not-yet-scanned attachment — deliberately
    # separate from the OCR/photo-review pipeline: this never writes
    # anything to disk or triggers a scan, it only streams bytes for the
    # browser to render (PDF in an iframe, image directly).
    try:
        message = load_full_message(message_id)
    except GmailSyncError as exc:
        return jsonify({"error": str(exc)}), 502
    if not message:
        return jsonify({"error": "Could not load that email."}), 404

    attachment = next(
        (a for a in message.get("attachments") or [] if str(a.get("attachmentId", "")) == attachment_id),
        None,
    )
    if not attachment:
        return jsonify({"error": "That attachment was not found on this email."}), 404

    try:
        data = gmail_client.download_attachment_bytes(message_id, attachment_id)
    except GmailSyncError as exc:
        return jsonify({"error": str(exc)}), 502

    return send_file(
        io.BytesIO(data),
        mimetype=attachment.get("mimeType") or "application/octet-stream",
        download_name=attachment.get("filename") or "attachment",
    )


@app.post("/api/files/<job_id>/rename")
def api_file_rename(job_id):
    body = request.get_json(silent=True) or {}
    filename = body.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return jsonify({"error": "filename is required."}), 400

    result = rename_saved_document(job_id, filename)
    if result.get("status") == "failed":
        return jsonify({"error": "; ".join(result.get("errors") or ["Could not rename saved document."])}), 400

    return jsonify(
        {
            "job_id": result.get("job_id", job_id),
            "filename": result.get("original_filename", filename.strip()),
            "page_count": result.get("page_count", 0),
            "status": result.get("status", "unknown"),
            "full_text": result.get("full_text", ""),
            "pages": [{"page_number": p["page_number"], "text": p["text"]} for p in result.get("pages", [])],
            "warnings": result.get("warnings", []),
        }
    )


@app.post("/api/files/<job_id>/delete")
def api_file_delete(job_id):
    result = delete_scanned_document(job_id)
    if result.get("status") == "not_found":
        return jsonify(result), 404
    return jsonify(result)


@app.post("/api/photo-review/start")
def api_photo_review_start():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No image files provided."}), 400

    payload = [(upload.filename or f"image-{index}.png", upload.read()) for index, upload in enumerate(files, start=1)]
    result = start_photo_review(payload)
    if result.get("status") != "ready":
        return jsonify({"error": "; ".join(result.get("errors") or ["Could not prepare photographed pages."])}), 400
    return jsonify(result)


@app.post("/api/photo-review/from-email")
def api_photo_review_from_email():
    body = request.get_json(silent=True) or {}
    message_id = body.get("messageId")
    attachment_ids = body.get("attachmentIds")
    if not isinstance(message_id, str) or not message_id:
        return jsonify({"error": "messageId is required."}), 400
    if attachment_ids is not None and not isinstance(attachment_ids, list):
        return jsonify({"error": "attachmentIds must be a list when provided."}), 400

    message = load_full_message(message_id)
    if not message:
        return jsonify({"error": "Could not load that email."}), 404

    attachments = message.get("attachments") or []
    allowed_ids = {str(a.get("attachmentId", "")) for a in attachments}
    if attachment_ids is not None and any(str(aid) not in allowed_ids for aid in attachment_ids):
        return jsonify({"error": "One or more attachmentIds do not belong to that email."}), 400

    selected = [
        attachment
        for attachment in attachments
        if is_photo_reviewable_attachment(attachment)
        and (attachment_ids is None or attachment.get("attachmentId") in attachment_ids)
    ]
    if not selected:
        return jsonify({"error": "No reviewable image attachments were found on that email."}), 400

    try:
        payload = [
            (
                attachment.get("filename") or f"attachment-{index}.png",
                gmail_client.download_attachment_bytes(message_id, attachment["attachmentId"]),
            )
            for index, attachment in enumerate(selected, start=1)
        ]
    except GmailSyncError as exc:
        return jsonify({"error": str(exc)}), 502

    source = {
        "kind": "email",
        "messageId": message_id,
        "subject": message.get("subject") or "(no subject)",
        "attachments": [
            {
                "messageId": message_id,
                "attachmentId": attachment.get("attachmentId"),
                "filename": attachment.get("filename") or "",
            }
            for attachment in selected
        ],
    }
    result = start_photo_review(payload, source=source)
    if result.get("status") != "ready":
        return jsonify({"error": "; ".join(result.get("errors") or ["Could not prepare photographed pages."])}), 400
    for attachment in selected:
        if attachment.get("attachmentId"):
            set_pending_review(message_id, attachment["attachmentId"], result["review"]["job_id"])
    return jsonify(result)


def _crm_write_email(conn, message: dict) -> None:
    """Writes one saved Gmail message into the CRM, filed under whichever
    side of the conversation isn't the user (the recipient for an outgoing
    message, the sender for an incoming one) — same 'person = the contact,
    not me' convention LinkedIn already uses. No subject column exists on
    Messages, so it's folded into the stored text instead of added as a
    new column."""
    from_obj = message.get("from") or {}
    to_list = message.get("to") or []
    label_ids = message.get("labelIds") or []
    direction = "outgoing" if "SENT" in label_ids else "incoming"
    contact = (to_list[0] if to_list else {}) if direction == "outgoing" else from_obj
    contact_email = contact.get("email") or ""
    if not contact_email:
        return
    contact_name = contact.get("name") or contact_email

    if crm_db.is_contact_excluded(conn, "email", contact_email):
        return  # explicitly removed by the user — never silently re-created

    person_id, account_id = crm_db.get_or_create_person_for_channel(
        conn, "email", contact_email, contact_name
    )
    subject = message.get("subject") or ""
    body_text = message.get("textBody") or message.get("snippet") or ""
    full_text = f"Subject: {subject}\n\n{body_text}" if subject else body_text
    crm_db.insert_message(
        conn,
        person_id=person_id,
        account_id=account_id,
        thread_id=message.get("threadId"),
        channel="email",
        sender=from_obj.get("name") or from_obj.get("email") or "",
        direction=direction,
        message_text=full_text,
        timestamp=message.get("receivedAt") or message.get("sentAt") or "",
        external_message_id=message.get("id"),
    )


@app.post("/api/gmail/save-message")
def gmail_save_message():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "Request must include a non-empty 'ids' list."}), 400

    account_id = get_active_account_id()
    if not account_id:
        return jsonify({"error": "No Gmail account has been scanned yet."}), 400

    from gmail import storage as gmail_storage

    scan_state = gmail_storage.load_scan_state(account_id)
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    failed_ids = []

    crm_db.init_db()
    crm_conn = crm_db.get_connection()

    for message_id in ids:
        if not isinstance(message_id, str) or not message_id:
            continue
        message = load_full_message(message_id)
        if not message:
            failed_ids.append(message_id)
            continue
        gmail_storage.save_message(account_id, message)
        gmail_storage.upsert_index_entry(account_id, message)
        _crm_write_email(crm_conn, message)
        existing = scan_state.get(message_id, {})
        scan_state[message_id] = {
            **existing,
            "status": "saved",
            "isRelevant": True,
            "reason": existing.get("reason") or "Marked as related to the case by the user.",
            "lastScannedAt": now,
            "historyId": message.get("historyId", existing.get("historyId", "")),
            "senderName": (message.get("from") or {}).get("name") or (message.get("from") or {}).get("email") or "",
            "senderEmail": (message.get("from") or {}).get("email", ""),
            "subject": message.get("subject") or "(no subject)",
            "receivedAt": message.get("receivedAt", ""),
            "snippet": message.get("snippet", ""),
            "hasAttachments": message.get("hasAttachments", False),
            "unread": message.get("isUnread", False),
            "labelIds": message.get("labelIds", []),
        }
        gmail_storage.delete_pending(account_id, message_id)
        saved += 1

    crm_conn.commit()
    crm_conn.close()

    if saved == 0 and failed_ids:
        return jsonify(
            {
                "error": "Could not load the selected email from Gmail. Open the email once and try again.",
                "failed_ids": failed_ids,
            }
        ), 502

    gmail_storage.save_scan_state(account_id, scan_state)
    sync_state = gmail_storage.load_sync_state(account_id)
    sync_state["messageCount"] = len(gmail_storage.load_index(account_id))
    gmail_storage.save_sync_state(account_id, sync_state)
    payload = {"saved": saved, "state": sync_state}
    if failed_ids:
        payload["failed_ids"] = failed_ids
    return jsonify(payload)


@app.get("/api/photo-review/<job_id>/assets/<asset_kind>/<path:filename>")
def api_photo_review_asset(job_id, asset_kind, filename):
    path = get_asset_path(job_id, asset_kind, filename)
    if not path:
        abort(404)
    return send_file(path)


@app.post("/api/photo-review/<job_id>/preview")
def api_photo_review_preview(job_id):
    body = request.get_json(silent=True) or {}
    try:
        result = regenerate_preview(
            job_id,
            body.get("pageId"),
            body.get("corners") or [],
            body.get("rotationTurns") or 0,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if result.get("status") != "updated":
        return jsonify({"error": result.get("message", "Could not refresh preview.")}), 404
    return jsonify(result)


@app.post("/api/photo-review/<job_id>/finalize")
def api_photo_review_finalize(job_id):
    body = request.get_json(silent=True) or {}
    result = finalize_photo_review(job_id, body.get("pages"), body.get("outputFilename"))
    if result.get("status") == "failed":
        return jsonify({"error": "; ".join(result.get("errors") or ["Could not finalize photographed pages."])}), 400
    response_payload = _build_photo_naming_response(result)
    logger.warning(
        "photo_review_finalize_response job_id=%s keys=%s naming_source=%s suggested_filename=%s",
        response_payload["job_id"],
        sorted(response_payload.keys()),
        response_payload.get("naming_source"),
        response_payload.get("suggested_filename"),
    )
    return jsonify(response_payload)


@app.post("/api/ocr")
def api_ocr():
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    upload = request.files["file"]
    filename = upload.filename or "upload.pdf"
    file_bytes = upload.read()

    result = scan_upload(file_bytes, filename)

    if result["status"] == "failed":
        message = "; ".join(result.get("errors") or ["OCR failed for an unknown reason."])
        return jsonify({"error": message}), 502

    return jsonify(
        {
            "job_id": result["job_id"],
            "status": result["status"],
            "filename": result["original_filename"],
            "page_count": result["page_count"],
            "full_text": result["full_text"],
            "pages": [{"page_number": p["page_number"], "text": p["text"]} for p in result["pages"]],
            "warnings": result.get("warnings", []),
        }
    )


if __name__ == "__main__":
    linkedin_sync.start_scheduler()
    followup_engine.start_scheduler()

    # threaded=True: /api/gmail/scan can run for minutes on a large mailbox;
    # without this the single-threaded dev server can't answer the
    # progress-polling requests (/api/gmail/scan/progress) that run
    # alongside it, so scan progress would never actually be visible.
    app.run(host="127.0.0.1", port=5052, debug=False, threaded=True)
