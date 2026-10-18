"""LyteKeepr inside Claude (web, iPhone, Android): a remote MCP connector.

Two parts, both in this file:
- Sign-in for Claude (OAuth 2.1 with PKCE and dynamic client registration, which is what Claude's custom connectors use).
  The person signs in to LyteKeepr the usual way and presses Allow; Claude then gets a token for that person in that
  workspace. Tokens are stored only as hashes (profiles/directory.db), expire after an hour and renew for 30 days.
- The MCP endpoint (POST /mcp, stateless JSON-RPC). Every tool calls the app's own API inside the server, signed in as
  that person, so Claude sees and changes exactly what they could in the browser: the same team-member limits, access
  levels and workspace separation, with no second copy of those rules.

Phase 1 leaves out Google data (email text and Google Calendar meetings) until the privacy policy covers sending it to Claude.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 24 * 3600
CODE_SECONDS = 300
SCOPE = "lytekeepr"
REDIRECT_OK = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
LOCAL_REDIRECT = ("http://localhost:", "http://127.0.0.1:")          # Claude Code / Claude Desktop testing on your own computer
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CALLS_PER_MINUTE = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY, client_name TEXT NOT NULL DEFAULT '', redirect_uris TEXT NOT NULL,
    secret_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
    user_id INTEGER NOT NULL, profile_id TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, user_id INTEGER NOT NULL, profile_id TEXT NOT NULL,
    expires REAL NOT NULL, created_at TEXT NOT NULL, last_used TEXT NOT NULL DEFAULT ''
);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _db():
    from . import directory
    conn = directory._conn()
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ tokens
def _issue(conn, client_id: str, user_id: int, profile_id: str) -> dict:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("INSERT INTO oauth_tokens VALUES (?, 'access', ?, ?, ?, ?, ?, '')", (_hash(access), client_id, user_id, profile_id, now + ACCESS_SECONDS, _now()))
    conn.execute("INSERT INTO oauth_tokens VALUES (?, 'refresh', ?, ?, ?, ?, ?, '')", (_hash(refresh), client_id, user_id, profile_id, now + REFRESH_SECONDS, _now()))
    conn.execute("DELETE FROM oauth_tokens WHERE expires < ?", (now,))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS, "refresh_token": refresh, "scope": SCOPE}


def token_owner(access: str) -> dict | None:
    """(user_id, profile_id) for a valid access token, if that person may still come in."""
    if not access:
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'access' AND expires > ?", (_hash(access), time.time())).fetchone()
        if not row:
            return None
        conn.execute("UPDATE oauth_tokens SET last_used = ? WHERE token_hash = ?", (_now(), row["token_hash"]))
    return {"user_id": row["user_id"], "profile_id": row["profile_id"], "client_id": row["client_id"]}


def _may_sign_in(user_id: int, profile_id: str) -> bool:
    """The same checks as the login cookie: the workspace exists and is approved, the person is switched on."""
    from profiles.store import home_profile_id, list_profiles, use_profile
    from . import directory, users
    if profile_id != home_profile_id() and profile_id not in {p["profile_id"] for p in list_profiles()}:
        return False
    account = directory.account_for_profile(profile_id)
    if account and account["status"] != "active":
        return False
    with use_profile(profile_id):
        return bool(users.for_session(int(user_id or 0)))


def connections(user_id: int, profile_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("""SELECT t.client_id, c.client_name, MIN(t.created_at) AS since, MAX(t.last_used) AS last_used FROM oauth_tokens t
                               LEFT JOIN oauth_clients c ON c.client_id = t.client_id WHERE t.user_id = ? AND t.profile_id = ? AND t.expires > ?
                               GROUP BY t.client_id ORDER BY since DESC""", (user_id, profile_id, time.time())).fetchall()
    return [{"client_id": r["client_id"], "name": r["client_name"] or "Claude", "since": r["since"], "last_used": r["last_used"] or ""} for r in rows]


def disconnect(user_id: int, profile_id: str, client_id: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM oauth_tokens WHERE user_id = ? AND profile_id = ? AND client_id = ?", (user_id, profile_id, client_id))


# ------------------------------------------------------------------ tools (each one is a call to the app's own API, as the person)
def _strip_google(items: list) -> list:
    return [e for e in items or [] if e.get("source") != "google"]


TOOLS = [
    {"name": "find", "title": "Find a person, company or opportunity",
     "description": ("START HERE when the user names someone or something. Searches contacts AND opportunities at once and says what each match is: "
                     "a contact (with their own funnel and stage, if any) and every opportunity they are on (with their role), plus opportunities "
                     "whose name or company matches. A person can be a single contact entry in a funnel, a participant in an opportunity, or both."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, "read": True},
    {"name": "search_contacts", "title": "Search contacts",
     "description": "Find contacts in LyteKeepr by name, company, email or notes. Returns id, name, company, email, channel, funnel and stage.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "Words to look for; empty lists the newest contacts."},
                                                      "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}}, "read": True},
    {"name": "get_contact", "title": "Contact details",
     "description": "Everything about one contact: profile, notes, funnel and stage, LinkedIn/Instagram messages, follow-ups and their recorded calls (with summaries). Use search_contacts first to get the id.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}}, "required": ["contact_id"]}, "read": True},
    {"name": "list_funnels", "title": "List funnels", "description": "The sales funnels with their stages and how many people are in each.",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "get_funnel", "title": "Funnel board", "description": "One funnel: every stage with its contact entries (people, with their status) and its opportunity entries (shared deals with several people).",
     "inputSchema": {"type": "object", "properties": {"funnel_id": {"type": "integer"}}, "required": ["funnel_id"]}, "read": True},
    {"name": "get_agenda", "title": "Agenda", "description": "Tasks and reminders that are overdue, due today and coming up in the next 7 days.",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "list_calendar", "title": "Calendar items", "description": "Tasks, reminders and meetings created in LyteKeepr between two dates (inclusive).",
     "inputSchema": {"type": "object", "properties": {"start_date": {"type": "string", "description": "YYYY-MM-DD"}, "end_date": {"type": "string", "description": "YYYY-MM-DD"}},
                     "required": ["start_date", "end_date"]}, "read": True},
    {"name": "calls_to_review", "title": "Calls to review", "description": "Recorded calls with a transcript not summarized yet, and meetings that passed with no recording (possible no-shows).",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "get_call", "title": "Call transcript", "description": "One recorded call (from Fathom): who was on it, the summary if any, and the full transcript.",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}}, "required": ["call_id"]}, "read": True},
    {"name": "create_task", "title": "Create a task",
     "description": "Add a task to the LyteKeepr calendar. Use for follow-ups too (e.g. 'Follow up with Sara about pricing'). Today or later only.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM, 24-hour; leave out for no specific time"},
                                                      "notes": {"type": "string"}}, "required": ["title", "date"]}, "read": False},
    {"name": "create_reminder", "title": "Create a reminder", "description": "A reminder that shows on the LyteKeepr Home page under 'Action needed' from its due date.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"}},
                     "required": ["text", "due_date"]}, "read": False},
    {"name": "mark_task_done", "title": "Mark a task done", "description": "Mark a LyteKeepr task done (or not done) on a given date. Get the id from get_agenda or list_calendar.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "date": {"type": "string", "description": "YYYY-MM-DD of that occurrence"},
                                                      "done": {"type": "boolean", "default": True}}, "required": ["task_id", "date"]}, "read": False},
    # --- added for everyday phone use: the same actions the three LyteKeepr AI chats have ---
    {"name": "create_contact", "title": "Add a contact",
     "description": "Add a new contact (someone you just met or talked to). An Instagram handle goes in 'instagram' (never in the name); a LinkedIn link in 'linkedin'.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "The person's name; leave empty if only a handle is known"},
                                                      "company": {"type": "string"}, "job_title": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
                                                      "instagram": {"type": "string", "description": "Instagram handle like @name (or the instagram.com link)"},
                                                      "linkedin": {"type": "string", "description": "LinkedIn profile link"}, "notes": {"type": "string"}}}, "read": False},
    {"name": "update_contact", "title": "Update a contact",
     "description": "Change a contact's details, or add a note (add_note is appended with today's date; it never replaces the existing notes). Only the fields given change.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "name": {"type": "string"}, "company": {"type": "string"},
                                                      "job_title": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
                                                      "instagram": {"type": "string", "description": "Instagram handle like @name"}, "linkedin": {"type": "string", "description": "LinkedIn profile link"},
                                                      "add_note": {"type": "string"}}, "required": ["contact_id"]}, "read": False},
    {"name": "move_contact_stage", "title": "Move to a funnel stage",
     "description": "Move a contact to a stage of their funnel by the stage's name (e.g. 'In conversation'). Only the stage changes: outreach statuses and follow-ups stay as they are (use set_outreach_status for those). Give funnel_id to put them into a funnel they are not in yet.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "stage": {"type": "string"}, "funnel_id": {"type": "integer"}},
                     "required": ["contact_id", "stage"]}, "read": False},
    {"name": "set_outreach_status", "title": "Set outreach status",
     "description": "Set where outreach to a contact stands on a channel (linkedin, instagram or email); this re-plans their follow-up and can move their funnel card forward (never back). Use one of the options get_contact lists for that channel under outreach (e.g. 'email sent', 'replied', 'call booked').",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "channel": {"type": "string", "enum": ["linkedin", "instagram", "email"]},
                                                      "status": {"type": "string"}}, "required": ["contact_id", "channel", "status"]}, "read": False},
    {"name": "set_follow_up_date", "title": "Set follow-up date", "description": "Move the outreach follow-up for a contact to a date. The lead_id per channel is in get_contact under outreach (set_outreach_status creates one if missing).",
     "inputSchema": {"type": "object", "properties": {"lead_ids": {"type": "array", "items": {"type": "integer"}}, "date": {"type": "string", "description": "YYYY-MM-DD"}},
                     "required": ["lead_ids", "date"]}, "read": False},
    {"name": "update_calendar_item", "title": "Reschedule or rename", "description": "Change a LyteKeepr task, reminder or meeting: new date, time, title or notes. Google Calendar meetings can't be changed here.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM"}, "notes": {"type": "string"}}, "required": ["item_id"]}, "read": False},
    {"name": "move_overdue", "title": "Move overdue items", "description": "Move every overdue one-off task and follow-up to today or tomorrow.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string", "enum": ["today", "tomorrow"]}}, "required": ["to"]}, "read": False},
    {"name": "set_meeting_status", "title": "Meeting status", "description": "Mark a meeting scheduled, completed, no_show or cancelled, and optionally save your notes on it.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "status": {"type": "string", "enum": ["scheduled", "completed", "no_show", "cancelled"]},
                                                      "notes": {"type": "string"}}, "required": ["item_id", "status"]}, "read": False},
    {"name": "check_reply", "title": "Did they reply?", "description": "Did this contact write back after your last message? Works on LinkedIn, Instagram and email (for email only the date is shared, not the text).",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "Name of the contact"},
                                                      "channel": {"type": "string", "enum": ["", "linkedin", "instagram", "email"]}}, "required": ["contact"]}, "read": True},
    {"name": "search_messages", "title": "Search messages", "description": "Search LinkedIn and Instagram message history, for one contact or everyone.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string"}, "query": {"type": "string"}, "channel": {"type": "string", "enum": ["", "linkedin", "instagram"]},
                                                      "since_days": {"type": "integer", "default": 30}}}, "read": True},
    {"name": "book_meeting", "title": "Book a meeting",
     "description": ("Create a real meeting in the user's Google Calendar (their chosen meetings calendar) and email the invites from Google. "
                     "Adds a Google Meet link and marks it to be recorded by Fathom unless told otherwise. Today or later only. "
                     "This SENDS invitations: always confirm title, date, time and who is invited with the user first. "
                     "Invite people by contact name (as in LyteKeepr) or email address."),
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM, 24-hour, the user's local time"},
                                                      "duration_min": {"type": "integer", "default": 30}, "invite": {"type": "array", "items": {"type": "string"}, "description": "Contact names or email addresses"},
                                                      "google_meet": {"type": "boolean", "default": True}, "record_with_fathom": {"type": "boolean", "default": True},
                                                      "notes": {"type": "string"}}, "required": ["title", "date", "time"]}, "read": False},
    {"name": "cancel_meeting", "title": "Cancel a meeting", "destructive": True,
     "description": "Delete a meeting from the user's own Google Calendar; Google emails a cancellation to the people invited. Confirm with the user first. The id comes from get_agenda or list_calendar.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}, "read": False},
    # --- opportunities: a second kind of funnel entry (one shared deal/project with several people); contacts stay contacts ---
    {"name": "find_opportunities", "title": "Find opportunities", "description": "Find opportunities (shared deals, projects, partnerships) by name, company or a participant's name. These are NOT contacts.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}, "read": True},
    {"name": "get_opportunity", "title": "Opportunity details",
     "description": ("Everything about one opportunity: funnel and stage, the lead (primary=true, if any), every participant with company, active or not, "
                     "who introduced them, the history of changes, notes, tasks, and each active participant's latest messages and calls. "
                     "Only the listed participants are involved; other people at the same companies are not."),
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}}, "required": ["opportunity_id"]}, "read": True},
    {"name": "create_opportunity", "title": "Create an opportunity",
     "description": ("Create a NEW opportunity in a funnel only when the user asks for one (never convert a contact entry into an opportunity). "
                     "Participants are optional; no lead (primary) is set unless the user names one."),
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "funnel_id": {"type": "integer"}, "stage": {"type": "string", "description": "Stage name; default the first stage"},
                                                      "company": {"type": "string", "description": "Main company, optional"}, "notes": {"type": "string"},
                                                      "participants": {"type": "array", "items": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "primary": {"type": "boolean", "description": "true = the lead"}}, "required": ["contact_id"]}}},
                     "required": ["name", "funnel_id"]}, "read": False},
    {"name": "add_participant", "title": "Add a person to an opportunity",
     "description": "Add an existing contact to an opportunity, optionally as the lead (primary=true) and who introduced them. There are only two kinds of participant: the lead or a regular participant.",
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "contact_id": {"type": "integer"},
                                                      "primary": {"type": "boolean"}, "introduced_by_contact_id": {"type": "integer"}}, "required": ["opportunity_id", "contact_id"]}, "read": False},
    {"name": "update_participant", "title": "Change a participant",
     "description": ("Make a participant the lead (primary=true; the previous lead stops being the lead), set involved=false when they are "
                     "no longer involved (kept in the history), involved=true when they are back, or set who introduced them."),
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "contact_id": {"type": "integer"},
                                                      "primary": {"type": "boolean"}, "involved": {"type": "boolean"}, "introduced_by_contact_id": {"type": "integer"}},
                     "required": ["opportunity_id", "contact_id"]}, "read": False},
    {"name": "clear_primary_contact", "title": "No lead", "description": "Make all participants equal: the opportunity has no lead.",
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}}, "required": ["opportunity_id"]}, "read": False},
    {"name": "remove_participant", "title": "Remove a participant (added by mistake)",
     "description": ("Delete the link between a contact and an opportunity ONLY when it was added by mistake. If the person simply stopped being involved, "
                     "use update_participant with involved=false instead. The contact itself is never deleted."),
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "contact_id": {"type": "integer"}}, "required": ["opportunity_id", "contact_id"]},
     "read": False, "destructive": True},
    {"name": "move_opportunity_stage", "title": "Move an opportunity", "description": "Move an opportunity to another stage of its funnel, by the stage's name. Only when the user asks.",
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "stage": {"type": "string"}}, "required": ["opportunity_id", "stage"]}, "read": False},
    {"name": "add_opportunity_task", "title": "Task for an opportunity", "description": "Add a next-step task linked to an opportunity (shows in the LyteKeepr calendar). Today or later.",
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM"}}, "required": ["opportunity_id", "title", "date"]}, "read": False},
    # --- who is in touch with whom (counted from email addresses, no AI) ---
    {"name": "who_is_connected", "title": "Who is connected to a contact",
     "description": "The people a contact keeps appearing with in emails (From, To, CC), with how many separate threads they share. Facts from email headers, not guesses.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}}, "required": ["contact_id"]}, "read": True},
    {"name": "find_related_people", "title": "People who may belong to an opportunity",
     "description": ("People who appear in emails with an opportunity's active participants but are not on it yet, strongest first (threads shared), "
                     "with who brought them in when known. Show them to the user; add only the ones the user confirms (add_participant)."),
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}}, "required": ["opportunity_id"]}, "read": True},
    {"name": "list_suggestions", "title": "Suggested people",
     "description": "LyteKeepr's own suggestions from repeated email patterns: someone to add to an opportunity, or someone to save as a contact. Optionally for one opportunity.",
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}}}, "read": True},
    {"name": "accept_suggestion", "title": "Accept a suggestion", "description": "Do what a suggestion proposes (create the contact if needed, add them with who introduced them). Only when the user says so.",
     "inputSchema": {"type": "object", "properties": {"suggestion_id": {"type": "integer"}}, "required": ["suggestion_id"]}, "read": False},
    {"name": "dismiss_suggestion", "title": "Dismiss a suggestion", "description": "Dismiss a suggestion; it will not come back.",
     "inputSchema": {"type": "object", "properties": {"suggestion_id": {"type": "integer"}}, "required": ["suggestion_id"]}, "read": False},
    {"name": "draft_email", "title": "Draft an email",
     "description": "Write an email to one contact and leave it in LyteKeepr for the user to send. NOTHING is sent: the user opens LyteKeepr (Home) within 30 minutes and presses Send now.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "Name of the contact"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["contact", "subject", "body"]}, "read": False},
    {"name": "draft_linkedin_message", "title": "Draft a LinkedIn message",
     "description": "Write a LinkedIn message to one contact and leave it in LyteKeepr for the user to send. NOTHING is sent until they press Send now in LyteKeepr.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string"}, "text": {"type": "string"}}, "required": ["contact", "text"]}, "read": False},
]


def _run_tool(call, name: str, args: dict):
    """call(method, path, body) -> (status, json). Returns what Claude gets back."""
    def need(status, data):
        if status >= 400:
            raise ValueError((data or {}).get("error") or f"LyteKeepr answered with an error ({status})")
        return data

    if name == "find":
        text = str(args.get("query") or "").strip()
        q = urllib.parse.quote(text)
        contacts_found = need(*call("GET", f"/api/calendar/contacts?limit=8&q={q}"))["contacts"]
        people = []
        for c in contacts_found:
            st, opps = call("GET", f"/api/crm/people/{c['id']}/opportunities")
            people.append({"kind": "contact", "contact_id": c["id"], "name": c.get("display"), "company": c.get("company"), "email": c.get("email"), "instagram": c.get("instagram") or None,
                           "funnel": c.get("funnel_name") or None, "stage": c.get("stage_name") or None,
                           "opportunities": [{"opportunity_id": o["opportunity_id"], "name": o["name"], "funnel": o["funnel"], "stage": o["stage"],
                                              "role": "lead" if o["primary"] else "participant", "involved": o["active"]}
                                             for o in ((opps or {}).get("opportunities", []) if st < 400 else [])]})
        seen = {o["opportunity_id"] for p in people for o in p["opportunities"]}
        opps = [{"kind": "opportunity", "opportunity_id": o["opportunity_id"], "name": o["name"], "participants": o["summary"]}
                for o in need(*call("GET", f"/api/crm/opportunities?q={q}"))["opportunities"] if o["opportunity_id"] not in seen]
        return {"contacts": people, "other_matching_opportunities": opps,
                "note": "Nothing matched." if not people and not opps else ""}
    if name == "search_contacts":
        q = urllib.parse.urlencode({"q": str(args.get("query") or ""), "limit": max(1, min(int(args.get("limit") or 15), 50))})
        rows = need(*call("GET", f"/api/calendar/contacts?{q}"))["contacts"]
        return [{"id": c["id"], "name": c.get("display"), "company": c.get("company"), "email": c.get("email"), "instagram": c.get("instagram") or None, "channel": c.get("channel"),
                 "funnel": c.get("funnel_name") or "", "stage": c.get("stage_name") or "", "job_title": c.get("job_title")} for c in rows]
    if name == "get_contact":
        pid = int(args["contact_id"])
        detail = need(*call("GET", f"/api/crm/people/{pid}"))
        emails = [m for m in detail.get("messages") or [] if m.get("channel") == "email"]
        detail["messages"] = [m for m in detail.get("messages") or [] if m.get("channel") != "email"]
        if emails:
            detail["emails_not_shared"] = f"{len(emails)} email(s) exist in LyteKeepr; email text is not shared with Claude yet."
        status, outreach = call("GET", f"/api/calendar/people/{pid}/outreach")
        if status < 400:
            detail["outreach"] = outreach
        status, opps = call("GET", f"/api/crm/people/{pid}/opportunities")
        if status < 400:
            detail["opportunities"] = opps.get("opportunities", [])
        status, calls = call("GET", f"/api/calls/person/{pid}")
        detail["calls"] = [{"call_id": c.get("call_id"), "title": c.get("title"), "date": c.get("started_at"), "status": c.get("status"),
                            "summary": c.get("summary"), "has_transcript": c.get("has_transcript")} for c in (calls or {}).get("calls", [])] if status < 400 else []
        return detail
    if name == "list_funnels":
        return need(*call("GET", "/api/crm/funnels"))
    if name == "get_funnel":
        return need(*call("GET", f"/api/crm/funnels/{int(args['funnel_id'])}/board"))
    if name == "get_agenda":
        data = need(*call("GET", "/api/calendar/agenda"))
        return {k: _strip_google(data.get(k)) for k in ("overdue", "today", "upcoming")} | {"now": data.get("now")}
    if name == "list_calendar":
        q = urllib.parse.urlencode({"start": args["start_date"], "end": args["end_date"]})
        return _strip_google(need(*call("GET", f"/api/calendar/events?{q}"))["events"])
    if name == "calls_to_review":
        return need(*call("GET", "/api/calls/attention"))
    if name == "get_call":
        return need(*call("GET", f"/api/calls/{int(args['call_id'])}"))["call"]
    if name == "create_task":
        body = {"title": str(args.get("title") or "").strip(), "project": "lytekeepr", "kind": "task", "start_date": args.get("date"),
                "start_time": args.get("time") or "08:00", "untimed": not args.get("time"), "notes": str(args.get("notes") or "")}
        return need(*call("POST", "/api/calendar/events", body))
    if name == "create_reminder":
        return need(*call("POST", "/api/reminders", {"text": args.get("text"), "due_date": args.get("due_date")}))
    if name == "mark_task_done":
        return need(*call("POST", f"/api/calendar/events/{int(args['task_id'])}/done", {"date": args["date"], "done": bool(args.get("done", True))}))
    if name == "create_contact":
        body = {k: args[k] for k in ("name", "company", "job_title", "email", "phone", "instagram", "notes") if args.get(k)}
        if args.get("linkedin") or args.get("profile_url"):
            body["url"] = args.get("linkedin") or args.get("profile_url")
        c = need(*call("POST", "/api/calendar/contacts", body))
        return {"id": c.get("id"), "name": c.get("display"), "company": c.get("company"), "email": c.get("email"), "instagram": c.get("instagram"), "link": c.get("url")}
    if name == "update_contact":
        pid = int(args["contact_id"])
        body = {k: args[k] for k in ("name", "company", "job_title", "email", "phone", "instagram") if args.get(k)}
        if args.get("linkedin") or args.get("profile_url"):
            body["url"] = args.get("linkedin") or args.get("profile_url")
        if args.get("add_note"):
            current = need(*call("GET", f"/api/calendar/contacts/{pid}"))
            stamp = datetime.now().strftime("%Y-%m-%d")
            body["notes"] = ((current.get("notes") or "").rstrip() + f"\n\n{stamp}: {args['add_note'].strip()}").strip()
        if not body:
            raise ValueError("Nothing to change.")
        c = need(*call("PUT", f"/api/calendar/contacts/{pid}", body))
        return {"id": c.get("id"), "name": c.get("display"), "updated": sorted(body)}
    if name == "move_contact_stage":
        pid = int(args["contact_id"])
        person = need(*call("GET", f"/api/crm/people/{pid}"))
        funnel_id = args.get("funnel_id") or person.get("funnel_id")
        if not funnel_id:
            raise ValueError("This contact is not in a funnel yet. Give funnel_id (list_funnels shows them).")
        if int(funnel_id) != (person.get("funnel_id") or 0):
            need(*call("POST", "/api/crm/funnels/move", {"person_ids": [pid], "funnel_id": int(funnel_id)}))
        board = need(*call("GET", f"/api/crm/funnels/{int(funnel_id)}/board"))
        wanted = str(args["stage"]).strip().lower()
        stages = board.get("stages") or []
        match = [s for s in stages if (s.get("name") or "").lower() == wanted] or [s for s in stages if wanted in (s.get("name") or "").lower()]
        if len(match) != 1:
            raise ValueError("Stage not found. The stages are: " + ", ".join(s.get("name", "") for s in stages))
        need(*call("POST", "/api/crm/funnels/stages/move", {"person_id": pid, "stage_id": match[0]["stage_id"]}))
        return {"ok": True, "contact_id": pid, "stage": match[0]["name"]}
    if name == "set_outreach_status":
        return need(*call("POST", f"/api/calendar/people/{int(args['contact_id'])}/outreach", {"channel": args["channel"], "status": args["status"]}))
    if name == "set_follow_up_date":
        return [need(*call("PUT", f"/api/calendar/leads/{int(i)}", {"follow_up_date": args["date"]})) for i in args["lead_ids"]]
    if name == "update_calendar_item":
        body = {k: v for k, v in (("title", args.get("title")), ("start_date", args.get("date")), ("start_time", args.get("time")), ("notes", args.get("notes"))) if v}
        if args.get("time"):
            body["untimed"] = False
        if not body:
            raise ValueError("Nothing to change.")
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}", body))
    if name == "move_overdue":
        return need(*call("POST", "/api/calendar/overdue/move", {"to": args.get("to", "today")}))
    if name == "set_meeting_status":
        body = {"meeting_status": args["status"]}
        if args.get("notes"):
            body["my_notes"] = args["notes"]
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/meeting", body))
    if name == "book_meeting":
        body = {"title": args["title"], "start_date": args["date"], "start_time": args["time"], "duration_min": int(args.get("duration_min") or 30),
                "invite": ", ".join(str(x) for x in (args.get("invite") or [])), "meet": args.get("google_meet", True) is not False,
                "expect_recording": args.get("record_with_fathom", True) is not False, "notes": str(args.get("notes") or "")}
        m = need(*call("POST", "/api/calendar/meetings", body))
        return {"booked": True, "item_id": m.get("id"), "title": m.get("title"), "date": m.get("start_date"), "time": m.get("start_time"),
                "google_meet": m.get("join_url") or None, "invited": [a.get("email") for a in m.get("attendees") or []]}
    if name == "cancel_meeting":
        return need(*call("DELETE", f"/api/calendar/meetings/{int(args['item_id'])}"))
    if name == "find_opportunities":
        return need(*call("GET", "/api/crm/opportunities?q=" + urllib.parse.quote(str(args.get("query") or ""))))["opportunities"]
    if name == "get_opportunity":
        o = need(*call("GET", f"/api/crm/opportunities/{int(args['opportunity_id'])}"))
        o.pop("stages", None)
        for p in o.get("participants") or []:
            if not p.get("active"):
                continue
            st, d = call("GET", f"/api/crm/people/{p['contact_id']}")
            msgs = sorted((d or {}).get("messages") or [], key=lambda m: m.get("timestamp") or "")[-3:] if st < 400 else []
            p["latest_messages"] = [{"channel": m.get("channel"), "direction": m.get("direction"), "when": m.get("timestamp"),
                                     **({} if m.get("channel") == "email" else {"text": (m.get("message_text") or "")[:400]})} for m in msgs]   # email text stays in LyteKeepr
            st, c = call("GET", f"/api/calls/person/{p['contact_id']}")
            p["calls"] = [{"title": x.get("title"), "date": x.get("started_at"), "summary": x.get("summary")} for x in (c or {}).get("calls", []) if x.get("call_id")][:3] if st < 400 else []
        return o
    if name == "create_opportunity":
        board = need(*call("GET", f"/api/crm/funnels/{int(args['funnel_id'])}/board"))
        stage_id = None
        if args.get("stage"):
            want = str(args["stage"]).strip().lower()
            m = [s for s in board.get("stages") or [] if (s.get("name") or "").lower() == want] or [s for s in board.get("stages") or [] if want in (s.get("name") or "").lower()]
            if len(m) != 1:
                raise ValueError("Stage not found. The stages are: " + ", ".join(s.get("name", "") for s in board.get("stages") or []))
            stage_id = m[0]["stage_id"]
        o = need(*call("POST", "/api/crm/opportunities", {"name": args["name"], "funnel_id": int(args["funnel_id"]), "stage_id": stage_id, "company": args.get("company") or "",
                                                        "notes": args.get("notes") or "", "participants": args.get("participants") or []}))
        return {"opportunity_id": o["opportunity_id"], "name": o["name"], "stage": o["stage"], "participants": o["summary"]}
    if name == "add_participant":
        o = need(*call("POST", f"/api/crm/opportunities/{int(args['opportunity_id'])}/participants", {"contact_id": int(args["contact_id"]), "role": args.get("role") or "",
                                                                                                    "primary": bool(args.get("primary")), "introduced_by": args.get("introduced_by_contact_id")}))
        return {"participants": o["summary"]}
    if name == "update_participant":
        body = {k: v for k, v in (("role", args.get("role")), ("primary", args.get("primary")), ("active", args.get("involved")),
                                  ("introduced_by", args.get("introduced_by_contact_id"))) if v is not None}
        o = need(*call("PUT", f"/api/crm/opportunities/{int(args['opportunity_id'])}/participants/{int(args['contact_id'])}", body))
        return {"participants": o["participants"], "summary": o["summary"]}
    if name == "clear_primary_contact":
        return {"summary": need(*call("POST", f"/api/crm/opportunities/{int(args['opportunity_id'])}/clear-primary"))["summary"]}
    if name == "remove_participant":
        return {"summary": need(*call("POST", f"/api/crm/opportunities/{int(args['opportunity_id'])}/participants/{int(args['contact_id'])}/remove"))["summary"]}
    if name == "move_opportunity_stage":
        o = need(*call("GET", f"/api/crm/opportunities/{int(args['opportunity_id'])}"))
        want = str(args["stage"]).strip().lower()
        m = [s for s in o["stages"] if s["name"].lower() == want] or [s for s in o["stages"] if want in s["name"].lower()]
        if len(m) != 1:
            raise ValueError("Stage not found. The stages are: " + ", ".join(s["name"] for s in o["stages"]))
        return {"stage": need(*call("POST", f"/api/crm/opportunities/{o['opportunity_id']}/move", {"stage_id": m[0]["stage_id"]}))["stage"]}
    if name == "add_opportunity_task":
        o = need(*call("POST", f"/api/crm/opportunities/{int(args['opportunity_id'])}/tasks", {"title": args["title"], "date": args["date"], "time": args.get("time")}))
        return {"tasks": o["tasks"]}
    if name == "who_is_connected":
        return need(*call("GET", f"/api/crm/people/{int(args['contact_id'])}/connections"))["connections"]
    if name == "find_related_people":
        o = need(*call("GET", f"/api/crm/opportunities/{int(args['opportunity_id'])}"))
        on = {p["contact_id"] for p in o["participants"]}
        found = {}
        for p in o["participants"]:
            if not p["active"]:
                continue
            st, r = call("GET", f"/api/crm/people/{p['contact_id']}/connections")
            for c in (r or {}).get("connections", []) if st < 400 else []:
                if c.get("contact_id") in on:
                    continue
                key = c["email"]
                cur = found.get(key)
                if not cur or c["threads"] > cur["threads"]:
                    found[key] = {**c, "with": p["name"]}
        st, sg = call("GET", "/api/crm/suggestions")
        intro = {x["email"]: x["reason"] for x in (sg or {}).get("suggestions", []) if x.get("opportunity_id") == o["opportunity_id"]} if st < 400 else {}
        out = sorted(found.values(), key=lambda c: -c["threads"])
        for c in out:
            if c["email"] in intro:
                c["why"] = intro[c["email"]]
        return {"opportunity": o["name"], "already_on_it": [p["name"] for p in o["participants"]], "maybe": out[:20]}
    if name == "list_suggestions":
        items = need(*call("GET", "/api/crm/suggestions"))["suggestions"]
        if args.get("opportunity_id"):
            items = [x for x in items if x.get("opportunity_id") == int(args["opportunity_id"])]
        return items
    if name == "accept_suggestion":
        return need(*call("POST", f"/api/crm/suggestions/{int(args['suggestion_id'])}/accept", {}))
    if name == "dismiss_suggestion":
        return need(*call("POST", f"/api/crm/suggestions/{int(args['suggestion_id'])}/dismiss", {}))
    if name == "check_reply":
        out = need(*call("POST", "/api/claude/home/check_reply", {"query": args.get("contact", ""), "channel": args.get("channel", "")}))
        for k in ("their_last_message", "your_last_message"):          # email text stays in LyteKeepr: only when and where
            if isinstance(out.get(k), dict) and (out[k].get("channel") or "") == "email":
                out[k] = {kk: vv for kk, vv in out[k].items() if kk != "text"}
        return out
    if name == "search_messages":
        out = need(*call("POST", "/api/claude/home/search_crm_messages", {"person_name": args.get("contact") or None, "query": args.get("query") or None,
                                                                         "channel": args.get("channel") or None, "since_days": args.get("since_days") or 30, "limit": 50}))
        out["results"] = [m for m in out.get("results") or [] if (m.get("channel") or "").lower() != "email"]      # emails are not shared with Claude yet
        out["count"] = len(out["results"])
        return out
    if name == "draft_email":
        out = need(*call("POST", "/api/claude/home/draft_email_message", {"person_name": args["contact"], "subject": args["subject"], "body": args["body"]}))
        if out.get("status") == "pending_confirmation":
            out["next_step"] = "Nothing was sent. Open LyteKeepr (Home) within 30 minutes and press Send now."
        return out
    if name == "draft_linkedin_message":
        out = need(*call("POST", "/api/claude/home/draft_linkedin_message", {"person_name": args["contact"], "message_text": args["text"]}))
        if out.get("status") == "pending_confirmation":
            out["next_step"] = "Nothing was sent. Open LyteKeepr (Home) and press Send now."
        return out
    raise ValueError(f"Unknown tool: {name}")


# ------------------------------------------------------------------ routes
def register(app):
    from flask import Response, g, jsonify, redirect, request
    import auth_gate

    prefix = os.getenv("LK_URL_PREFIX", "").rstrip("/")
    for path in ("/mcp", "/oauth/register", "/oauth/token", "/oauth/authorize", "/.well-known/oauth-authorization-server",
                 "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        auth_gate.PUBLIC_PATHS.add(path)          # each checks its own token, code or sign-in
    recent: dict = {}                             # token -> request times (simple rate limit)

    def base() -> str:
        scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
        return f"{scheme}://{request.host}{prefix}"

    def oauth_error(code: str, text: str = "", status: int = 400):
        return jsonify({"error": code, "error_description": text}), status

    @app.get("/.well-known/oauth-authorization-server")
    def oauth_metadata():
        b = base()
        return jsonify({"issuer": b, "authorization_endpoint": b + "/oauth/authorize", "token_endpoint": b + "/oauth/token",
                        "registration_endpoint": b + "/oauth/register", "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"], "scopes_supported": [SCOPE]})

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def resource_metadata():
        b = base()
        return jsonify({"resource": b + "/mcp", "authorization_servers": [b], "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                        "resource_name": "LyteKeepr"})

    @app.post("/oauth/register")
    def oauth_register():
        body = request.get_json(silent=True) or {}
        uris = body.get("redirect_uris") or []
        if not uris or not all(isinstance(u, str) and (u in REDIRECT_OK or u.startswith(LOCAL_REDIRECT)) for u in uris):
            return oauth_error("invalid_redirect_uri", "Only Claude may connect to LyteKeepr.")
        with _db() as conn:
            hour_ago = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
            if conn.execute("SELECT COUNT(*) FROM oauth_clients WHERE created_at > ?", (hour_ago,)).fetchone()[0] >= 200:
                return oauth_error("temporarily_unavailable", "Too many registrations; try again later.", 429)
            client_id = "lkc_" + secrets.token_urlsafe(16)
            method = body.get("token_endpoint_auth_method") or "none"
            secret = secrets.token_urlsafe(32) if method in ("client_secret_post", "client_secret_basic") else ""
            conn.execute("INSERT INTO oauth_clients VALUES (?, ?, ?, ?, ?)", (client_id, str(body.get("client_name") or "Claude")[:100],
                                                                             json.dumps(uris), _hash(secret) if secret else "", _now()))
        out = {"client_id": client_id, "client_id_issued_at": int(time.time()), "redirect_uris": uris, "client_name": body.get("client_name") or "Claude",
               "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
               "token_endpoint_auth_method": method if secret else "none"}
        if secret:
            out["client_secret"], out["client_secret_expires_at"] = secret, 0
        return jsonify(out), 201

    def _client(conn, client_id):
        row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return dict(row) if row else None

    signer_salt = "lytekeepr-claude-consent"

    def _signer():
        from itsdangerous import URLSafeTimedSerializer
        return URLSafeTimedSerializer(os.getenv("LYTEKEEPR_SECRET_KEY") or os.getenv("LYTEKEEPR_PASSWORD") or "local-dev-only", salt=signer_salt)

    @app.route("/oauth/authorize", methods=["GET", "POST"])
    def oauth_authorize():
        """The person signs in to LyteKeepr (if needed) and allows Claude. Nothing is granted without the Allow button."""
        if request.method == "POST":
            try:
                req = _signer().loads(request.form.get("grant", ""), max_age=600)
            except Exception:
                return "This page expired. Start connecting again from Claude.", 400
            user = auth_gate._session["current_user"]() if "current_user" in auth_gate._session else None
            if not user or int(user.get("id") or 0) != req["u"]:
                return "You are no longer signed in. Start connecting again from Claude.", 400
            if request.form.get("choice") != "allow":
                return redirect(req["redirect_uri"] + ("&" if "?" in req["redirect_uri"] else "?") + urllib.parse.urlencode({"error": "access_denied", "state": req["state"]}))
            code = secrets.token_urlsafe(32)
            with _db() as conn:
                conn.execute("INSERT INTO oauth_codes VALUES (?, ?, ?, ?, ?, ?, ?)", (_hash(code), req["client_id"], req["redirect_uri"], req["challenge"],
                                                                                    req["u"], req["w"], time.time() + CODE_SECONDS))
            from . import directory
            account = directory.account_for_profile(req["w"])
            directory.log_activity(user.get("email") or "admin", "Connected Claude", account["id"] if account else None)
            params = {"code": code, "state": req["state"]} if req["state"] else {"code": code}
            return redirect(req["redirect_uri"] + ("&" if "?" in req["redirect_uri"] else "?") + urllib.parse.urlencode(params))

        a = request.args
        with _db() as conn:
            client = _client(conn, a.get("client_id", ""))
        if not client:
            return "Unknown app. Add LyteKeepr again in Claude.", 400
        redirect_uri = a.get("redirect_uri") or json.loads(client["redirect_uris"])[0]
        if redirect_uri not in json.loads(client["redirect_uris"]):
            return "This return address was not registered.", 400
        if a.get("response_type") != "code" or not a.get("code_challenge") or (a.get("code_challenge_method") or "S256") != "S256":
            return redirect(redirect_uri + "?" + urllib.parse.urlencode({"error": "invalid_request", "state": a.get("state", "")}))
        user = auth_gate._session["current_user"]() if "current_user" in auth_gate._session else {"id": 0, "email": "", "name": "Admin", "role": "admin"}
        if not user:                              # sign in first, then come straight back here
            response = redirect(prefix + "/login")
            response.set_cookie("lk_next", prefix + request.full_path, max_age=900, httponly=True, samesite="Lax", path="/",
                                secure=request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure)
            return response
        if getattr(g, "account_status", "active") == "waiting":
            return "Your LyteKeepr account is waiting for approval.", 403
        from profiles.store import get_active_profile_id
        grant = _signer().dumps({"client_id": client["client_id"], "redirect_uri": redirect_uri, "challenge": a.get("code_challenge"),
                                 "state": a.get("state", ""), "u": int(user.get("id") or 0), "w": get_active_profile_id()})
        who = user.get("name") or user.get("email") or "you"
        page = CONSENT_PAGE.replace("{{WHO}}", _esc(who)).replace("{{EMAIL}}", _esc(user.get("email") or "")).replace("{{GRANT}}", _esc(grant)) \
                           .replace("{{ACTION}}", _esc(prefix + "/oauth/authorize"))
        return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @app.post("/oauth/token")
    def oauth_token():
        f = request.form
        client_id, secret = f.get("client_id", ""), f.get("client_secret", "")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                client_id, secret = [urllib.parse.unquote(x) for x in base64.b64decode(auth[6:]).decode().split(":", 1)]
            except Exception:
                return oauth_error("invalid_client", status=401)
        with _db() as conn:
            client = _client(conn, client_id)
            if not client or (client["secret_hash"] and _hash(secret) != client["secret_hash"]):
                return oauth_error("invalid_client", status=401)
            if f.get("grant_type") == "authorization_code":
                row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (_hash(f.get("code", "")),)).fetchone()
                conn.execute("DELETE FROM oauth_codes WHERE code_hash = ? OR expires < ?", (_hash(f.get("code", "")), time.time()))
                if not row or row["expires"] < time.time() or row["client_id"] != client_id or row["redirect_uri"] != f.get("redirect_uri", row["redirect_uri"]):
                    return oauth_error("invalid_grant", "The code is wrong or expired.")
                verifier = f.get("code_verifier", "")
                expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not verifier or expected != row["challenge"]:
                    return oauth_error("invalid_grant", "PKCE check failed.")
                if not _may_sign_in(row["user_id"], row["profile_id"]):
                    return oauth_error("invalid_grant", "This account can't sign in.")
                return jsonify(_issue(conn, client_id, row["user_id"], row["profile_id"]))
            if f.get("grant_type") == "refresh_token":
                row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'", (_hash(f.get("refresh_token", "")),)).fetchone()
                if not row or row["expires"] < time.time() or row["client_id"] != client_id:
                    return oauth_error("invalid_grant", "Sign in again.")
                conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (row["token_hash"],))      # one use only: a new one comes back
                if not _may_sign_in(row["user_id"], row["profile_id"]):
                    return oauth_error("invalid_grant", "This account can't sign in.")
                return jsonify(_issue(conn, client_id, row["user_id"], row["profile_id"]))
        return oauth_error("unsupported_grant_type")

    def unauthorized():
        return Response(json.dumps({"error": "Sign in to LyteKeepr from Claude."}), status=401, mimetype="application/json",
                        headers={"WWW-Authenticate": f'Bearer realm="LyteKeepr", resource_metadata="{base()}/.well-known/oauth-protected-resource"'})

    @app.route("/mcp", methods=["GET", "POST", "DELETE"])
    def mcp_endpoint():
        auth = request.headers.get("Authorization", "")
        owner = token_owner(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if not owner or not _may_sign_in(owner["user_id"], owner["profile_id"]):
            return unauthorized()
        if request.method != "POST":                # no server-sent stream: every answer comes back on its own POST
            return Response(status=405, headers={"Allow": "POST"})
        key = auth[7:20]
        now = time.time()
        recent[key] = [t for t in recent.get(key, []) if now - t < 60] + [now]
        if len(recent[key]) > CALLS_PER_MINUTE:
            return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Too many requests; wait a minute."}}), 429
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), 400
        batch = isinstance(payload, list)
        answers = [a for a in (_handle(m, owner) for m in (payload if batch else [payload])) if a is not None]
        if not answers:
            return Response(status=202)
        return jsonify(answers if batch else answers[0])

    def _handle(msg: dict, owner: dict):
        if not isinstance(msg, dict) or "method" not in msg:
            return None                              # a response or junk: nothing to answer
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if mid is None:                              # notifications (e.g. notifications/initialized) get no answer
            return None
        def ok(result):
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return ok({"protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "LyteKeepr", "version": "1.0"},
                       "instructions": ("LyteKeepr is the user's CRM: contacts, sales funnels, tasks, reminders and recorded calls. "
                                        "Dates are YYYY-MM-DD and times HH:MM in the user's local time. Find a contact's id with search_contacts "
                                        "before get_contact; when the user names someone or something, use find first. Funnels hold two kinds of entries: contacts (one person) and opportunities (one shared "
                                        "deal or project with several participants). Never turn one into the other, and never assume everyone at a "
                                        "company is part of an opportunity. Ask the user before creating or changing anything.")})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": [{"name": t["name"], "title": t["title"], "description": t["description"], "inputSchema": t["inputSchema"],
                                  "annotations": {"title": t["title"], "readOnlyHint": t["read"], "destructiveHint": bool(t.get("destructive")), "openWorldHint": False}}
                                 for t in TOOLS]})
        if method == "tools/call":
            name, args = params.get("name", ""), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            try:
                result = _run_tool(_caller(owner), name, args)
                text = json.dumps(result, ensure_ascii=False, default=str)
                if not next(t for t in TOOLS if t["name"] == name)["read"]:
                    _log_change(owner, name, args)
                return ok({"content": [{"type": "text", "text": text[:200000]}], "isError": False})
            except (ValueError, KeyError, TypeError) as exc:
                return ok({"content": [{"type": "text", "text": f"Could not do that: {exc}"}], "isError": True})
        if method in ("resources/list", "prompts/list"):
            return ok({method.split("/")[0]: []})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def _caller(owner: dict):
        """Calls the app's own API inside the server, signed in as this person (their own login cookie, made here)."""
        sign = auth_gate._session.get("sign")
        cookie = f"{auth_gate.COOKIE}={sign(owner['user_id'], owner['profile_id'])}" if sign else ""
        client = app.test_client(use_cookies=False)      # the Cookie header below is the only sign-in
        def call(method: str, path: str, body: dict | None = None):
            res = client.open(path, method=method, json=body, headers={"Cookie": cookie} if cookie else {})
            try:
                return res.status_code, res.get_json(silent=True)
            finally:
                res.close()
        return call

    def _log_change(owner: dict, name: str, args: dict):
        try:
            from . import directory
            account = directory.account_for_profile(owner["profile_id"])
            directory.log_activity(f"Claude (user {owner['user_id']})", f"{name}: {json.dumps(args, ensure_ascii=False)[:200]}", account["id"] if account else None)
        except Exception:
            pass

    HOME_TOOLS = ("check_reply", "search_crm_messages", "draft_email_message", "draft_linkedin_message")

    @app.post("/api/claude/home/<name>")
    def claude_home_tool(name):
        """The Home chat's own tools, for the connector (signed in as the person; not open to team members, like the Home chat)."""
        if name not in HOME_TOOLS:
            return jsonify({"error": "unknown"}), 404
        from assistant.tools import run_tool
        return jsonify(json.loads(run_tool(name, request.get_json(silent=True) or {})))

    # Settings: the address to paste into Claude, and who is connected (with Disconnect)
    @app.get("/api/me/claude")
    def my_claude():
        from profiles.store import get_active_profile_id
        user = getattr(g, "user", None) or {"id": 0}
        return jsonify({"connector_url": base() + "/mcp", "connections": connections(int(user.get("id") or 0), get_active_profile_id())})

    @app.delete("/api/me/claude/<client_id>")
    def my_claude_disconnect(client_id):
        from profiles.store import get_active_profile_id
        user = getattr(g, "user", None) or {"id": 0}
        disconnect(int(user.get("id") or 0), get_active_profile_id(), client_id)
        return jsonify({"ok": True})


def _esc(text: str) -> str:
    import html
    return html.escape(str(text), quote=True)


CONSENT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Claude to LyteKeepr</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#b5c9d1;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#29434E;padding:16px;box-sizing:border-box}
.card{background:#f2f8fa;border-radius:18px;box-shadow:0 4px 16px rgba(41,67,78,.10);padding:28px;max-width:420px;width:100%}
h1{font-size:20px;margin:0 0 6px}p{font-size:14px;line-height:1.5;color:#657D86;margin:0 0 14px}
ul{font-size:14px;line-height:1.6;margin:0 0 16px;padding-left:18px}b{color:#29434E}
.row{display:flex;gap:10px;margin-top:8px}button{flex:1;border:none;border-radius:999px;padding:12px 16px;font-size:14px;font-weight:600;cursor:pointer}
.allow{background:#29434E;color:#f2f8fa}.no{background:rgba(230,237,240,.9);color:#29434E}
</style></head><body><div class="card">
<h1>Allow Claude to use your LyteKeepr?</h1>
<p>Signed in as <b>{{WHO}}</b> {{EMAIL}}</p>
<ul><li>See your contacts, funnels, tasks, reminders, recorded calls and LinkedIn/Instagram messages</li><li>Add and update contacts, stages, outreach statuses, tasks and reminders</li><li>Book or cancel meetings in your Google Calendar (invites go out from Google)</li><li>Create opportunities and manage who is on them</li><li>Write email and LinkedIn drafts that you send yourself from LyteKeepr</li></ul>
<p>Claude sees only what you can see in LyteKeepr, and asks you before it changes anything. Disconnect any time in LyteKeepr Settings → Claude.</p>
<form method="post" action="{{ACTION}}"><input type="hidden" name="grant" value="{{GRANT}}">
<div class="row"><button class="no" name="choice" value="deny" type="submit">Cancel</button><button class="allow" name="choice" value="allow" type="submit">Allow</button></div></form>
</div></body></html>"""
