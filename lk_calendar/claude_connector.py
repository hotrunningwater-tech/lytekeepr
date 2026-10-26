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
import re
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
def _close_matches(call, need, text: str, kinds=("contact", "opportunity", "funnel")) -> list:
    """Speech-to-text gets names wrong ("Mario Loretti", "NXY pilot"): the closest names by spelling, best first (at most 3)."""
    from difflib import SequenceMatcher
    t = " ".join(str(text or "").lower().split())
    if not t:
        return []
    def score(name):
        n = " ".join(str(name or "").lower().split())
        if not n:
            return 0.0
        whole = SequenceMatcher(None, t, n).ratio()
        words = [max((SequenceMatcher(None, w, x).ratio() for x in n.split()), default=0) for w in t.split()]
        return max(whole, sum(words) / len(words) if words else 0)
    pool = []
    if "contact" in kinds:
        pool += [{"kind": "contact", "contact_id": c["id"], "name": c.get("display"), "company": c.get("company") or None}
                 for c in need(*call("GET", "/api/calendar/contacts?limit=500"))["contacts"]]
    if "opportunity" in kinds:
        pool += [{"kind": "opportunity", "opportunity_id": o["opportunity_id"], "name": o["name"]}
                 for o in need(*call("GET", "/api/crm/opportunities?limit=500"))["opportunities"]]
    if "funnel" in kinds:
        pool += [{"kind": "funnel", "funnel_id": f["funnel_id"], "name": f["name"]} for f in need(*call("GET", "/api/crm/funnels"))["funnels"]]
    ranked = sorted(((score(x["name"]), x) for x in pool), key=lambda p: -p[0])
    return [x for sc, x in ranked if sc >= 0.6][:3]


def _did_you_mean(matches: list) -> dict:
    if not matches:
        return {"did_you_mean": [], "say": "Nothing similar either. Say so in one short line."}
    return {"did_you_mean": matches,
            "say": "No exact match. Ask in ONE short line: 'Did you mean " + " or ".join(m["name"] for m in matches) + "?' Nothing else."}


def _brand_key(call, need, text):
    """A brand name or key → its key; None when no brand was asked for; '' for 'none'. Unknown brands are an error listing the real ones."""
    t = str(text or "").strip()
    if not t:
        return None
    if t.lower() in ("none", "no brand", "unassigned"):
        return ""
    from .brands import best_match                      # the same forgiving match as the app ("Nexi" -> Nexy)
    brands = need(*call("GET", "/api/crm/brands"))["brands"]
    b = best_match(t, brands)
    if b:
        return b["key"]
    raise ValueError(f"There is no brand '{t}'. Brands: {', '.join(b['name'] for b in brands)}.")


def _strip_google(items: list) -> list:
    """Calendar items for Claude, Google meetings included: what a meeting is (title, time, guests, join link, status, the viewer's private notes),
    with the raw Google description kept short."""
    out = []
    for e in items or []:
        e = dict(e)
        if e.get("source") == "google":
            e["notes"] = (e.get("notes") or "")[:600]
            try:
                e["guests"] = [a.get("email") for a in (json.loads(e.get("attendees") or "[]") if isinstance(e.get("attendees"), str) else e.get("attendees") or [])]
            except ValueError:
                e["guests"] = []
            e.pop("attendees", None)
        out.append(e)
    return out


# Tools that save text the user dictated (speech-to-text: typos, repeats, corrections on the fly). The text must be cleaned up, shown and approved first.
CONFIRM_TEXT_TOOLS = {"update_company": "add_note", "doc_create": "content", "doc_write": "text", "tab_create": "content", "add_contact_note": "text", "log_conversation": "text",
                      "write_meeting_notes": "text", "edit_contact_record": "text", "set_meeting_agenda": "text", "update_contact": "add_note", "reply_email": "text"}
CONFIRM_RULE = ("BEFORE SAVING: the user dictates by voice, with typos, repeated words and corrections on the fly. First write the cleaned-up text "
                "(typos fixed, repeats removed, their corrections applied, their meaning and wording kept, nothing invented), show it in the chat, "
                "and ask if it is good to save. Call this only after they say yes, with exactly that text and user_approved_text=true.")


TOOLS = [
    {"name": "find", "title": "Find a person, company or opportunity",
     "description": ("START HERE when the user names someone or something. Searches contacts AND opportunities at once and says what each match is: "
                     "a contact (with their own funnel and stage, if any) and every opportunity they are on (with their role), plus opportunities "
                     "whose name or company matches, and funnels whose name matches. A person can be a single contact entry in a funnel, a participant in an opportunity, or both. "
                     "Names often arrive misspelled by speech-to-text: when nothing matches exactly, the result has did_you_mean with the closest names; "
                     "then just ask 'Did you mean …?' in one line."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, "read": True},
    {"name": "list_brands", "title": "List brands",
     "description": ("The brands (businesses) this account runs, e.g. LyteKeepr, Aspan, Nexy. Contacts, funnels and calendar items each belong to one brand; "
                     "a contact in a funnel has the funnel's brand. Contacts with no brand yet show brand ''."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "create_brand", "title": "Add a brand", "description": "Add a new brand (business). Only when the user asks for it.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, "read": False},
    {"name": "set_funnel_brand", "title": "Set a funnel's brand",
     "description": "Make a funnel belong to a brand. Everyone in the funnel takes that brand, and their follow-ups are filed under it. Empty brand clears the funnel's brand.",
     "inputSchema": {"type": "object", "properties": {"funnel_id": {"type": "integer"}, "brand": {"type": "string", "description": "The brand (name or key, see list_brands). When it is unclear which brand the user means, ask; never guess."}},
                     "required": ["funnel_id", "brand"]}, "read": False},
    {"name": "search_contacts", "title": "Search contacts",
     "description": "Find contacts in LyteKeepr by name, company, email or notes. Returns id, name, company, email, channel, brand, funnel and stage.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "Words to look for; empty lists the newest contacts."},
                                                      "brand": {"type": "string", "description": "Only contacts of this brand (name or key); 'none' = contacts with no brand yet."},
                                                      "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}}, "read": True},
    {"name": "get_contact", "title": "Contact details",
     "description": "Everything about one contact: profile, notes, funnel and stage, LinkedIn/Instagram messages, follow-ups and their recorded calls (with summaries). Use search_contacts first to get the id.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}}, "required": ["contact_id"]}, "read": True},
    {"name": "contacts_gone_quiet", "title": "Who is waiting (gone quiet)",
     "description": "A full sweep of every contact whose conversation stopped, split by who owes the next message: waiting_on_you = they wrote last and the user "
                    "hasn't answered for you_days days (3 by default); waiting_on_them = the user wrote last and they haven't answered for days days (14 by default). "
                    "Use for 'who hasn't replied to me?' (waiting_on_them), 'who am I leaving hanging?' (waiting_on_you), or both. The brand must come from the USER: "
                    "pass their own words that named it in brand_from_user. Never choose a brand or 'all' yourself; if they didn't name one, call without it and ask "
                    "the question the tool returns. Always label each person with their side, and say when a follow-up is planned or overdue (overdue = on the user). "
                    "Leaves out contacts marked not interested (Lost) and ones put off with 'Not now'.",
     "inputSchema": {"type": "object", "properties": {"brand": {"type": "string", "description": "Brand name or key, or 'all' for every brand"},
                                                      "brand_from_user": {"type": "string", "description": "The user's own words that named the brand or said all (quote them)"},
                                                      "side": {"type": "string", "enum": ["you", "them", "both"], "default": "both"},
                                                      "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 14, "description": "Waiting on them: at least this many days"},
                                                      "you_days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 3, "description": "Waiting on you: at least this many days"},
                                                      "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}}, "read": True},
    {"name": "snooze_quiet_contact", "title": "Not now (gone quiet)",
     "description": "Take a contact off the gone-quiet list for some days (they come back after that if nobody got in touch).",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 14}},
                     "required": ["contact_id"]}},
    {"name": "list_funnels", "title": "List funnels", "description": "The sales funnels with their stages and how many people are in each.",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "get_funnel", "title": "Funnel board", "description": "One funnel: every stage with its contact entries (people, with their status) and its opportunity entries (shared deals with several people).",
     "inputSchema": {"type": "object", "properties": {"funnel_id": {"type": "integer"}}, "required": ["funnel_id"]}, "read": True},
    {"name": "get_agenda", "title": "Agenda", "description": "Meetings (Google Calendar ones too, with guests), tasks and reminders that are overdue, due today and coming up in the next 7 days. item_id = id.",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "list_calendar", "title": "Calendar items", "description": "Meetings (including Google Calendar ones, with guests), tasks and reminders between two dates (inclusive). Use it to find a meeting by person or date; item_id = id.",
     "inputSchema": {"type": "object", "properties": {"start_date": {"type": "string", "description": "YYYY-MM-DD"}, "end_date": {"type": "string", "description": "YYYY-MM-DD"}},
                     "required": ["start_date", "end_date"]}, "read": True},
    {"name": "autopilot_queue", "title": "Autopilot: work to do",
     "description": "The scheduled autopilot run's work list (every 2 hours). `instructions` says what to do with each section: follow it for every item, "
                    "save with the tools it names, then stop. Do not ask the user anything; nothing here is ever sent.",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "save_message_review", "title": "Save message review",
     "description": "After reading the new_messages from autopilot_queue: the keys read, the few clear flags, and times leads gave for the next contact. Call once per run. Nothing is sent.",
     "inputSchema": {"type": "object", "properties": {
         "reviewed": {"type": "array", "items": {"type": "string"}, "description": "Every item key read on this run"},
         "flags": {"type": "array", "items": {"type": "object", "properties": {
             "key": {"type": "string"}, "kind": {"type": "string", "enum": ["interested", "waiting", "commitment"]},
             "side": {"type": "string", "enum": ["ours", "theirs"], "description": "commitments only"},
             "summary": {"type": "string"}, "quote": {"type": "string"}, "due": {"type": "string", "description": "YYYY-MM-DD, commitments only"}},
             "required": ["key", "kind", "summary"]}},
         "follow_ups": {"type": "array", "items": {"type": "object", "properties": {
             "contact_id": {"type": "integer"}, "date": {"type": "string"}, "quote": {"type": "string"}}, "required": ["contact_id", "date"]}}},
         "required": ["reviewed"]}},
    {"name": "save_onboarding", "title": "Save onboarding",
     "description": "For a newly won contact (autopilot_queue newly_won): a welcome email draft (NOT sent; it waits on Home) and onboarding reminders for the user. "
                    "Body without a subject line, signed 'Best,' and [Your Name]. Leave subject/body empty when the contact has no email.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "subject": {"type": "string"}, "body": {"type": "string"},
                                                      "reminders": {"type": "array", "items": {"type": "object", "properties": {
                                                          "title": {"type": "string"}, "days_from_now": {"type": "integer", "minimum": 0, "maximum": 180}},
                                                          "required": ["title"]}}}, "required": ["contact_id"]}},
    {"name": "set_stage_won", "title": "Mark a funnel stage as Won",
     "description": "Mark (or unmark) a funnel stage as the won-deal stage; reaching it starts onboarding on the next autopilot run. People already in it are treated as existing customers. Admin only. stage_id from get_funnel.",
     "inputSchema": {"type": "object", "properties": {"stage_id": {"type": "integer"}, "won": {"type": "boolean", "default": True}}, "required": ["stage_id"]}},
    {"name": "save_meeting_prep", "title": "Save meeting prep",
     "description": "When the user asks to prepare for a meeting (and in the autopilot run): ONLY for a meeting in the calendar (item_id from get_agenda or list_calendar); "
                    "if there is no such meeting, say so and prepare nothing. FIRST read get_meeting_notes: if it already has a prep, tell the user it is already prepped "
                    "(and when), show it, and prepare again only if they insist (then replace=true). Then write the prep (who they are, where things stand, the last "
                    "conversations, open questions, what to aim for), show it in the chat and save it here: it goes into the meeting's private notes as Claude's prep, "
                    "never touching the user's own notes, never sent to Google or guests.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "text": {"type": "string"},
                                                      "replace": {"type": "boolean", "description": "true only after the user said to redo an existing prep"}},
                     "required": ["item_id", "text"]}},
    {"name": "save_meeting_brief", "title": "Save meeting prep (old name)",
     "description": "Same as save_meeting_prep (kept for scheduled runs set up earlier). Never replaces an existing prep.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["item_id", "text"]}},
    {"name": "save_call_summary", "title": "Save call summary",
     "description": "Save a call's summary on the call (shows on the contact and on Home under Calls to review). Whenever you summarize a recorded call for the user, "
                    "save it here too. If the answer says it already has a summary, warn the user and save again with replace=true only after they say yes.",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}, "summary": {"type": "string"},
                                                      "replace": {"type": "boolean", "description": "true only after the user agreed to replace the existing summary"},
                                                      "no_contact": {"type": "boolean", "description": "true only if the user said the call was not with any contact (e.g. internal)"}},
                     "required": ["call_id", "summary"]}},
    {"name": "link_call_to_contact", "title": "Link a call to a contact",
     "description": "Put a contact on a recorded call that nobody was matched to (an impromptu Zoom, a call that moved to Telegram). Only after the user confirmed who it was. "
                    "The call then shows in that contact's history.",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}, "contact_id": {"type": "integer"}}, "required": ["call_id", "contact_id"]}},
    {"name": "save_followup_draft", "title": "Save follow-up draft",
     "description": "Save a follow-up email draft after a meeting (autopilot): call_id for a recorded call, or item_id for a meeting with no recording. "
                    "Goes to the first contact on it with an email. NOT sent: the user reviews it on Home and sends it themselves. Body without a subject line; sign off with 'Best,' and [Your Name].",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}, "item_id": {"type": "integer"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["subject", "body"]}},
    {"name": "calls_to_review", "title": "Calls to review", "description": "Recorded calls with a transcript not summarized yet, and meetings that passed with no recording (possible no-shows).",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "get_call", "title": "Call transcript", "description": "One recorded call (from Fathom): who was on it (contacts), the summary if any, and the full transcript. Before summarizing: if contacts is empty, "
                    "ask the user who the call was with (suggest from what they said or the names in the transcript), find them with search_contacts and link the call "
                    "with link_call_to_contact after they confirm. Then summarize and save the summary with save_call_summary.",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}}, "required": ["call_id"]}, "read": True},
    {"name": "create_task", "title": "Create a task",
     "description": "Add a task (shows on the user's Home, under Actions needed when due today, otherwise under Tasks; the calendar shows only meetings). Use for follow-ups too (e.g. 'Follow up with Sara about pricing'). Today or later only.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM, 24-hour; leave out for no specific time"},
                                                      "notes": {"type": "string"}, "brand": {"type": "string", "description": "Which brand the task is for; default LyteKeepr. " + "The brand (name or key, see list_brands). When it is unclear which brand the user means, ask; never guess."}},
                     "required": ["title", "date"]}, "read": False},
    {"name": "create_reminder", "title": "Create a reminder", "description": "A reminder that shows on the LyteKeepr Home page under 'Action needed' from its due date.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"}},
                     "required": ["text", "due_date"]}, "read": False},
    {"name": "mark_task_done", "title": "Mark a task done", "description": "Mark a LyteKeepr task done (or not done) on a given date. Get the id from get_agenda or list_calendar.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "date": {"type": "string", "description": "YYYY-MM-DD of that occurrence"},
                                                      "done": {"type": "boolean", "default": True}}, "required": ["task_id", "date"]}, "read": False},
    # --- added for everyday phone use: the same actions the three LyteKeepr AI chats have ---
    {"name": "create_contact", "title": "Add a contact",
     "description": "Add a new contact (someone you just met or talked to). An Instagram handle goes in 'instagram' (never in the name); a LinkedIn link in 'linkedin'. "
                    "From a business card photo or a voice note: read name, job title, company, email, phone and links, call search_contacts first (no duplicates; "
                    "update_contact if they exist), show the user what you read, and save after they confirm. Anything else said goes in notes.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "The person's name; leave empty if only a handle is known"},
                                                      "company": {"type": "string"}, "job_title": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
                                                      "instagram": {"type": "string", "description": "Instagram handle like @name (or the instagram.com link)"},
                                                      "linkedin": {"type": "string", "description": "LinkedIn profile link"}, "notes": {"type": "string"},
                                                      "brand": {"type": "string", "description": "REQUIRED: the brand (name or key, see list_brands). Ask the user which brand unless they said it; never guess. With no brands yet, tell them to create one first (Settings → Brands, or create_brand)."}},
                     "required": ["brand"]}, "read": False},
    {"name": "update_contact", "title": "Update a contact",
     "description": "Change a contact's details, or add a note (add_note is saved as a dated note, like add_contact_note). Only the fields given change.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "name": {"type": "string"}, "company": {"type": "string"},
                                                      "job_title": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
                                                      "instagram": {"type": "string", "description": "Instagram handle like @name"}, "linkedin": {"type": "string", "description": "LinkedIn profile link"},
                                                      "add_note": {"type": "string", "description": "A note to add. Clean it up, show it and get a yes first, then set user_approved_text=true."},
                                                      "user_approved_text": {"type": "boolean"},
                                                      "brand": {"type": "string", "description": "Give the contact this brand ('' removes it). A contact in a branded funnel keeps the funnel's brand: "
                                                                                                 "change the funnel's brand or move them instead. " + "The brand (name or key, see list_brands). When it is unclear which brand the user means, ask; never guess."}},
                     "required": ["contact_id"]}, "read": False},
    {"name": "move_contact_stage", "title": "Move to a funnel stage",
     "description": "Move a contact to a stage of their funnel by the stage's name (e.g. 'In conversation'). Only the stage changes: outreach statuses and follow-ups stay as they are (use set_outreach_status for those). Give funnel_id to put them into a funnel they are not in yet.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "stage": {"type": "string"}, "funnel_id": {"type": "integer"}},
                     "required": ["contact_id", "stage"]}, "read": False},
    {"name": "set_outreach_status", "title": "Set outreach status",
     "description": "Set where outreach to a contact stands on a channel (linkedin, instagram or email); it can move their funnel card forward (never back). Use one of the options get_contact lists for that channel under outreach (e.g. 'email sent', 'replied', 'call booked'). "
                    "When the status is a send (a DM, message, email or connection request sent), ASK the user first: 'Follow up in 3 days, in another number of days, or no follow-up?' "
                    "and pass their answer as follow_up_days (3, their number, or 0). Never decide it yourself. If the answer says asks_follow_up, ask and call again with the same status and follow_up_days.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "channel": {"type": "string", "enum": ["linkedin", "instagram", "email"]},
                                                      "status": {"type": "string"},
                                                      "follow_up_days": {"type": "integer", "minimum": 0, "maximum": 365, "description": "After a send: days until the follow-up, 0 = none"}},
                     "required": ["contact_id", "channel", "status"]}, "read": False},
    {"name": "clear_follow_ups", "title": "Clear follow-ups", "destructive": True,
     "description": "Mark open outreach follow-ups done in bulk, to clean up a pile: filter by channel, brand, how overdue (older_than_days) or contact. "
                    "ALWAYS call with preview=true first, tell the user how many and which, and clear (preview=false) only after they say yes. 'Reply to' tasks are left alone unless include_replies.",
     "inputSchema": {"type": "object", "properties": {"channel": {"type": "string", "enum": ["linkedin", "instagram", "email"]}, "brand": {"type": "string"},
                                                      "older_than_days": {"type": "integer", "minimum": 0}, "contact_id": {"type": "integer"},
                                                      "include_replies": {"type": "boolean"}, "preview": {"type": "boolean", "default": True}}}},
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
                     "Adds a Google Meet link and marks it to be recorded by Fathom. Leave record_with_fathom out: it is always on unless the user explicitly says "
                     "not to record this meeting. Today or later only. "
                     "This SENDS invitations: always confirm title, date, time and who is invited with the user first. "
                     "Invite people by contact name (as in LyteKeepr) or email address."),
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM, 24-hour, the user's local time"},
                                                      "duration_min": {"type": "integer", "default": 30}, "invite": {"type": "array", "items": {"type": "string"}, "description": "Contact names or email addresses"},
                                                      "google_meet": {"type": "boolean", "default": True}, "record_with_fathom": {"type": "boolean", "default": True, "description": "Leave out (on). false only when the user says not to record."},
                                                      "notes": {"type": "string"}}, "required": ["title", "date", "time"]}, "read": False},
    {"name": "get_meeting_notes", "title": "A meeting's private notes",
     "description": "The user's own private notes on a meeting (notes before, during or after the call), with Claude's prep separately in `prep` and when it was written in `prepared_on` (empty = not prepped yet). item_id from get_agenda or list_calendar. Only the user sees these.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}, "read": True},
    {"name": "write_meeting_notes", "title": "Write private meeting notes",
     "description": (CONFIRM_RULE + " " + "Add to the user's private notes on a meeting (prep plan, what was said, follow-ups). mode 'append' (the default) adds at the end and keeps "
                     "everything already written; use 'replace' only when the user asks to rewrite the whole note. Never sent to Google or the guests."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "item_id": {"type": "integer"}, "text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]}},
                     "required": ["item_id", "text"]}, "read": False},
    {"name": "set_meeting_agenda", "title": "Write the meeting's description (guests see it)", "destructive": True,
     "description": (CONFIRM_RULE + " " + "Write into the Google Calendar description of a meeting the user organized, e.g. an agenda. THE GUESTS SEE IT. First show the user the exact text "
                     "and get a clear yes. mode 'append' (default) adds below the current description; 'replace' rewrites it. notify=true also emails the guests "
                     "about the change (default false). Private prep goes in write_meeting_notes instead, never here."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "item_id": {"type": "integer"}, "text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]},
                                                      "notify": {"type": "boolean"}}, "required": ["item_id", "text"]}, "read": False},
    {"name": "set_meeting_recording", "title": "Record a meeting with Fathom (on/off)",
     "description": "Switch Fathom recording on or off for a meeting already in the calendar (item_id from get_agenda or list_calendar). Only when the user asks.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "record": {"type": "boolean"}}, "required": ["item_id", "record"]}, "read": False},
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
    # --- documents: folders, documents and tabs kept in LyteKeepr (like Google Drive/Docs, but in LyteKeepr) ---
    {"name": "docs_places", "title": "Where documents are",
     "description": ("Every brand and funnel that has documents, with how many and when last updated. Documents are organised Brand → Funnel → Folders → Documents → Tabs. "
                     "To find something: work out the brand or funnel first (list_brands, list_funnels), then docs_list there, then docs_search, and only then read the documents."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "docs_list", "title": "List folders and documents",
     "description": "Folders (nested), documents and their tab names in one brand (its own documents, not its funnels') or one funnel. Give brand or funnel_id.",
     "inputSchema": {"type": "object", "properties": {"brand": {"type": "string", "description": "Brand name or key (see list_brands)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"}}}, "read": True},
    {"name": "docs_search", "title": "Search documents",
     "description": ("Find folders, documents and tabs by name, and passages in their text. Narrow it with brand or funnel_id when known. "
                     "Name matches come first; text matches show a snippet. Then use doc_read."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"},
                                                      "names_only": {"type": "boolean", "description": "true = don't search the text"}}, "required": ["query"]}, "read": True},
    {"name": "doc_read", "title": "Read a document",
     "description": "A document's text and all its tabs; or just one tab (tab = name or id); or one section (a # heading, by its words or number) of the text or of a tab.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "tab": {"type": "string"}, "section": {"type": "string"}}, "required": ["doc_id"]}, "read": True},
    {"name": "doc_create", "title": "Create a document",
     "description": (CONFIRM_RULE + " " + "Create a document at the top of a brand or funnel, or in a folder (folder_id), optionally with text and named tabs. "
                     "A funnel must have a brand first. If it's unclear where it belongs, ask."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "title": {"type": "string"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"}, "folder_id": {"type": "integer"}, "content": {"type": "string"},
                                                      "tabs": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}}, "required": ["name"]}}},
                     "required": ["title"]}, "read": False},
    {"name": "doc_write", "title": "Write in a document",
     "description": (CONFIRM_RULE + " " + "Change the text of a document or of one of its tabs (tab = name or id), touching nothing else. mode: "
                     "'append' adds at the end (the default, e.g. saving what was just discussed); 'replace_section' replaces one # heading's part (section = its words or number); "
                     "'find_replace' replaces one exact passage (find must appear exactly once); 'replace' replaces the whole text. "
                     "Before 'replace', 'replace_section' or 'find_replace', the old text is kept as a previous version. Read first, then change only what was asked."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "doc_id": {"type": "integer"}, "text": {"type": "string"}, "tab": {"type": "string"},
                                                      "mode": {"type": "string", "enum": ["append", "replace_section", "find_replace", "replace"]},
                                                      "section": {"type": "string"}, "find": {"type": "string"}}, "required": ["doc_id", "text"]}, "read": False},
    {"name": "doc_update", "title": "Rename or move a document",
     "description": "Rename a document, or move it into a folder (folder_id), or to the top of a brand or funnel (brand/funnel_id, or to_top=true for the top of where it is). Its text and tabs stay.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "title": {"type": "string"}, "folder_id": {"type": "integer"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"},
                                                      "to_top": {"type": "boolean"}}, "required": ["doc_id"]}, "read": False},
    {"name": "doc_delete", "title": "Delete a document", "destructive": True,
     "description": "Delete a document with its tabs. Only when the user asks; if it has a lot of text, confirm with the user first.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}}, "required": ["doc_id"]}, "read": False},
    {"name": "tab_create", "title": "Add a tab to a document",
     "description": CONFIRM_RULE + " " + "Add a named tab to a document, optionally with text (e.g. 'Save what we discussed into a new tab called Partnerships'). position 1 = first.",
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "doc_id": {"type": "integer"}, "name": {"type": "string"}, "content": {"type": "string"}, "position": {"type": "integer"}},
                     "required": ["doc_id", "name"]}, "read": False},
    {"name": "tab_update", "title": "Rename or reorder a tab",
     "description": "Rename a tab and/or move it (position 1 = first). To change its text use doc_write with tab.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "tab": {"type": "string", "description": "Tab name or id"}, "name": {"type": "string"},
                                                      "position": {"type": "integer"}}, "required": ["doc_id", "tab"]}, "read": False},
    {"name": "tab_delete", "title": "Delete a tab", "destructive": True,
     "description": "Delete one tab of a document. Only when the user asks; if it has a lot of text, confirm with the user first.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "tab": {"type": "string", "description": "Tab name or id"}}, "required": ["doc_id", "tab"]}, "read": False},
    {"name": "folder_create", "title": "Create a folder",
     "description": "Create a folder at the top of a brand or funnel, or inside another folder (parent_id).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"}, "parent_id": {"type": "integer"}}, "required": ["name"]}, "read": False},
    {"name": "folder_update", "title": "Rename or move a folder",
     "description": "Rename a folder, or move it into another folder (parent_id) or to the top of a brand or funnel. Everything inside moves with it.",
     "inputSchema": {"type": "object", "properties": {"folder_id": {"type": "integer"}, "name": {"type": "string"}, "parent_id": {"type": "integer"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"},
                                                      "to_top": {"type": "boolean"}}, "required": ["folder_id"]}, "read": False},
    {"name": "folder_delete", "title": "Delete a folder", "destructive": True,
     "description": "Delete a folder. One with folders or documents inside is refused unless with_contents=true, which the user must confirm first.",
     "inputSchema": {"type": "object", "properties": {"folder_id": {"type": "integer"}, "with_contents": {"type": "boolean"}}, "required": ["folder_id"]}, "read": False},
    # --- notes and the communication history (calls, meetings and conversations logged by hand next to messages, emails and recordings) ---
    {"name": "find_companies", "title": "Find companies",
     "description": "Companies in LyteKeepr by name (empty lists them). Returns company_id, name, website and how many contacts.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}, "read": True},
    {"name": "get_company", "title": "Company overview",
     "description": "One company: name, website, notes, its people (with funnel and stage), its opportunities, and the recent history of everyone there (newest first).",
     "inputSchema": {"type": "object", "properties": {"company_id": {"type": "integer"}}, "required": ["company_id"]}, "read": True},
    {"name": "update_company", "title": "Update a company",
     "description": CONFIRM_RULE + " (This applies to add_note.) " + "Change a company's name or website, or add a note to it (added at the end, nothing is replaced). To put a contact in a company, use update_contact with company.",
     "inputSchema": {"type": "object", "properties": {"company_id": {"type": "integer"}, "name": {"type": "string"}, "website": {"type": "string"},
                                                      "add_note": {"type": "string"},
                                                      "user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}},
                     "required": ["company_id"]}},
    {"name": "add_contact_note", "title": "Add a note to a contact",
     "description": (CONFIRM_RULE + " " + "A general note about a contact (not a conversation), dated and kept with them, e.g. 'prefers mornings', 'decides with her partner'. "
                     "For something that was said in a call, meeting or talk, use log_conversation instead."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "contact_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["contact_id", "text"]}, "read": False},
    {"name": "log_conversation", "title": "Log a call or conversation",
     "description": (CONFIRM_RULE + " " + "Record a phone call, meeting, in-person talk, video call or other conversation that no integration captured, in the contact's communication history "
                     "(e.g. 'I just spoke with John: the customer hasn't approved the color, don't proceed with the order'). Write what was said in the user's words; "
                     "when = when it happened (default now); add other_contact_ids if more people were in it, and opportunity_id when it was about one."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "contact_id": {"type": "integer"}, "type": {"type": "string", "enum": ["phone call", "meeting", "in person", "video call", "other"]},
                                                      "text": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DD HH:MM"},
                                                      "opportunity_id": {"type": "integer"}, "other_contact_ids": {"type": "array", "items": {"type": "integer"}}},
                     "required": ["contact_id", "text"]}, "read": False},
    {"name": "edit_contact_record", "title": "Edit a note or logged conversation",
     "description": CONFIRM_RULE + " " + "Change the text, date or type of a note or logged conversation (record_id comes from get_contact, the histories or search_contact_notes).",
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean", "description": "true only after the user approved the cleaned-up text shown in the chat"}, "record_id": {"type": "integer"}, "text": {"type": "string"}, "when": {"type": "string"},
                                                      "type": {"type": "string"}, "opportunity_id": {"type": "integer"}}, "required": ["record_id"]}, "read": False},
    {"name": "delete_contact_record", "title": "Delete a note or logged conversation", "destructive": True,
     "description": "Delete a note or logged conversation. Only when the user asks.",
     "inputSchema": {"type": "object", "properties": {"record_id": {"type": "integer"}}, "required": ["record_id"]}, "read": False},
    {"name": "search_contact_notes", "title": "Search notes and logged conversations",
     "description": "Find notes and logged conversations by words in their text, optionally for one contact or one opportunity, or only kind 'note' or 'conversation'.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "contact_id": {"type": "integer"}, "opportunity_id": {"type": "integer"},
                                                      "kind": {"type": "string", "enum": ["note", "conversation"]}}, "required": ["query"]}, "read": True},
    {"name": "get_contact_history", "title": "A contact's communication history",
     "description": ("Everything that happened with one contact, oldest first: LinkedIn messages, emails (subject only), call recordings (summaries) and logged calls, "
                     "meetings and conversations. Narrow with channels (e.g. ['linkedin','phone call']), query words, or since (YYYY-MM-DD)."),
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "channels": {"type": "array", "items": {"type": "string"}},
                                                      "query": {"type": "string"}, "since": {"type": "string"}}, "required": ["contact_id"]}, "read": True},
    {"name": "get_opportunity_history", "title": "An opportunity's communication history",
     "description": ("The combined history of everyone on an opportunity, oldest first; each entry says who was in it and when. Use it to trace who said what, "
                     "when a decision changed, and contradictions (e.g. 'what did everyone say about the color and when did it change?' → query 'color'). "
                     "Narrow with contact_ids, channels, query or since."),
     "inputSchema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "contact_ids": {"type": "array", "items": {"type": "integer"}},
                                                      "channels": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string"}, "since": {"type": "string"}},
                     "required": ["opportunity_id"]}, "read": True},
    # --- emails saved in LyteKeepr (the LyteKeepr tab); the admin sees all, team members only with "Sees my emails" and only their contacts' ---
    {"name": "email_lookup", "title": "Look up emails",
     "description": ("Quick: find saved emails by contact (name, id or address), words in subject/preview/sender, and since (YYYY-MM-DD). Returns who, when, subject, "
                     "a short preview and whether it was from the user or to them, newest first. Enough for 'did he reply?'. in_body=true also searches "
                     "inside the full text (slower). Searches the emails saved in LyteKeepr and, for the admin, the Primary-inbox emails from people who are not "
                     "contacts yet (not_saved: true). Promotions, social and automated mail are not kept, so they never show."),
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string"}, "query": {"type": "string"}, "since": {"type": "string"},
                                                      "in_body": {"type": "boolean"}, "limit": {"type": "integer"}}}, "read": True},
    {"name": "reply_email", "title": "Reply to an email", "destructive": True,
     "description": (CONFIRM_RULE + " " + "Reply inside the thread of one saved email (email_id from email_lookup or email_latest), or of a contact's latest email (contact): "
                     "it goes from the user's Gmail as 'Re: <subject>' to the sender (or, if the user wrote the last one, to its recipients), and is saved on the contact. "
                     "Show the user who it goes to, the subject and the exact reply first; send only after their yes."),
     "inputSchema": {"type": "object", "properties": {"user_approved_text": {"type": "boolean"}, "email_id": {"type": "string"}, "contact": {"type": "string"},
                                                      "text": {"type": "string", "description": "The reply"}}, "required": ["text"]}, "read": False},
    {"name": "email_latest", "title": "Latest emails with a contact",
     "description": "The last email from a contact and the user's last email to them, in full, and whether they replied after it. The everyday 'what did he say back?'.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "Name, contact id or email address"}}, "required": ["contact"]}, "read": True},
    {"name": "email_thread", "title": "A whole email thread",
     "description": "Every saved email of one conversation, oldest first, in full (email_id or thread_id from email_lookup). For deep reading or summaries only.",
     "inputSchema": {"type": "object", "properties": {"email_id": {"type": "string"}, "thread_id": {"type": "string"}}}, "read": True},
    # --- goals and tasks on Home (each person's own; goals: today's and this week's) ---
    {"name": "list_goals", "title": "Goals", "description": ("The user's goals for today and this week (done or not, with ids), unfinished goals from earlier days, "
                                                            "and goals teammates chose to share. Tasks are in get_agenda."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "add_goal", "title": "Add a goal", "description": "Add a goal for today (period 'day') or this week (period 'week'). Short, as the user said it. shared=true shows it to the team.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "period": {"type": "string", "enum": ["day", "week"]}, "shared": {"type": "boolean"}},
                     "required": ["text"]}, "read": False},
    {"name": "update_goal", "title": "Change a goal", "description": "Tick a goal done (done=true) or reopen it, rename it, show it to the team or hide it (shared), or move an earlier unfinished daily goal to today (to_today=true).",
     "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "integer"}, "done": {"type": "boolean"}, "text": {"type": "string"}, "shared": {"type": "boolean"},
                                                      "to_today": {"type": "boolean"}}, "required": ["goal_id"]}, "read": False},
    {"name": "delete_goal", "title": "Delete a goal", "destructive": True, "description": "Delete one of the user's goals. Only when asked.",
     "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "integer"}}, "required": ["goal_id"]}, "read": False},
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
    {"name": "send_email", "title": "Send an email", "destructive": True,
     "description": ("Send a real email from the user's Gmail to one contact (to their saved email address). FIRST show the user the full email in the chat "
                     "(to, subject, body) and ask; call this only after they clearly say to send it, with exactly that text. Never send without that yes. "
                     "It is saved on the contact and their status becomes 'email sent' with the usual follow-up."),
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "Name of the contact"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["contact", "subject", "body"]}, "read": False},
    {"name": "draft_linkedin_message", "title": "Draft a LinkedIn message",
     "description": "Write a LinkedIn message to one contact and leave it in LyteKeepr for the user to send. NOTHING is sent until they press Send now in LyteKeepr.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string"}, "text": {"type": "string"}}, "required": ["contact", "text"]}, "read": False},
]


for _t in TOOLS:                                       # a tool that changes something says so by leaving "read" out
    _t.setdefault("read", False)


def tool_list() -> list:
    """What Claude gets for tools/list (one bad tool here hides every tool, so tests build it too)."""
    return [{"name": t["name"], "title": t["title"], "description": t["description"], "inputSchema": t["inputSchema"],
             "annotations": {"title": t["title"], "readOnlyHint": bool(t.get("read")), "destructiveHint": bool(t.get("destructive")), "openWorldHint": False}}
            for t in TOOLS]


def _run_tool(call, name: str, args: dict):
    """call(method, path, body) -> (status, json). Returns what Claude gets back."""
    def need(status, data):
        if status >= 400:
            raise ValueError((data or {}).get("error") or f"LyteKeepr answered with an error ({status})")
        return data

    field = CONFIRM_TEXT_TOOLS.get(name)
    dictated = field and ((args.get(field) or "").strip() or (name == "doc_create" and any((t.get("content") or "").strip() for t in args.get("tabs") or [])))
    if dictated and args.get("user_approved_text") is not True:
        return {"saved": False, "do_this_first": "Nothing was saved. Show the user the cleaned-up text in the chat and ask if it is good to save. "
                                                  "When they say yes, call again with that exact text and user_approved_text=true."}

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
        funnels = [{"kind": "funnel", "funnel_id": f["funnel_id"], "name": f["name"]} for f in need(*call("GET", "/api/crm/funnels"))["funnels"]
                   if text.lower() and text.lower() in (f["name"] or "").lower()]
        if not people and not opps and not funnels:
            return _did_you_mean(_close_matches(call, need, text))
        return {"contacts": people, "other_matching_opportunities": opps, "funnels": funnels}
    if name == "list_brands":
        return need(*call("GET", "/api/crm/brands"))["brands"]
    if name == "create_brand":
        return need(*call("POST", "/api/crm/brands", {"name": args.get("name", "")}))["brand"]
    if name == "set_funnel_brand":
        return need(*call("POST", f"/api/crm/funnels/{int(args['funnel_id'])}/brand", {"brand": args.get("brand", "")}))
    if name == "search_contacts":
        limit = max(1, min(int(args.get("limit") or 15), 50))
        wanted = _brand_key(call, need, args.get("brand"))
        q = urllib.parse.urlencode({"q": str(args.get("query") or ""), "limit": 500 if wanted is not None else limit})
        rows = need(*call("GET", f"/api/calendar/contacts?{q}"))["contacts"]
        if wanted is not None:
            rows = [c for c in rows if (c.get("brand") or "") == wanted][:limit]
        if not rows and str(args.get("query") or "").strip():
            return _did_you_mean(_close_matches(call, need, args["query"], ("contact",)))
        return [{"id": c["id"], "name": c.get("display"), "company": c.get("company"), "email": c.get("email"), "instagram": c.get("instagram") or None, "channel": c.get("channel"),
                 "brand": c.get("brand") or "", "funnel": c.get("funnel_name") or "", "stage": c.get("stage_name") or "", "job_title": c.get("job_title")} for c in rows]
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
    if name == "contacts_gone_quiet":
        q = {k: int(args[k]) for k in ("days", "you_days") if args.get(k) not in (None, "")}
        q["limit"] = int(args.get("limit") or 50)
        brand = str(args.get("brand") or "").strip()
        said = str(args.get("brand_from_user") or "").lower()
        listed = need(*call("GET", "/api/crm/brands")).get("brands", [])
        names = [b.get("name") for b in listed]
        wants_all = brand.lower() in ("all", "all brands", "every brand", "everything")
        from .brands import best_match                  # typos count: "Nexi" in the user's words names Nexy
        meant = None if wants_all else best_match(brand, listed)
        words = re.findall(r"[a-z0-9]+", said)
        phrases = words + [a + b for a, b in zip(words, words[1:])]
        named = any(w in said for w in ("all", "every", "everything", "both")) if wants_all else \
            bool(meant) and any((best_match(p, listed) or {}).get("key") == meant["key"] for p in phrases if len(p) >= 3)
        if meant:
            brand = meant["key"]
        if not brand or not named:                         # the brand comes from the user's words, never from Claude
            return {"searched": False, "ask_the_user": "Which brand: " + ", ".join(names) + ", or all of them?"}
        if not wants_all:
            q["brand"] = brand
        if args.get("side") in ("you", "them"):
            q["side"] = args["side"]
        return need(*call("GET", "/api/crm/quiet?" + urllib.parse.urlencode(q)))
    if name == "snooze_quiet_contact":
        return need(*call("POST", f"/api/crm/quiet/{int(args['contact_id'])}/snooze", {"days": int(args.get("days") or 14)}))
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
    if name == "autopilot_queue":
        return need(*call("GET", "/api/calendar/autopilot/queue"))
    if name == "save_message_review":
        return need(*call("POST", "/api/calendar/inbox/review", {k: args.get(k) or [] for k in ("reviewed", "flags", "follow_ups")}))
    if name == "find_companies":
        rows = need(*call("GET", "/api/calendar/companies?q=" + urllib.parse.quote(str(args.get("query") or ""))))["companies"]
        return [{"company_id": r["id"], "name": r["name"], "website": r.get("website") or "", "contacts": r.get("contacts", 0)} for r in rows[:50]]
    if name == "get_company":
        return need(*call("GET", f"/api/calendar/companies/{int(args['company_id'])}/overview"))
    if name == "update_company":
        cur = need(*call("GET", f"/api/calendar/companies/{int(args['company_id'])}"))
        notes = cur.get("notes") or ""
        if str(args.get("add_note") or "").strip():
            notes = (notes.rstrip() + "\n\n" if notes.strip() else "") + str(args["add_note"]).strip()
        body = {"name": args.get("name") or cur.get("name"), "website": args["website"] if args.get("website") is not None else (cur.get("website") or ""), "notes": notes}
        out = need(*call("PUT", f"/api/calendar/companies/{int(args['company_id'])}", body))
        return {"company_id": out.get("id"), "name": out.get("name"), "website": out.get("website"), "notes": out.get("notes")}
    if name == "save_onboarding":
        return need(*call("POST", "/api/calendar/autopilot/onboarding", {k: args.get(k) for k in ("contact_id", "subject", "body", "reminders")}))
    if name == "set_stage_won":
        return need(*call("POST", f"/api/crm/funnels/stages/{int(args['stage_id'])}/won", {"won": args.get("won", True) is not False}))
    if name in ("save_meeting_prep", "save_meeting_brief"):
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/prep",
                          {"text": args.get("text", ""), "replace": name == "save_meeting_prep" and args.get("replace") is True}))
    if name == "save_call_summary":
        return need(*call("PUT", f"/api/calls/{int(args['call_id'])}/summary", {"summary": args.get("summary", ""), "replace": args.get("replace") is True,
                                                                               "no_contact": args.get("no_contact") is True}))
    if name == "link_call_to_contact":
        return need(*call("POST", f"/api/calls/{int(args['call_id'])}/contacts", {"contact_id": int(args["contact_id"])}))
    if name == "save_followup_draft":
        return need(*call("POST", "/api/calendar/autopilot/drafts", {k: args.get(k) for k in ("call_id", "item_id", "subject", "body")}))
    if name == "get_call":
        return need(*call("GET", f"/api/calls/{int(args['call_id'])}"))["call"]
    if name == "create_task":
        body = {"title": str(args.get("title") or "").strip(), "project": _brand_key(call, need, args.get("brand")) or "lytekeepr", "kind": "task", "start_date": args.get("date"),
                "start_time": args.get("time") or "08:00", "untimed": not args.get("time"), "notes": str(args.get("notes") or "")}
        return need(*call("POST", "/api/calendar/events", body))
    if name == "create_reminder":
        return need(*call("POST", "/api/reminders", {"text": args.get("text"), "due_date": args.get("due_date")}))
    if name == "mark_task_done":
        return need(*call("POST", f"/api/calendar/events/{int(args['task_id'])}/done", {"date": args["date"], "done": bool(args.get("done", True))}))
    if name == "create_contact":
        body = {k: args[k] for k in ("name", "company", "job_title", "email", "phone", "instagram", "notes", "brand") if args.get(k)}
        if args.get("linkedin") or args.get("profile_url"):
            body["url"] = args.get("linkedin") or args.get("profile_url")
        c = need(*call("POST", "/api/calendar/contacts", body))
        return {"id": c.get("id"), "name": c.get("display"), "company": c.get("company"), "email": c.get("email"), "instagram": c.get("instagram"), "link": c.get("url"),
                "brand": c.get("brand") or ""}
    if name == "update_contact":
        pid = int(args["contact_id"])
        body = {k: args[k] for k in ("name", "company", "job_title", "email", "phone", "instagram") if args.get(k)}
        if "brand" in args and args["brand"] is not None:
            body["brand"] = args["brand"]
        if args.get("linkedin") or args.get("profile_url"):
            body["url"] = args.get("linkedin") or args.get("profile_url")
        if args.get("add_note"):                     # a dated note of its own (see add_contact_note)
            need(*call("POST", f"/api/crm/people/{pid}/notes", {"text": args["add_note"]}))
            if not body:
                return {"id": pid, "note_added": True}
        if not body:
            raise ValueError("Nothing to change.")
        c = need(*call("PUT", f"/api/calendar/contacts/{pid}", body))
        return {"id": c.get("id"), "name": c.get("display"), "updated": sorted(body), "brand": c.get("brand") or ""}
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
        body = {"channel": args["channel"], "status": args["status"]}
        if args.get("follow_up_days") is not None:
            body["follow_up_days"] = int(args["follow_up_days"])
        return need(*call("POST", f"/api/calendar/people/{int(args['contact_id'])}/outreach", body))
    if name == "clear_follow_ups":
        body = {k: args.get(k) for k in ("channel", "brand", "older_than_days", "contact_id", "include_replies")}
        body["preview"] = args.get("preview", True) is not False
        if body["brand"]:
            body["brand"] = _brand_key(call, need, body["brand"])
        return need(*call("POST", "/api/calendar/follow-ups/clear", body))
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
        if args.get("notes"):                          # added to the private notes, never replacing them
            need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/private-notes", {"text": args["notes"], "mode": "append"}))
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/meeting", {"meeting_status": args["status"]}))
    if name == "book_meeting":
        body = {"title": args["title"], "start_date": args["date"], "start_time": args["time"], "duration_min": int(args.get("duration_min") or 30),
                "invite": ", ".join(str(x) for x in (args.get("invite") or [])), "meet": args.get("google_meet", True) is not False,
                "expect_recording": args.get("record_with_fathom", True) is not False, "notes": str(args.get("notes") or "")}     # on unless told not to
        m = need(*call("POST", "/api/calendar/meetings", body))
        return {"booked": True, "item_id": m.get("id"), "title": m.get("title"), "date": m.get("start_date"), "time": m.get("start_time"),
                "google_meet": m.get("join_url") or None, "invited": [a.get("email") for a in m.get("attendees") or []]}
    if name == "get_meeting_notes":
        return need(*call("GET", f"/api/calendar/events/{int(args['item_id'])}/private-notes"))
    if name == "write_meeting_notes":
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/private-notes", {"text": args.get("text", ""), "mode": args.get("mode") or "append"}))
    if name == "set_meeting_agenda":
        return need(*call("POST", f"/api/calendar/meetings/{int(args['item_id'])}/description",
                          {"text": args.get("text", ""), "mode": args.get("mode") or "append", "notify": bool(args.get("notify"))}))
    if name == "set_meeting_recording":
        ev = need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/meeting", {"expect_recording": bool(args.get("record"))}))
        return {"item_id": ev.get("id"), "title": ev.get("title"), "record_with_fathom": bool(ev.get("expect_recording"))}
    if name == "cancel_meeting":
        return need(*call("DELETE", f"/api/calendar/meetings/{int(args['item_id'])}"))
    if name == "find_opportunities":
        found = need(*call("GET", "/api/crm/opportunities?q=" + urllib.parse.quote(str(args.get("query") or ""))))["opportunities"]
        if not found and str(args.get("query") or "").strip():
            return _did_you_mean(_close_matches(call, need, args["query"], ("opportunity", "funnel")))
        return found
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
    if name == "docs_places":
        return need(*call("GET", "/api/crm/docs"))["places"]
    if name in ("docs_list", "docs_search"):
        q = {k: args[k] for k in ("brand", "funnel_id") if args.get(k)}
        if name == "docs_list":
            return need(*call("GET", "/api/crm/docs/tree?" + urllib.parse.urlencode(q)))
        q.update({"q": args.get("query", ""), "content": "0" if args.get("names_only") else "1"})
        return need(*call("GET", "/api/crm/docs/search?" + urllib.parse.urlencode(q)))["results"]
    if name == "doc_read":
        q = {k: args[k] for k in ("tab", "section") if args.get(k)}
        return need(*call("GET", f"/api/crm/docs/{int(args['doc_id'])}" + ("?" + urllib.parse.urlencode(q) if q else "")))
    if name == "doc_create":
        return need(*call("POST", "/api/crm/docs", {k: args[k] for k in ("title", "brand", "funnel_id", "folder_id", "content", "tabs") if args.get(k) is not None}))
    if name == "doc_write":
        return need(*call("POST", f"/api/crm/docs/{int(args['doc_id'])}/write", {k: args[k] for k in ("text", "tab", "mode", "section", "find") if args.get(k) is not None}))
    if name == "doc_update":
        return need(*call("PUT", f"/api/crm/docs/{int(args['doc_id'])}", {k: args[k] for k in ("title", "folder_id", "brand", "funnel_id", "to_top") if args.get(k) is not None}))
    if name == "doc_delete":
        return need(*call("POST", f"/api/crm/docs/{int(args['doc_id'])}/delete"))
    if name in ("tab_update", "tab_delete"):
        doc = need(*call("GET", f"/api/crm/docs/{int(args['doc_id'])}"))
        tab = str(args.get("tab") or "").strip()
        match = next((t for t in doc.get("tabs") or [] if str(t["tab_id"]) == tab or t["name"].lower() == tab.lower()), None)
        if not match:
            raise ValueError(f"This document has no tab '{tab}'. Tabs: {', '.join(t['name'] for t in doc.get('tabs') or []) or 'none'}.")
        path = f"/api/crm/docs/{int(args['doc_id'])}/tabs/{match['tab_id']}"
        if name == "tab_delete":
            return need(*call("POST", path + "/delete"))
        return need(*call("PUT", path, {k: args[k] for k in ("name", "position") if args.get(k) is not None}))
    if name == "tab_create":
        return need(*call("POST", f"/api/crm/docs/{int(args['doc_id'])}/tabs", {k: args[k] for k in ("name", "content", "position") if args.get(k) is not None}))
    if name == "folder_create":
        return need(*call("POST", "/api/crm/docs/folders", {k: args[k] for k in ("name", "brand", "funnel_id", "parent_id") if args.get(k) is not None}))
    if name == "folder_update":
        return need(*call("PUT", f"/api/crm/docs/folders/{int(args['folder_id'])}", {k: args[k] for k in ("name", "parent_id", "brand", "funnel_id", "to_top") if args.get(k) is not None}))
    if name == "folder_delete":
        return need(*call("POST", f"/api/crm/docs/folders/{int(args['folder_id'])}/delete", {"with_contents": bool(args.get("with_contents"))}))
    if name == "add_contact_note":
        return need(*call("POST", f"/api/crm/people/{int(args['contact_id'])}/notes", {"text": args.get("text", "")}))
    if name == "log_conversation":
        return need(*call("POST", f"/api/crm/people/{int(args['contact_id'])}/conversations",
                          {k: args[k] for k in ("type", "text", "when", "opportunity_id", "other_contact_ids") if args.get(k) is not None}))
    if name == "edit_contact_record":
        return need(*call("PUT", f"/api/crm/records/{int(args['record_id'])}", {k: args[k] for k in ("text", "when", "type", "opportunity_id") if args.get(k) is not None}))
    if name == "delete_contact_record":
        return need(*call("POST", f"/api/crm/records/{int(args['record_id'])}/delete"))
    if name == "search_contact_notes":
        q = {"q": args.get("query", ""), **{k: args[k] for k in ("contact_id", "opportunity_id", "kind") if args.get(k)}}
        return need(*call("GET", "/api/crm/records/search?" + urllib.parse.urlencode(q)))["results"]
    if name in ("get_contact_history", "get_opportunity_history"):
        if name == "get_contact_history":
            items, extra = need(*call("GET", f"/api/crm/people/{int(args['contact_id'])}/history"))["history"], {}
        else:
            h = need(*call("GET", f"/api/crm/opportunities/{int(args['opportunity_id'])}/history"))
            items, extra = h["history"], {"opportunity": h["name"], "people": h["people"]}
            if args.get("contact_ids"):
                want = {int(i) for i in args["contact_ids"]}
                items = [m for m in items if want & {c["contact_id"] for c in m.get("contacts") or []}]
        if args.get("channels"):
            want = {str(c).lower() for c in args["channels"]}
            items = [m for m in items if (m.get("channel") or "").lower() in want]
        if args.get("since"):
            items = [m for m in items if (m.get("timestamp") or "") >= str(args["since"])]
        out = []
        for m in items:
            text = m.get("message_text") or ""
            if m.get("channel") == "email":              # email text stays in LyteKeepr: the subject only
                text = text.split("\n\n", 1)[0] if text.startswith("Subject: ") else ""
            row = {"when": m.get("timestamp"), "channel": m.get("channel"), "direction": m.get("direction"), "text": text}
            if m.get("logged"):
                row.update({"record_id": m.get("record_id"), "logged_by": m.get("sender"), "people": [p["name"] for p in m.get("people") or []],
                            "opportunity": m.get("opportunity") or None})
            if m.get("call"):
                row.update({"title": m["call"].get("title"), "call_id": m["call"].get("call_id")})
            if m.get("contacts"):
                row["contacts"] = [c["name"] for c in m["contacts"]]
            out.append(row)
        if args.get("query"):
            words = [w for w in str(args["query"]).lower().split() if w]
            out = [r for r in out if all(w in (r.get("text") or "").lower() or w in (r.get("title") or "").lower() for w in words)]
        return {**extra, "history": out[-200:]}
    if name == "email_lookup":
        q = {"contact": args.get("contact") or "", "q": args.get("query") or "", "since": args.get("since") or "",
             "in_body": "1" if args.get("in_body") else "0", "limit": args.get("limit") or 20}
        return need(*call("GET", "/api/crm/email/lookup?" + urllib.parse.urlencode({k: v for k, v in q.items() if v not in ("", None)})))
    if name == "reply_email":
        return need(*call("POST", "/api/crm/email/reply", {k: args[k] for k in ("text", "email_id", "contact") if args.get(k)}))
    if name == "email_latest":
        return need(*call("GET", "/api/crm/email/latest?" + urllib.parse.urlencode({"contact": args.get("contact", "")})))
    if name == "email_thread":
        return need(*call("GET", "/api/crm/email/thread?" + urllib.parse.urlencode({k: args[k] for k in ("email_id", "thread_id") if args.get(k)})))
    if name == "list_goals":
        return need(*call("GET", "/api/calendar/goals"))
    if name == "add_goal":
        return need(*call("POST", "/api/calendar/goals", {"text": args.get("text", ""), "period": args.get("period") or "day", "shared": bool(args.get("shared"))}))
    if name == "update_goal":
        gid = int(args["goal_id"])
        out = None
        if args.get("to_today"):
            out = need(*call("POST", f"/api/calendar/goals/{gid}/to-today"))
        body = {k: args[k] for k in ("done", "text", "shared") if args.get(k) is not None}
        if body:
            out = need(*call("PUT", f"/api/calendar/goals/{gid}", body))
        return out or {"unchanged": True}
    if name == "delete_goal":
        return need(*call("POST", f"/api/calendar/goals/{int(args['goal_id'])}/delete"))
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
    if name == "send_email":                       # the user approved it in the chat and in the Claude app's Allow prompt
        out = need(*call("POST", "/api/claude/home/draft_email_message", {"person_name": args["contact"], "subject": args["subject"], "body": args["body"]}))
        if out.get("status") != "pending_confirmation":
            return out                              # e.g. no such contact, or no email address: nothing was sent
        return need(*call("POST", "/api/email/send-pending/confirm"))
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
                       "instructions": ("LyteKeepr is the user's app: their contacts, funnels, calendar, emails, calls, documents, goals and tasks live in it. "
                                        "Dates are YYYY-MM-DD and times HH:MM in the user's local time. Find a contact's id with search_contacts "
                                        "before get_contact; when the user names someone or something, use find first. Funnels hold two kinds of entries: contacts (one person) and opportunities (one shared "
                                        "deal or project with several participants). Never turn one into the other, and never assume everyone at a "
                                        "company is part of an opportunity. Ask the user before creating or changing anything (except the scheduled autopilot run, which follows autopilot_queue).")})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": tool_list()})
        if method == "tools/call":
            name, args = params.get("name", ""), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            try:
                result = _run_tool(_caller(owner), name, args)
                text = json.dumps(result, ensure_ascii=False, default=str)
                if not next(t for t in TOOLS if t["name"] == name).get("read"):
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
