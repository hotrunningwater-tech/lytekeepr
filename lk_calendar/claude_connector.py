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


def _asked_to_save(words) -> bool:
    """Did the user ask to save (typos forgiven: 'sav', 'saev', 'keeep', 'drfat')?"""
    import difflib
    for w in re.findall(r"[a-z]+", str(words or "").lower()):
        if any(w == k or (len(w) >= 3 and difflib.SequenceMatcher(None, w, k).ratio() >= 0.75) for k in ("save", "saving", "saved", "keep", "draft", "store")):
            return True
    return False


def _work_stage(call, need, funnel_id, stage) -> int:
    """A work pipeline's stage by its name (any capitals) → its id; an unknown name is an error listing the real ones."""
    board = need(*call("GET", f"/api/crm/funnels/{int(funnel_id)}/board"))
    hit = next((st["stage_id"] for st in board.get("stages") or [] if st["name"].strip().lower() == str(stage).strip().lower()), None)
    if hit is None:
        raise ValueError(f"No stage '{stage}'. Stages: {', '.join(st['name'] for st in board.get('stages') or [])}.")
    return hit


def _brand_name(call, need, key) -> str:
    """A brand's name as the user knows it, never its internal key ('' stays '')."""
    if not key:
        return ""
    return next((b["name"] for b in need(*call("GET", "/api/crm/brands"))["brands"] if b["key"] == key), key)


def _int_or(value, default=-1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


# Tools that save text the user dictated (speech-to-text: typos, repeats, corrections on the fly): the text is cleaned up before the first call,
# and that first call shows it (SHOW_RULE below).
CONFIRM_RULE = ("DICTATED TEXT: the user dictates by voice, with typos, repeated words and corrections on the fly. Pass the cleaned-up text "
                "(typos fixed, repeats removed, their corrections applied, their meaning and wording kept, nothing invented).")


TOOLS = [
    {"name": "find", "title": "Find a person, company or opportunity",
     "description": ("START HERE when the user names someone or something. Searches contacts AND opportunities at once and says what each match is: "
                     "a contact (with their own funnel and stage, if any) and every opportunity they are on (with their role), plus opportunities "
                     "whose name or company matches, and funnels whose name matches. A person can be a single contact entry in a funnel, a participant in an opportunity, or both. "
                     "Names often arrive misspelled by speech-to-text: when nothing matches exactly, the result has did_you_mean with the closest names; "
                     "then just ask 'Did you mean …?' in one line. With a brand, or no words, it lists contacts (id, name, company, email, channel, brand, funnel, stage)."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "Name, company, email or words from notes; empty lists the newest contacts."},
                                                      "brand": {"type": "string", "description": "Only contacts of this brand (name or key); 'none' = contacts with no brand yet. Lists contacts."},
                                                      "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}}, "read": True},
    {"name": "list_brands", "title": "List brands",
     "description": ("The brands (businesses) this account runs, e.g. LyteKeepr, Aspan, Nexy. Contacts, funnels and calendar items each belong to one brand; "
                     "a contact in a funnel has the funnel's brand. Contacts with no brand yet show brand ''."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "create_brand", "title": "Add a brand", "description": "Add a new brand (business). Only when the user asks for it.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, "read": False},
    {"name": "work_items", "title": "Work pipeline items", "work": True,
     "description": ("A work pipeline (a funnel of kind 'work' from list_funnels; it holds items, never contacts): its stages and every item "
                     "with its stage, notes and sub-items. Use only when the user asks about a work pipeline."),
     "inputSchema": {"type": "object", "properties": {"funnel_id": {"type": "integer"}}, "required": ["funnel_id"]}, "read": True},
    {"name": "create_work_item", "title": "Add an item to a work pipeline", "work": True,
     "description": "Add an item (the pipeline's own word for it: an idea, an order...) to a work pipeline. Items never have contacts.",
     "inputSchema": {"type": "object", "properties": {"funnel_id": {"type": "integer"}, "name": {"type": "string"},
                                                      "stage": {"type": "string", "description": "Stage name; empty = the first stage"},
                                                      "notes": {"type": "string"}}, "required": ["funnel_id", "name"]}, "read": False},
    {"name": "update_work_item", "title": "Change a work item", "work": True,
     "description": "Rename a work item, change its notes, move it to another stage of its pipeline, or delete it (item_id from work_items).",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "name": {"type": "string"}, "notes": {"type": "string"},
                                                      "stage": {"type": "string"}, "delete": {"type": "boolean"}}, "required": ["item_id"]}, "read": False},
    {"name": "add_sub_item", "title": "Add a sub-item", "work": True,
     "description": "Add a sub-item inside a work item (it exists only there).",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "name": {"type": "string"}, "note": {"type": "string"}},
                     "required": ["item_id", "name"]}, "read": False},
    {"name": "update_sub_item", "title": "Change a sub-item", "work": True,
     "description": "Mark a sub-item done or not done, rename it, change its note, or remove it (subitem_id from work_items).",
     "inputSchema": {"type": "object", "properties": {"subitem_id": {"type": "integer"}, "done": {"type": "boolean"}, "name": {"type": "string"},
                                                      "note": {"type": "string"}, "remove": {"type": "boolean"}}, "required": ["subitem_id"]}, "read": False},
    {"name": "funnel_templates", "title": "Funnel templates",
     "description": ("The standard funnels LyteKeepr can create, one per kind of sales process: number, name, examples, how to recognise "
                     "the business, and the stages. Use it to pick the template for create_funnel."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "create_funnel", "title": "Create a funnel",
     "description": ("Create a sales funnel from a template (funnel_templates). Pick the template from what the user says about their "
                     "business: by the sales process they manage, not only their industry; if their main work is finding prospects by cold "
                     "outreach and following up, template 1 whatever the industry. It needs a name and a brand (everyone in the funnel takes its "
                     "brand): use the user's business as the brand (create_brand first if it doesn't exist yet); never guess a brand that isn't "
                     "theirs. Leave template empty only for the older standard stages (New lead, Contacted, In conversation, Confirmed, "
                     "Onboarding, Completed, Lost). Stages can be renamed later in the app."),
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"},
                                                      "brand": {"type": "string", "description": "The brand (name or key, see list_brands)"},
                                                      "template": {"type": "integer", "description": "Template number from funnel_templates"},
                                                      "kind": {"type": "string", "enum": ["sales", "work"], "work": True,
                                                               "description": ("'work' ONLY when the user explicitly asks for a work pipeline "
                                                                               "(items, not contacts). Never suggest it; never ask sales or work. Default: sales.")},
                                                      "item_name": {"type": "string", "work": True, "description": "Work pipelines: the word for one item (Idea, Order...); default Item"},
                                                      "stages": {"type": "array", "items": {"type": "string"}, "work": True,
                                                                 "description": "Work pipelines: stage names if the user gave them; default To do, In progress, Done"}},
                     "required": ["name", "brand"]}, "read": False},
    {"name": "set_funnel_brand", "title": "Set a funnel's brand",
     "description": "Make a funnel belong to a brand. Everyone in the funnel takes that brand, and their follow-ups are filed under it. Empty brand clears the funnel's brand.",
     "inputSchema": {"type": "object", "properties": {"funnel_id": {"type": "integer"}, "brand": {"type": "string", "description": "The brand (name or key, see list_brands). When it is unclear which brand the user means, ask; never guess."}},
                     "required": ["funnel_id", "brand"]}, "read": False},
    {"name": "get_contact", "title": "Contact details",
     "description": "Everything about one contact: profile, notes, funnel and stage, LinkedIn/Instagram messages, follow-ups and their recorded calls (with summaries). Use find first to get the id.",
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
    {"name": "autopilot_queue", "title": "Scheduled run: work to do",
     "description": "The scheduled autopilot run's work list (every 2 hours). `instructions` says what to do with each section: follow it for every item, "
                    "save with the tools it names, then stop. Do not ask the user anything; nothing here is ever sent.",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "save_message_review", "title": "Scheduled run: save message review",
     "description": "Scheduled run only, never in a chat. After reading the new_messages from autopilot_queue: the keys read, the few clear flags, and times leads gave for the next contact. Call once per run. Nothing is sent.",
     "inputSchema": {"type": "object", "properties": {
         "run_key": {"type": "string", "description": "The run_key autopilot_queue gave this run"}, 
         "reviewed": {"type": "array", "items": {"type": "string"}, "description": "Every item key read on this run"},
         "flags": {"type": "array", "items": {"type": "object", "properties": {
             "key": {"type": "string"}, "kind": {"type": "string", "enum": ["interested", "waiting", "commitment"]},
             "side": {"type": "string", "enum": ["ours", "theirs"], "description": "commitments only"},
             "summary": {"type": "string"}, "quote": {"type": "string"}, "due": {"type": "string", "description": "YYYY-MM-DD, commitments only"}},
             "required": ["key", "kind", "summary"]}},
         "follow_ups": {"type": "array", "items": {"type": "object", "properties": {
             "contact_id": {"type": "integer"}, "date": {"type": "string"}, "quote": {"type": "string"}}, "required": ["contact_id", "date"]}}},
         "required": ["reviewed", "run_key"]}},
    {"name": "save_onboarding", "title": "Scheduled run: save onboarding",
     "description": "Scheduled run only, never in a chat. For a newly won contact (autopilot_queue newly_won): a welcome email draft (NOT sent; it waits on Home) and onboarding reminders for the user. "
                    "Body without a subject line, signed 'Best,' and [Your Name]. Leave subject/body empty when the contact has no email.",
     "inputSchema": {"type": "object", "properties": {"run_key": {"type": "string", "description": "The run_key autopilot_queue gave this run"}, "contact_id": {"type": "integer"}, "subject": {"type": "string"}, "body": {"type": "string"},
                                                      "reminders": {"type": "array", "items": {"type": "object", "properties": {
                                                          "title": {"type": "string"}, "days_from_now": {"type": "integer", "minimum": 0, "maximum": 180}},
                                                          "required": ["title"]}}}, "required": ["contact_id", "run_key"]}},
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
    {"name": "save_followup_draft", "title": "Scheduled run: save follow-up draft",
     "description": "Scheduled run only, never in a chat (there, save_email_draft). Save a follow-up email draft after a meeting (autopilot): call_id for a recorded call, or item_id for a meeting with no recording. "
                    "Goes to the first contact on it with an email. NOT sent: the user reviews it on Home and sends it themselves. Body without a subject line; sign off with 'Best,' and [Your Name].",
     "inputSchema": {"type": "object", "properties": {"run_key": {"type": "string", "description": "The run_key autopilot_queue gave this run"}, "call_id": {"type": "integer"}, "item_id": {"type": "integer"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["subject", "body", "run_key"]}},
    {"name": "calls_to_review", "title": "Calls to review", "description": "Recorded calls with a transcript not summarized yet, and meetings that passed with no recording (possible no-shows).",
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "get_call", "title": "Call transcript", "description": "One recorded call (from Fathom): who was on it (contacts), the summary if any, and the full transcript. Before summarizing: if contacts is empty, "
                    "ask the user who the call was with (suggest from what they said or the names in the transcript), find them with find and link the call "
                    "with link_call_to_contact after they confirm. Then summarize and save the summary with save_call_summary.",
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "integer"}}, "required": ["call_id"]}, "read": True},
    {"name": "create_task", "title": "Create a task",
     "description": "Add a task or reminder for the user ('remind me to send Anna the demo on Friday'): it shows on Home (under Actions when due, otherwise under Tasks). "
                    "When it is about a contact, give contact_id: it then also shows in 'Coming up' on their page. Today or later only. "
                    "Something a contact said or wants ('she wants a demo') is a note (add_contact_note), not a task. "
                    "A personal one (not work: 'remind me to call mom', 'pick up the dry cleaning'): personal = true, no brand and no contact; only the user sees it. "
                    "When it is unclear whether it is personal or work, ask.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM, 24-hour; leave out for no specific time"},
                                                      "notes": {"type": "string"}, "contact_id": {"type": "integer", "description": "The contact it is about, if any"},
                                                      "brand": {"type": "string", "description": "Which brand the task is for; default: the contact's brand, else LyteKeepr. " + "The brand (name or key, see list_brands). When it is unclear which brand the user means, ask; never guess."},
                                                      "personal": {"type": "boolean", "description": "true = a personal task or reminder, not work: private to the user (nobody else sees it, not even the admin)"}},
                     "required": ["title", "date"]}, "read": False},
    {"name": "mark_task_done", "title": "Mark a task done", "description": "Mark a LyteKeepr task done (or not done) on a given date. Get the id from get_agenda or list_calendar.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "date": {"type": "string", "description": "YYYY-MM-DD of that occurrence"},
                                                      "done": {"type": "boolean", "default": True}}, "required": ["task_id", "date"]}, "read": False},
    # --- added for everyday phone use: the same actions the three LyteKeepr AI chats have ---
    {"name": "create_contact", "title": "Add a contact",
     "description": "Add a new contact. It needs a name or a company, a way to reach them (email, phone, LinkedIn or Instagram) and a brand: ask for what's missing first. "
                    "Then call find first (no duplicates; update_contact if they exist). An Instagram handle goes in 'instagram' (never in the name); a LinkedIn link in 'linkedin'. From a business card photo "
                    "or a voice note: read the details the same way; what the user adds about them (e.g. 'met at the expo, decides with her partner') goes in notes. "
                    "Notes hold only what the user tells you that LyteKeepr doesn't already keep: never summaries or copies of emails, LinkedIn or Instagram messages, call recordings or logged conversations (those are already in the contact's communication history).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "The person's name; leave empty if only a handle is known"},
                                                      "company": {"type": "string"}, "job_title": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
                                                      "instagram": {"type": "string", "description": "Instagram handle like @name (or the instagram.com link)"},
                                                      "linkedin": {"type": "string", "description": "LinkedIn profile link"},
                                                      "notes": {"type": "string", "description": "Only what the user said about them that isn't in an email, message, call or logged conversation. Usually empty."},
                                                      "brand": {"type": "string", "description": "REQUIRED: the brand (name or key, see list_brands). Ask the user which brand unless they said it; never guess. With no brands yet, tell them to create one first (Settings → Brands, or create_brand)."}},
                     "required": []}, "read": False},
    {"name": "update_contact", "title": "Update a contact",
     "description": "Change a contact's details. Only the fields given change. Notes go through add_contact_note.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "name": {"type": "string"}, "company": {"type": "string"},
                                                      "job_title": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
                                                      "instagram": {"type": "string", "description": "Instagram handle like @name"}, "linkedin": {"type": "string", "description": "LinkedIn profile link"},
                                                      "brand": {"type": "string", "description": "Give the contact this brand ('' removes it). A contact in a branded funnel keeps the funnel's brand: "
                                                                                                 "change the funnel's brand or move them instead. " + "The brand (name or key, see list_brands). When it is unclear which brand the user means, ask; never guess."}},
                     "required": ["contact_id"]}, "read": False},
    {"name": "move_contact_stage", "title": "Move to a funnel stage",
     "description": "Move a contact to a stage of their funnel by the stage's name (e.g. 'In conversation'). Their outreach status follows the stage. "
                    "If the answer says needs_confirmation, show the user that message and call again with confirmed=true only after they say yes. "
                    "Give funnel_id to put them into a funnel they are not in yet.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "stage": {"type": "string"}, "funnel_id": {"type": "integer"},
                                                      "confirmed": {"type": "boolean", "description": "true only after the user confirmed the warning"}},
                     "required": ["contact_id", "stage"]}, "read": False},
    {"name": "set_outreach_status", "title": "Set outreach status",
     "description": "Set where outreach to a contact stands on a channel (linkedin, instagram or email); it can move their funnel card forward (never back). Use one of the options get_contact lists for that channel under outreach (e.g. 'email sent', 'replied', 'call booked'). "
                    "When the status is a send (a DM, message, email or connection request sent), ASK the user first: 'Follow up in 3 days, in another number of days, or no follow-up?' "
                    "and pass their answer as follow_up_days (3, their number, or 0). Never decide it yourself. If the answer says asks_follow_up, ask and call again with the same status and follow_up_days.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "channel": {"type": "string", "enum": ["linkedin", "instagram", "email"]},
                                                      "status": {"type": "string"},
                                                      "follow_up_days": {"type": "integer", "minimum": 0, "maximum": 365, "description": "After a send: days until the follow-up, 0 = none"}},
                     "required": ["contact_id", "channel", "status"]}, "read": False},
    {"name": "remove_follow_ups", "title": "Remove follow-ups", "destructive": True,
     "description": "Remove open follow-ups for ONE contact ('this one isn't needed') or for everyone in ONE funnel ('we've stopped working that funnel'). "
                    "The first call shows how many and who. "
                    "'Reply to' ones (people waiting on the user) stay unless the user says to include them. To see what follow-ups there are, use get_agenda.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "funnel": {"type": "string", "description": "Funnel name or id"},
                                                      "include_replies": {"type": "boolean"}}}},
    {"name": "set_follow_up_date", "title": "Set follow-up date", "description": "Move the outreach follow-up for a contact to a date. The lead_id per channel is in get_contact under outreach (set_outreach_status creates one if missing).",
     "inputSchema": {"type": "object", "properties": {"lead_ids": {"type": "array", "items": {"type": "integer"}}, "date": {"type": "string", "description": "YYYY-MM-DD"}},
                     "required": ["lead_ids", "date"]}, "read": False},
    {"name": "update_calendar_item", "title": "Reschedule or rename", "description": ("Change a LyteKeepr task, reminder or meeting: new date, time, title, notes "
                     "or brand. A Google Calendar meeting can only get its brand changed here (the brand is LyteKeepr's own label; Google is untouched)."),
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM"}, "notes": {"type": "string"},
                                                      "brand": {"type": "string", "description": "The brand it belongs to (see list_brands); 'personal' only for tasks and reminders"}},
                     "required": ["item_id"]}, "read": False},
    {"name": "move_overdue", "title": "Move overdue items", "description": "Move every overdue one-off task and follow-up to today or tomorrow.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string", "enum": ["today", "tomorrow"]}}, "required": ["to"]}, "read": False},
    {"name": "set_meeting_status", "title": "Meeting status", "description": "Mark a meeting scheduled, completed, no_show or cancelled. Notes on it go through write_meeting_notes.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "status": {"type": "string", "enum": ["scheduled", "completed", "no_show", "cancelled"]}}, "required": ["item_id", "status"]}, "read": False},
    {"name": "last_exchange", "title": "Did they reply? (last messages)",
     "description": "The everyday 'did she reply?' / 'what did he say back?': the contact's last message and the user's last message to them, in full, on "
                    "email, LinkedIn or Instagram (channel narrows it), and whether they wrote back after the user's last one.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "Name, contact id or email address"},
                                                      "channel": {"type": "string", "enum": ["", "email", "linkedin", "instagram"]}}, "required": ["contact"]}, "read": True},
    {"name": "search_messages", "title": "Search messages and emails",
     "description": "Search emails, LinkedIn and Instagram messages, for one contact or everyone: words, channel, and how far back. Newest first, with a "
                    "preview; open a whole email conversation with email_thread (email_id). Emails also come from people who are not contacts yet "
                    "(not_saved: true; admin only). Promotions, social and automated mail are not kept, so they never show.",
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string"}, "query": {"type": "string"},
                                                      "channel": {"type": "string", "enum": ["", "email", "linkedin", "instagram"]},
                                                      "since_days": {"type": "integer", "default": 30}, "in_body": {"type": "boolean", "description": "Emails: also search the full text (slower)"}}}, "read": True},
    {"name": "book_meeting", "title": "Book a meeting",
     "description": ("Create a real meeting in the user's Google Calendar (their chosen meetings calendar) and email the invites from Google. "
                     "Adds a Google Meet link and marks it to be recorded by Fathom. Leave record_with_fathom out: it is always on unless the user explicitly says "
                     "not to record this meeting. Today or later only. "
                     "This SENDS invitations: always confirm title, date, time and who is invited with the user first. "
                     "Invite people by contact name (as in LyteKeepr) or email address. Every meeting belongs to one of the user's brands: use the "
                     "brand of the people or funnel it's about (get_contact / list_funnels show it); if that isn't clear, ask which brand. Never leave it out."),
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "time": {"type": "string", "description": "HH:MM, 24-hour, the user's local time"},
                                                      "duration_min": {"type": "integer", "default": 30}, "invite": {"type": "array", "items": {"type": "string"}, "description": "Contact names or email addresses"},
                                                      "google_meet": {"type": "boolean", "default": True}, "record_with_fathom": {"type": "boolean", "default": True, "description": "Leave out (on). false only when the user says not to record."},
                                                      "notes": {"type": "string"},
                                                      "brand": {"type": "string", "description": "The brand this meeting is for (name or key, see list_brands)"}},
                     "required": ["title", "date", "time", "brand"]}, "read": False},
    {"name": "get_meeting_notes", "title": "A meeting's private notes",
     "description": "The user's own private notes on a meeting (notes before, during or after the call), with Claude's prep separately in `prep` and when it was written in `prepared_on` (empty = not prepped yet). item_id from get_agenda or list_calendar. Only the user sees these.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}, "read": True},
    {"name": "write_meeting_notes", "title": "Write private meeting notes",
     "description": (CONFIRM_RULE + " " + "Add to the user's private notes on a meeting (prep plan, what was said, follow-ups). mode 'append' (the default) adds at the end and keeps "
                     "everything already written; use 'replace' only when the user asks to rewrite the whole note. Never sent to Google or the guests."),
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]}},
                     "required": ["item_id", "text"]}, "read": False},
    {"name": "set_meeting_agenda", "title": "Write the meeting's description (guests see it)", "destructive": True,
     "description": (CONFIRM_RULE + " " + "Write into the Google Calendar description of a meeting the user organized, e.g. an agenda. THE GUESTS SEE IT. "
                     "mode 'append' (default) adds below the current description; 'replace' rewrites it. notify=true also emails the guests "
                     "about the change (default false). Private prep goes in write_meeting_notes instead, never here."),
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]},
                                                      "notify": {"type": "boolean"}}, "required": ["item_id", "text"]}, "read": False},
    {"name": "set_meeting_recording", "title": "Record a meeting with Fathom (on/off)",
     "description": "Switch Fathom recording on or off for a meeting already in the calendar (item_id from get_agenda or list_calendar). Only when the user asks.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "record": {"type": "boolean"}}, "required": ["item_id", "record"]}, "read": False},
    {"name": "cancel_meeting", "title": "Cancel a meeting", "destructive": True,
     "description": "Delete a meeting from the user's own Google Calendar; Google emails a cancellation to the people invited. Confirm with the user first. The id comes from get_agenda or list_calendar.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}, "read": False},
    # --- opportunities: a second kind of funnel entry (one shared deal/project with several people); contacts stay contacts ---
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
     "inputSchema": {"type": "object", "properties": {"brand": {"type": "string", "description": "Brand name or key (see list_brands), or 'personal' for the user's own Personal place (private to them, no funnels)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"}}}, "read": True},
    {"name": "docs_search", "title": "Search documents",
     "description": ("Find folders, documents and tabs by name, and passages in their text. Narrow it with brand or funnel_id when known. "
                     "Name matches come first; text matches show a snippet. Then use doc_read."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands), or 'personal' for the user's own Personal place (private to them, no funnels)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"},
                                                      "names_only": {"type": "boolean", "description": "true = don't search the text"}}, "required": ["query"]}, "read": True},
    {"name": "doc_read", "title": "Read a document",
     "description": "A document's text and all its tabs; or just one tab (tab = name or id); or one section (a # heading, by its words or number) of the text or of a tab.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "tab": {"type": "string"}, "section": {"type": "string"}}, "required": ["doc_id"]}, "read": True},
    {"name": "doc_create", "title": "Create a document",
     "description": (CONFIRM_RULE + " " + "Create a document at the top of a brand or funnel, or in a folder (folder_id), optionally with text and named tabs. "
                     "A funnel must have a brand first. If it's unclear where it belongs, ask."),
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands), or 'personal' for the user's own Personal place (private to them, no funnels)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"}, "folder_id": {"type": "integer"}, "content": {"type": "string"},
                                                      "tabs": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}}, "required": ["name"]}}},
                     "required": ["title"]}, "read": False},
    {"name": "doc_write", "title": "Write in a document",
     "description": (CONFIRM_RULE + " " + "Change the text of a document or of one of its tabs (tab = name or id), touching nothing else. mode: "
                     "'append' adds at the end (the default, e.g. saving what was just discussed); 'replace_section' replaces one # heading's part (section = its words or number); "
                     "'find_replace' replaces one exact passage (find must appear exactly once); 'replace' replaces the whole text. "
                     "Before 'replace', 'replace_section' or 'find_replace', the old text is kept as a previous version. Read first, then change only what was asked."),
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "text": {"type": "string"}, "tab": {"type": "string"},
                                                      "mode": {"type": "string", "enum": ["append", "replace_section", "find_replace", "replace"]},
                                                      "section": {"type": "string"}, "find": {"type": "string"}}, "required": ["doc_id", "text"]}, "read": False},
    {"name": "doc_update", "title": "Rename or move a document",
     "description": "Rename a document, or move it into a folder (folder_id), or to the top of a brand or funnel (brand/funnel_id, or to_top=true for the top of where it is). Its text and tabs stay.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "title": {"type": "string"}, "folder_id": {"type": "integer"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands), or 'personal' for the user's own Personal place (private to them, no funnels)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"},
                                                      "to_top": {"type": "boolean"}}, "required": ["doc_id"]}, "read": False},
    {"name": "doc_delete", "title": "Delete a document", "destructive": True,
     "description": "Delete a document with its tabs. Only when the user asks; if it has a lot of text, confirm with the user first.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}}, "required": ["doc_id"]}, "read": False},
    {"name": "tab_create", "title": "Add a tab to a document",
     "description": CONFIRM_RULE + " " + "Add a named tab to a document, optionally with text (e.g. 'Save what we discussed into a new tab called Partnerships'). position 1 = first.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "name": {"type": "string"}, "content": {"type": "string"}, "position": {"type": "integer"}},
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
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands), or 'personal' for the user's own Personal place (private to them, no funnels)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"}, "parent_id": {"type": "integer"}}, "required": ["name"]}, "read": False},
    {"name": "folder_update", "title": "Rename or move a folder",
     "description": "Rename a folder, or move it into another folder (parent_id) or to the top of a brand or funnel. Everything inside moves with it.",
     "inputSchema": {"type": "object", "properties": {"folder_id": {"type": "integer"}, "name": {"type": "string"}, "parent_id": {"type": "integer"}, "brand": {"type": "string", "description": "Brand name or key (see list_brands), or 'personal' for the user's own Personal place (private to them, no funnels)"}, "funnel_id": {"type": "integer", "description": "A funnel (it brings its own brand)"},
                                                      "to_top": {"type": "boolean"}}, "required": ["folder_id"]}, "read": False},
    {"name": "folder_delete", "title": "Delete a folder", "destructive": True,
     "description": "Delete a folder. One with folders or documents inside is refused unless with_contents=true, which the user must confirm first.",
     "inputSchema": {"type": "object", "properties": {"folder_id": {"type": "integer"}, "with_contents": {"type": "boolean"}}, "required": ["folder_id"]}, "read": False},
    # --- notes and the communication history (calls, meetings and conversations logged by hand next to messages, emails and recordings) ---
    {"name": "save_email_draft", "title": "Save email draft",
     "description": "THE one tool to save an email draft, for one person, several people, or everyone in a funnel stage: ONE call with all of them (never one call "
                    "per person). Save ONLY when the user asks ('save it', 'keep it as a draft'); pass their own words in user_said. When an email looks "
                    "finished in the chat, you may ask 'Save it as a draft?'. It saves the wording you last printed in the chat. Write {name} where each "
                    "person's first name goes (several people), or no {name} for the same text to everyone ('Hi, ...'). as_reply=true sends each copy as a "
                    "reply in that person's last email thread with the user (a new email if there is none). It shows in each person's activity as "
                    "'Draft (not sent)'. Nothing is sent here.",
     "inputSchema": {"type": "object", "properties": {"contact_ids": {"type": "array", "items": {"type": "integer"}, "description": "The people: one or more"},
                                                      "funnel": {"type": "string", "description": "Instead of contact_ids: a funnel (name or id) with its stage, for everyone in it"},
                                                      "stage": {"type": "string"}, "as_reply": {"type": "boolean"},
                                                      "subject": {"type": "string", "description": "Not needed for replies (they use Re: the thread's subject)"},
                                                      "body": {"type": "string"},
                                                      "user_said": {"type": "string", "description": "The user's own words asking to save (quote them)"}},
                     "required": ["body", "user_said"]}},
    {"name": "list_drafts", "title": "Email drafts",
     "description": "THE list of saved, unsent email drafts, all of them: to one person, to several people, replies, and the scheduled run's follow-up and "
                    "welcome drafts, newest first, each with who it is for, the subject and how many days it has been waiting. Use it for any 'what drafts do I have?'.",
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer", "description": "Only drafts that include this contact"}}}, "read": True},
    {"name": "open_draft", "title": "Open an email draft",
     "description": "One draft in full: subject, text, who it goes to (for several people: each person, who is skipped and why, and one person's copy), "
                    "and, while it is being sent, the progress (sent, failed, left).",
     "inputSchema": {"type": "object", "properties": {"draft": {"type": "string", "description": "The draft from list_drafts, e.g. 'one:12' (to one person) or 'group:3' (several people, or a reply)"}}, "required": ["draft"]}, "read": True},
    {"name": "update_draft", "title": "Change an email draft",
     "description": "Change a draft's subject or text; for a several-people draft also add or remove people, switch reply on or off, or take everyone of "
                    "another stage. Show the user the result (open_draft).",
     "inputSchema": {"type": "object", "properties": {"draft": {"type": "string", "description": "The draft from list_drafts, e.g. 'one:12' (to one person) or 'group:3' (several people, or a reply)"}, "subject": {"type": "string"}, "body": {"type": "string"},
                                                      "add_contact_ids": {"type": "array", "items": {"type": "integer"}},
                                                      "remove_contact_ids": {"type": "array", "items": {"type": "integer"}}, "stage": {"type": "string"},
                                                      "as_reply": {"type": "boolean"}},
                     "required": ["draft"]}},
    {"name": "discard_draft", "title": "Discard an email draft", "destructive": True,
     "description": "Only when the user asks. With draft: delete that draft (not one being sent). With contact_id: delete their own drafts and take them "
                    "out of every several-people draft.",
     "inputSchema": {"type": "object", "properties": {"draft": {"type": "string", "description": "The draft from list_drafts, e.g. 'one:12' (to one person) or 'group:3' (several people, or a reply)"}, "contact_id": {"type": "integer"}}}},
    {"name": "send_draft", "title": "Send an email draft", "destructive": True,
     "description": "Send a saved draft exactly as saved. Ask once: follow up in 3 days, another number of days, or no follow-up (0). Several people are sent "
                    "one by one, about a minute apart, on its own (open_draft shows progress); the first failure stops it. Each email is saved on the contact. "
                    + "SENDING: call first WITHOUT confirm_code. Nothing is sent: you get show_the_user, the email exactly as LyteKeepr will send it (to, cc, bcc, in reply to, subject, body). Show it to the user word for word, then ask 'Send? yes / no'. Only after their yes, call again with the same details and that confirm_code. If anything changed, nothing is sent and you get the new version to show.",
     "inputSchema": {"type": "object", "properties": {"draft": {"type": "string", "description": "The draft from list_drafts, e.g. 'one:12' (to one person) or 'group:3' (several people, or a reply)"}, "follow_up_days": {"type": "integer", "minimum": 0, "maximum": 365},
                                                      "confirm_code": {"type": "string", "description": "From the first call, only after the user said yes to exactly that email"}},
                     "required": ["draft", "follow_up_days"]}},
    {"name": "get_company", "title": "Company overview",
     "description": "One company (company_id from a contact's details or find): name, website, notes, its people (with funnel and stage), its opportunities, and the recent history of everyone there (newest first).",
     "inputSchema": {"type": "object", "properties": {"company_id": {"type": "integer"}}, "required": ["company_id"]}, "read": True},
    {"name": "update_company", "title": "Update a company",
     "description": CONFIRM_RULE + " (This applies to add_note.) " + "Change a company's name or website, or add a note to it (added at the end, nothing is replaced). To put a contact in a company, use update_contact with company.",
     "inputSchema": {"type": "object", "properties": {"company_id": {"type": "integer"}, "name": {"type": "string"}, "website": {"type": "string"},
                                                      "add_note": {"type": "string"}},
                     "required": ["company_id"]}},
    {"name": "add_contact_note", "title": "Add a note to a contact",
     "description": (CONFIRM_RULE + " " + "A note the user tells you about a contact, dated and kept with them, e.g. 'prefers mornings', 'decides with her partner'. "
                     "Notes hold only what the user tells you that LyteKeepr doesn't already keep: never summaries or copies of emails, LinkedIn or Instagram messages, call recordings or logged conversations (those are already in the contact's communication history). A call, meeting or talk that nothing recorded goes in log_conversation instead."),
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["contact_id", "text"]}, "read": False},
    {"name": "log_conversation", "title": "Log a call or conversation",
     "description": (CONFIRM_RULE + " " + "Record a phone call, meeting, in-person talk, video call or other conversation that no integration captured, in the contact's communication history "
                     "(e.g. 'I just spoke with John: the customer hasn't approved the color, don't proceed with the order'). Write what was said in the user's words; "
                     "when = when it happened (default now); add other_contact_ids if more people were in it, and opportunity_id when it was about one."),
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "type": {"type": "string", "enum": ["phone call", "meeting", "in person", "video call", "other"]},
                                                      "text": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DD HH:MM"},
                                                      "opportunity_id": {"type": "integer"}, "other_contact_ids": {"type": "array", "items": {"type": "integer"}}},
                     "required": ["contact_id", "text"]}, "read": False},
    {"name": "edit_contact_record", "title": "Edit a note or logged conversation",
     "description": CONFIRM_RULE + " " + "Change the text, date or type of a note or logged conversation (record_id comes from get_contact, the histories or search_contact_notes).",
     "inputSchema": {"type": "object", "properties": {"record_id": {"type": "integer"}, "text": {"type": "string"}, "when": {"type": "string"},
                                                      "type": {"type": "string"}, "opportunity_id": {"type": "integer"}}, "required": ["record_id"]}, "read": False},
    {"name": "delete_contact_record", "title": "Delete a note or logged conversation", "destructive": True,
     "description": "Delete a note or logged conversation. Only when the user asks.",
     "inputSchema": {"type": "object", "properties": {"record_id": {"type": "integer"}}, "required": ["record_id"]}, "read": False},
    {"name": "search_contact_notes", "title": "Search notes and logged conversations",
     "description": "Find notes and logged conversations by words in their text, optionally for one contact or one opportunity, or only kind 'note' or 'conversation'.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "contact_id": {"type": "integer"}, "opportunity_id": {"type": "integer"},
                                                      "kind": {"type": "string", "enum": ["note", "conversation"]}}, "required": ["query"]}, "read": True},
    {"name": "get_contact_history", "title": "A contact's communication history",
     "description": ("Everything that happened with one contact, oldest first: LinkedIn and Instagram messages, emails, call recordings (summaries) and logged calls, "
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
    {"name": "reply_email", "title": "Reply to an email", "destructive": True,
     "description": ("Reply inside the thread of one saved email (email_id from search_messages or last_exchange), or of a contact's latest email (contact: "
                     "their email address is safest; a name must match exactly one contact). It goes as 'Re: <subject>' to that email's sender (or, if the "
                     "user wrote it, to its recipients) and is saved on the contact. Clean up dictated text first. " + "SENDING: call first WITHOUT confirm_code. Nothing is sent: you get show_the_user, the email exactly as LyteKeepr will send it (to, cc, bcc, in reply to, subject, body). Show it to the user word for word, then ask 'Send? yes / no'. Only after their yes, call again with the same details and that confirm_code. If anything changed, nothing is sent and you get the new version to show."),
     "inputSchema": {"type": "object", "properties": {"email_id": {"type": "string"}, "contact": {"type": "string"},
                                                      "text": {"type": "string", "description": "The reply"},
                                                      "confirm_code": {"type": "string", "description": "From the first call, only after the user said yes to exactly that email"}},
                     "required": ["text"]}, "read": False},
    {"name": "email_thread", "title": "A whole email thread",
     "description": "Every saved email of one conversation, oldest first, in full (email_id or thread_id from search_messages or last_exchange). For deep reading or summaries only.",
     "inputSchema": {"type": "object", "properties": {"email_id": {"type": "string"}, "thread_id": {"type": "string"}}}, "read": True},
    # --- goals and tasks on Home (each person's own; goals: today's and this week's) ---
    {"name": "list_goals", "title": "Goals", "description": ("The user's goals. With no level and no brand: today's and this week's (done or not, with ids), unfinished goals "
                                                            "from earlier days, and goals teammates chose to share. With level and/or brand: every level (or that one) "
                                                            "for that brand or Personal, each with its steps and when it was ticked off (done_at). "
                                                            "Levels: year (long term, 1 year+), mid (mid term, 2-4 months), month (short term, about 1 month), week, day. "
                                                            "brand 'all' = every brand and Personal. Tasks are in get_agenda."),
     "inputSchema": {"type": "object", "properties": {"level": {"type": "string", "enum": ["year", "mid", "month", "week", "day"]},
                                                      "brand": {"type": "string", "description": "A brand name or key, 'personal', or 'all'"}}}, "read": True},
    {"name": "add_goal", "title": "Add a goal", "description": ("Add a goal, short, as the user said it. level: day (today), week (this week), month (short term, about 1 month), "
                                                               "mid (mid term, 2-4 months) or year (long term, 1 year or more). For a brand or Personal: when it is "
                                                               "unclear which, ask. target_date for month/mid/year (default 30/90/365 days from today). "
                                                               "parent_goal_id makes it a step of a bigger goal (one level: a step has no steps; it takes the goal's brand). "
                                                               "shared=true shows it to the team (brand goals, and daily/weekly ones)."),
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "level": {"type": "string", "enum": ["day", "week", "month", "mid", "year"]},
                                                      "brand": {"type": "string", "description": "A brand name or key, or 'personal' (default)"},
                                                      "target_date": {"type": "string", "description": "YYYY-MM-DD, for month/mid/year"},
                                                      "parent_goal_id": {"type": "integer"}, "shared": {"type": "boolean"}},
                     "required": ["text"]}, "read": False},
    {"name": "update_goal", "title": "Change a goal", "description": ("Tick a goal done (done=true; the time is kept) or reopen it (the time is cleared), rename it, move it to "
                                                                      "another level or target date, change its brand or Personal, show it to the team or hide it (shared), "
                                                                      "or move an earlier unfinished daily goal to today (to_today=true)."),
     "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "integer"}, "done": {"type": "boolean"}, "text": {"type": "string"}, "shared": {"type": "boolean"},
                                                      "level": {"type": "string", "enum": ["day", "week", "month", "mid", "year"]},
                                                      "target_date": {"type": "string", "description": "YYYY-MM-DD"},
                                                      "brand": {"type": "string", "description": "A brand name or key, or 'personal'"},
                                                      "to_today": {"type": "boolean"}}, "required": ["goal_id"]}, "read": False},
    {"name": "delete_goal", "title": "Delete a goal", "destructive": True, "description": "Delete one of the user's goals. Only when asked.",
     "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "integer"}}, "required": ["goal_id"]}, "read": False},
    # --- who is in touch with whom (counted from email addresses, no AI) ---
    {"name": "related_people", "title": "Who else belongs here",
     "description": ("People who may belong with a contact or an opportunity, from email facts (who appears together in From, To, CC, and in how many "
                     "separate threads) and LyteKeepr's own suggestions. With contact_id: who that person keeps emailing with. With opportunity_id: "
                     "people who appear with its active participants but are not on it yet, strongest first, with who brought them in. With neither: "
                     "every open suggestion (someone to add to an opportunity, or to save as a contact). A suggestion has a suggestion_id for "
                     "accept_suggestion / dismiss_suggestion; add others only when the user confirms (add_participant)."),
     "inputSchema": {"type": "object", "properties": {"contact_id": {"type": "integer"}, "opportunity_id": {"type": "integer"}}}, "read": True},
    {"name": "accept_suggestion", "title": "Accept a suggestion", "description": "Do what a suggestion proposes (create the contact if needed, add them with who introduced them). Only when the user says so.",
     "inputSchema": {"type": "object", "properties": {"suggestion_id": {"type": "integer"}}, "required": ["suggestion_id"]}, "read": False},
    {"name": "dismiss_suggestion", "title": "Dismiss a suggestion", "description": "Dismiss a suggestion; it will not come back.",
     "inputSchema": {"type": "object", "properties": {"suggestion_id": {"type": "integer"}}, "required": ["suggestion_id"]}, "read": False},
    {"name": "send_email", "title": "Send an email", "destructive": True,
     "description": ("Send a new email to one contact (to their saved email address). It is saved on the contact. A saved draft is sent with send_draft, "
                     "a reply in a thread with reply_email. " + "SENDING: call first WITHOUT confirm_code. Nothing is sent: you get show_the_user, the email exactly as LyteKeepr will send it (to, cc, bcc, in reply to, subject, body). Show it to the user word for word, then ask 'Send? yes / no'. Only after their yes, call again with the same details and that confirm_code. If anything changed, nothing is sent and you get the new version to show."),
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "The contact's name or email address (one contact must match)"},
                                                      "subject": {"type": "string"}, "body": {"type": "string"},
                                                      "confirm_code": {"type": "string", "description": "From the first call, only after the user said yes to exactly that email"}},
                     "required": ["contact", "subject", "body"]}, "read": False},
    {"name": "linkedin_inbox", "title": "LinkedIn people waiting for review",
     "description": ("People who wrote to the user on LinkedIn but whom the user never wrote to: they wait for review before they become contacts. "
                     "Each with their last message. For 'sort my LinkedIn': if the LyteKeepr LinkedIn add-on is installed, call its sync_linkedin first, "
                     "then this, then suggest who to keep (real prospects, people the user knows) and who to remove (spam, recruiters, sales pitches), "
                     "and do it with sort_linkedin_inbox."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "sort_linkedin_inbox", "title": "Sort LinkedIn people waiting for review",
     "description": "Keep some as contacts and remove others (a removed person is never brought back by a later sync, and can be restored for 30 days).",
     "inputSchema": {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "integer"}, "description": "person_ids to keep as contacts"},
                                                      "remove": {"type": "array", "items": {"type": "integer"}, "description": "person_ids to remove"}}},
     "read": False},
    {"name": "email_inbox", "title": "Email waiting for review",
     "description": ("Emails from people who aren't contacts yet wait for review. Here they are by sender, in groups (written: the user has written "
                     "to them; automatic: no-reply, lists, receipts; selling; never: the user never wrote to them), each sender with how many emails "
                     "and one example. For 'sort my email': call this, decide keep or remove per group (suggested: keep written, remove automatic "
                     "and selling) and per sender where the example says otherwise, list only the senders you can't judge for the user to decide, "
                     "then do it all with sort_email_inbox."),
     "inputSchema": {"type": "object", "properties": {}}, "read": True},
    {"name": "sort_email_inbox", "title": "Sort email waiting for review",
     "description": ("Keep senders as contacts (with all their waiting emails; later emails go straight onto them) and remove others (their later "
                     "emails skip review; can be brought back for 30 days with bring_back)."),
     "inputSchema": {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "string"}, "description": "sender email addresses to keep"},
                                                      "remove": {"type": "array", "items": {"type": "string"}, "description": "sender email addresses to remove"},
                                                      "bring_back": {"type": "array", "items": {"type": "string"}, "description": "removed senders to bring back"}}},
     "read": False},
    {"name": "send_linkedin_message", "title": "Send a LinkedIn message", "destructive": True,
     "description": ("Send a LinkedIn message to one contact: in their existing conversation, or a first message if LyteKeepr has their LinkedIn profile. "
                     "It goes out from the user's own computer through the LyteKeepr LinkedIn add-on (within a minute while it is on). "
                     "SENDING: call first WITHOUT confirm_code. Nothing is sent: you get show_the_user, the message exactly as it will go out. Show it word "
                     "for word and ask 'Send? yes / no'. Only after their yes, call again with the same contact and text and that confirm_code. "
                     "Several people: one message each, each shown and approved."),
     "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "The contact's name (one LinkedIn contact must match)"},
                                                      "text": {"type": "string"},
                                                      "confirm_code": {"type": "string", "description": "From the first call, only after the user said yes to exactly that message"}},
                     "required": ["contact", "text"]}, "read": False},
]


for _t in TOOLS:                                       # a tool that changes something says so by leaving "read" out
    _t.setdefault("read", False)


def _schema(schema: dict, work: bool) -> dict:
    """A tool's inputs as Claude sees them: Work pipeline options only where Work pipelines are switched on; the 'work' marks themselves never."""
    props = {k: {x: y for x, y in v.items() if x != "work"} for k, v in (schema.get("properties") or {}).items() if work or not v.get("work")}
    return {**schema, "properties": props}


def tool_list(work: bool = False) -> list:
    """What Claude gets for tools/list (one bad tool here hides every tool, so tests build it too). The Work pipeline tools and options
    exist for Claude only in a workspace where they are switched on (Owner console); everywhere else Claude never sees them."""
    return [{"name": t["name"], "title": t["title"], "description": t["description"], "inputSchema": _schema(t["inputSchema"], work),
             "annotations": {"title": t["title"], "readOnlyHint": bool(t.get("read")), "destructiveHint": bool(t.get("destructive")), "openWorldHint": False}}
            for t in TOOLS if work or not t.get("work")]


WORK_TOOLS = {t["name"] for t in TOOLS if t.get("work")}


RUN_ONLY_TOOLS = {        # name -> what Claude is told when it reaches for one outside a scheduled run
    "save_message_review": "Nothing was saved: this is for the scheduled run only (it starts with autopilot_queue).",
    "save_onboarding": "Nothing was saved: this is for the scheduled run only. In a chat, write the welcome email and save it with save_email_draft when the user asks; reminders with create_task.",
    "save_followup_draft": "Nothing was saved: this is for the scheduled run only. In a chat, save an email draft with save_email_draft when the user asks.",
}
SENDS = {"send_email", "reply_email", "send_draft", "send_linkedin_message"}          # these show the message itself (_send_preview, _linkedin_preview)
SHOWN_FIRST = {t["name"] for t in TOOLS if not t["read"]} - SENDS - set(RUN_ONLY_TOOLS)
RUN_MAY_SKIP = {"save_meeting_prep", "save_call_summary"}    # also the scheduled run's: nobody to ask there, so its run key is enough
SHOW_RULE = ("SHOWN FIRST: call first WITHOUT confirm_code. Nothing is done: you get show_the_user, exactly what LyteKeepr will do (who, what, "
             "old → new, the full text). Show it to the user word for word, then ask 'Do it? yes / no'. Only after their yes, call again with the "
             "same details and that confirm_code. If anything differs, nothing is done and you get the new version to show.")

for _t in TOOLS:
    if _t["name"] in SHOWN_FIRST:
        props = _t["inputSchema"].setdefault("properties", {})
        for gone in ("user_confirmed", "user_approved_text", "preview"):            # replaced by the showing below
            props.pop(gone, None)
        _t["inputSchema"]["required"] = [r for r in _t["inputSchema"].get("required", []) if r in props]
        props["confirm_code"] = {"type": "string", "description": "From the first call, only after the user said yes to exactly what it showed"}
        if _t["name"] in RUN_MAY_SKIP:
            props["run_key"] = {"type": "string", "description": "Scheduled run only: the run_key from autopilot_queue (then nothing is shown first)"}
        _t["description"] = _t["description"] + " " + SHOW_RULE


def _run_tool(call, name: str, args: dict):
    """call(method, path, body) -> (status, json). Returns what Claude gets back."""
    def need(status, data):
        if status >= 400:
            raise ValueError((data or {}).get("error") or f"LyteKeepr answered with an error ({status})")
        return data

    def run_key_ok():
        return bool(args.get("run_key")) and need(*call("GET", "/api/calendar/autopilot/run-key?" + urllib.parse.urlencode({"key": str(args["run_key"])}))).get("ok")

    if name in RUN_ONLY_TOOLS and not run_key_ok():  # the scheduled run's own saves: only with the key autopilot_queue handed out
        return {"saved": False, "message": RUN_ONLY_TOOLS[name]}

    if name in SHOWN_FIRST and not (name in RUN_MAY_SKIP and run_key_ok()):      # every change: shown exactly, done only with that code
        stop = _precheck(call, need, name, args)
        if stop:
            return stop
        shown = _change_preview(call, need, name, args)
        given = str(args.get("confirm_code") or "").strip()
        if given != shown["confirm_code"]:
            if given:
                shown["message"] = "Nothing was done: this is not exactly what the user saw (or it changed since). Show this version and ask again."
            return shown

    if name == "find" and (args.get("brand") or not str(args.get("query") or "").strip()):
        limit = max(1, min(int(args.get("limit") or 15), 50))
        wanted = _brand_key(call, need, args.get("brand"))
        q = urllib.parse.urlencode({"q": str(args.get("query") or ""), "limit": 500 if wanted is not None else limit})
        rows = need(*call("GET", f"/api/calendar/contacts?{q}"))["contacts"]
        if wanted is not None:
            rows = [c for c in rows if (c.get("brand") or "") == wanted][:limit]
        if not rows and str(args.get("query") or "").strip():
            return _did_you_mean(_close_matches(call, need, args["query"], ("contact",)))
        return [{"id": c["id"], "name": c.get("display"), "company": c.get("company"), "company_id": c.get("company_id"), "email": c.get("email"), "instagram": c.get("instagram") or None, "channel": c.get("channel"),
                 "brand": c.get("brand") or "", "funnel": c.get("funnel_name") or "", "stage": c.get("stage_name") or "", "job_title": c.get("job_title")} for c in rows]
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
    if name == "funnel_templates":
        return [{"number": t["number"], "name": t["name"], "examples": t["examples"], "recognise": t["recognise"],
                 "stages": [st["name"] for st in t["stages"]]} for t in need(*call("GET", "/api/crm/funnel-templates"))["templates"]]
    if name == "work_items":
        return need(*call("GET", f"/api/crm/work/funnels/{int(args['funnel_id'])}/items"))
    if name == "create_work_item":
        fid = int(args["funnel_id"])
        body = {"funnel_id": fid, "name": str(args.get("name") or ""), "notes": str(args.get("notes") or "")}
        if args.get("stage"):
            body["stage_id"] = _work_stage(call, need, fid, args["stage"])
        o = need(*call("POST", "/api/crm/work/items", body))
        return {"created": True, "item_id": o["opportunity_id"], "name": o["name"], "stage": o["stage"]}
    if name == "update_work_item":
        iid = int(args["item_id"])
        if args.get("delete"):
            need(*call("POST", f"/api/crm/work/items/{iid}/delete", {}))
            return {"deleted": True, "item_id": iid}
        o = need(*call("GET", f"/api/crm/work/items/{iid}"))
        if args.get("name") or args.get("notes") is not None:
            o = need(*call("PUT", f"/api/crm/work/items/{iid}", {k: args[k] for k in ("name", "notes") if args.get(k) is not None}))
        if args.get("stage"):
            o = need(*call("POST", f"/api/crm/work/items/{iid}/move", {"stage_id": _work_stage(call, need, o["funnel_id"], args["stage"])}))
        return {"item_id": iid, "name": o["name"], "stage": o["stage"], "notes": o["notes"], "subitems": o.get("subitems", [])}
    if name == "add_sub_item":
        o = need(*call("POST", f"/api/crm/work/items/{int(args['item_id'])}/subitems", {"name": args.get("name", ""), "note": args.get("note", "")}))
        return {"item": o["name"], "subitems": o.get("subitems", [])}
    if name == "update_sub_item":
        sid = int(args["subitem_id"])
        if args.get("remove"):
            o = need(*call("POST", f"/api/crm/work/subitems/{sid}/delete", {}))
        else:
            o = need(*call("PUT", f"/api/crm/work/subitems/{sid}", {k: args[k] for k in ("name", "note", "done") if args.get(k) is not None}))
        return {"item": o["name"], "subitems": o.get("subitems", [])}
    if name == "create_funnel":
        fname, key = str(args["name"]).strip(), _brand_key(call, need, args.get("brand"))       # checked in _precheck
        body = {"name": fname, "brand": key, **({"template": int(args["template"])} if args.get("template") not in (None, "") else {})}
        if args.get("kind") == "work":
            body.update({"kind": "work", "item_name": str(args.get("item_name") or ""), **({"stages": list(args["stages"])} if args.get("stages") else {})})
            body.pop("template", None)
        made = need(*call("POST", "/api/crm/funnels", body))
        return {"created": True, "funnel_id": made["funnel_id"], "name": fname, "brand": _brand_name(call, need, key)}
    if name == "set_funnel_brand":
        return need(*call("POST", f"/api/crm/funnels/{int(args['funnel_id'])}/brand", {"brand": args.get("brand", "")}))
    if name == "get_contact":
        pid = int(args["contact_id"])
        detail = need(*call("GET", f"/api/crm/people/{pid}"))
        emails = [m for m in detail.get("messages") or [] if m.get("channel") == "email"]
        detail["messages"] = [m for m in detail.get("messages") or [] if m.get("channel") != "email"]
        if emails:
            detail["emails"] = f"{len(emails)} email(s) with them in LyteKeepr: read them with last_exchange, search_messages (channel email) or get_contact_history."
        st_c, card = call("GET", f"/api/calendar/contacts/{pid}")
        if st_c < 400 and card.get("company_id"):
            detail["company_id"] = card["company_id"]          # for get_company / update_company
        status, outreach = call("GET", f"/api/calendar/people/{pid}/outreach")
        if status < 400:
            detail["outreach"] = outreach
        status, opps = call("GET", f"/api/crm/people/{pid}/opportunities")
        if status < 400:
            detail["opportunities"] = opps.get("opportunities", [])
        status, upcoming = call("GET", f"/api/crm/people/{pid}/coming-up")
        if status < 400:
            detail["coming_up"] = upcoming                      # meetings they're invited to, tasks about them, follow-ups (only what's ahead)
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
    if name == "save_email_draft":
        ids = [int(i) for i in (args.get("contact_ids") or ([args["contact_id"]] if args.get("contact_id") else []))]
        if len(ids) == 1 and not args.get("funnel") and not args.get("as_reply"):     # one person: their own draft
            return need(*call("PUT", f"/api/crm/people/{ids[0]}/draft", {"subject": args.get("subject", ""), "body": args.get("body", "")}))
        body = {"contact_ids": ids or None, "funnel": args.get("funnel"), "stage": args.get("stage"), "subject": args.get("subject", ""),
                "body": args.get("body", ""), "as_reply": bool(args.get("as_reply"))}
        return need(*call("POST", "/api/calendar/funnel-drafts", body))              # several people (or a reply): one draft for all of them
    if name in ("list_drafts", "open_draft", "update_draft", "discard_draft", "send_draft"):
        return _drafts_tool(call, need, name, args)
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
    if name == "save_meeting_prep":
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/prep", {"text": args.get("text", ""), "replace": args.get("replace") is True}))
    if name == "save_call_summary":
        return need(*call("PUT", f"/api/calls/{int(args['call_id'])}/summary", {"summary": args.get("summary", ""), "replace": args.get("replace") is True,
                                                                               "no_contact": args.get("no_contact") is True}))
    if name == "link_call_to_contact":
        return need(*call("POST", f"/api/calls/{int(args['call_id'])}/contacts", {"contact_id": int(args["contact_id"])}))
    if name == "save_followup_draft":
        return need(*call("POST", "/api/calendar/autopilot/drafts", {k: args.get(k) for k in ("call_id", "item_id", "subject", "body")}))
    if name == "get_call":
        return need(*call("GET", f"/api/calls/{int(args['call_id'])}"))["call"]
    if name == "create_task" and args.get("personal") is True:
        body = {"title": str(args.get("title") or "").strip(), "project": "personal", "kind": "task", "start_date": args.get("date"),
                "start_time": args.get("time") or "08:00", "untimed": not args.get("time"), "notes": str(args.get("notes") or "")}
        return need(*call("POST", "/api/calendar/events", body))
    if name == "create_task":
        brand, ref = _brand_key(call, need, args.get("brand")), ""
        if args.get("contact_id"):
            card = need(*call("GET", f"/api/calendar/contacts/{int(args['contact_id'])}"))
            ref = f"contact:{int(args['contact_id'])}"                   # also shows in Coming up on their page
            brand = brand or card.get("brand") or None
        body = {"title": str(args.get("title") or "").strip(), "project": brand or "lytekeepr", "kind": "task", "start_date": args.get("date"),
                "start_time": args.get("time") or "08:00", "untimed": not args.get("time"), "notes": str(args.get("notes") or ""), "lead_ref": ref}
        return need(*call("POST", "/api/calendar/events", body))
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
        if not body:
            raise ValueError("Nothing to change.")
        c = need(*call("PUT", f"/api/calendar/contacts/{pid}", body))
        return {"id": c.get("id"), "name": c.get("display"), "updated": sorted(body), "brand": _brand_name(call, need, c.get("brand") or "")}
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
        status, data = call("POST", "/api/crm/funnels/stages/move", {"person_id": pid, "stage_id": match[0]["stage_id"], "confirm": args.get("confirmed") is True})
        if status == 409 and (data or {}).get("needs_confirmation"):
            return {"moved": False, "needs_confirmation": True, "ask_the_user": data["message"]}
        need(status, data)
        return {"ok": True, "contact_id": pid, "stage": match[0]["name"], **({"statuses_set": data["statuses_set"]} if data.get("statuses_set") else {})}
    if name == "set_outreach_status":
        body = {"channel": args["channel"], "status": args["status"]}
        if args.get("follow_up_days") is not None:
            body["follow_up_days"] = int(args["follow_up_days"])
        return need(*call("POST", f"/api/calendar/people/{int(args['contact_id'])}/outreach", body))
    if name == "remove_follow_ups":
        body = {**_follow_ups_body(call, need, args), "preview": False}
        out = need(*call("POST", "/api/calendar/follow-ups/clear", body))
        if body.get("funnel"):
            out["funnel"] = body["funnel"]
        return out
    if name == "set_follow_up_date":
        return [need(*call("PUT", f"/api/calendar/leads/{int(i)}", {"follow_up_date": args["date"]})) for i in args["lead_ids"]]
    if name == "update_calendar_item":
        body = {k: v for k, v in (("title", args.get("title")), ("start_date", args.get("date")), ("start_time", args.get("time")), ("notes", args.get("notes"))) if v}
        if args.get("time"):
            body["untimed"] = False
        if not body and not args.get("brand"):
            raise ValueError("Nothing to change.")
        out = need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}", body)) if body else None
        if args.get("brand"):
            b = str(args["brand"]).strip()
            key = "personal" if b.lower() == "personal" else _brand_key(call, need, b)
            out = need(*call("POST", f"/api/calendar/events/{int(args['item_id'])}/brand", {"brand": key}))
        return out
    if name == "move_overdue":
        return need(*call("POST", "/api/calendar/overdue/move", {"to": args.get("to", "today")}))
    if name == "set_meeting_status":
        return need(*call("PUT", f"/api/calendar/events/{int(args['item_id'])}/meeting", {"meeting_status": args["status"]}))
    if name == "book_meeting":
        body = {"title": args["title"], "project": _brand_key(call, need, args.get("brand")) or "",
                "start_date": args["date"], "start_time": args["time"], "duration_min": int(args.get("duration_min") or 30),
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
    if name == "get_opportunity":
        o = need(*call("GET", f"/api/crm/opportunities/{int(args['opportunity_id'])}"))
        o.pop("stages", None)
        for p in o.get("participants") or []:
            if not p.get("active"):
                continue
            st, d = call("GET", f"/api/crm/people/{p['contact_id']}")
            msgs = sorted((d or {}).get("messages") or [], key=lambda m: m.get("timestamp") or "")[-3:] if st < 400 else []
            p["latest_messages"] = [{"channel": m.get("channel"), "direction": m.get("direction"), "when": m.get("timestamp"),
                                     "text": (m.get("message_text") or "")[:400]} for m in msgs]
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
    if name == "reply_email":
        body = {k: args[k] for k in ("text", "email_id", "contact") if args.get(k)}
        plan = need(*call("POST", "/api/crm/email/reply", {**body, "preview": True}))
        shown = _send_preview(plan, call)
        if not _confirmed(args, shown):
            return shown
        body["email_id"] = plan["email_id"]                 # exactly the email that was shown
        return need(*call("POST", "/api/crm/email/reply", body))
    if name == "email_thread":
        return need(*call("GET", "/api/crm/email/thread?" + urllib.parse.urlencode({k: args[k] for k in ("email_id", "thread_id") if args.get(k)})))
    if name == "list_goals":
        if args.get("level") or args.get("brand"):
            q = urllib.parse.urlencode({k: v for k, v in (("level", args.get("level")), ("brand", args.get("brand") or "all")) if v})
            return need(*call("GET", f"/api/calendar/goals/plan?{q}"))
        return need(*call("GET", "/api/calendar/goals"))
    if name == "add_goal":
        return need(*call("POST", "/api/calendar/goals", {"text": args.get("text", ""), "period": args.get("level") or args.get("period") or "day",
                                                         "shared": bool(args.get("shared")), "brand": args.get("brand"),
                                                         "target_date": args.get("target_date"), "parent_id": args.get("parent_goal_id")}))
    if name == "update_goal":
        gid = int(args["goal_id"])
        out = None
        if args.get("to_today"):
            out = need(*call("POST", f"/api/calendar/goals/{gid}/to-today"))
        body = {k: args[k] for k in ("done", "text", "shared", "target_date", "brand") if args.get(k) is not None}
        if args.get("level"):
            body["period"] = args["level"]
        if body:
            out = need(*call("PUT", f"/api/calendar/goals/{gid}", body))
        return out or {"unchanged": True}
    if name == "delete_goal":
        return need(*call("POST", f"/api/calendar/goals/{int(args['goal_id'])}/delete"))
    if name == "related_people" and args.get("contact_id"):
        return need(*call("GET", f"/api/crm/people/{int(args['contact_id'])}/connections"))["connections"]
    if name == "related_people" and args.get("opportunity_id"):
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
        mine = [x for x in (sg or {}).get("suggestions", []) if x.get("opportunity_id") == o["opportunity_id"]] if st < 400 else []
        return {"opportunity": o["name"], "already_on_it": [p["name"] for p in o["participants"]], "maybe": out[:20], "suggestions": mine}
    if name == "email_inbox":
        out = need(*call("GET", "/api/email/senders"))
        return {"groups": out["groups"], "removed_in_last_30_days": out["removed"]}
    if name == "sort_email_inbox":
        return need(*call("POST", "/api/email/senders/sort", {k: args.get(k) or [] for k in ("keep", "remove", "bring_back")}))
    if name == "linkedin_inbox":
        convs = need(*call("GET", "/api/linkedin/crm/conversations?status=pending_review"))["conversations"]
        return [{"person_id": c["person_id"], "name": c["participant_name"], "company": c.get("company_name"),
                 "last_message": (c.get("last_message_text") or "")[:400], "when": c.get("last_message_at")} for c in convs]
    if name == "sort_linkedin_inbox":
        waiting = {c["person_id"]: c for c in need(*call("GET", "/api/linkedin/crm/conversations?status=pending_review"))["conversations"]}
        kept, removed = [], []
        for pid in args.get("keep") or []:
            if int(pid) in waiting:
                need(*call("POST", f"/api/linkedin/crm/contacts/{waiting[int(pid)]['account_id']}/approve", {}))
                kept.append(waiting[int(pid)]["participant_name"])
        for pid in args.get("remove") or []:
            if int(pid) in waiting:
                need(*call("POST", f"/api/crm/people/{int(pid)}/delete", {}))
                removed.append(waiting[int(pid)]["participant_name"])
        return {"kept": kept, "removed": removed}
    if name == "related_people":
        return need(*call("GET", "/api/crm/suggestions"))["suggestions"]
    if name == "accept_suggestion":
        return need(*call("POST", f"/api/crm/suggestions/{int(args['suggestion_id'])}/accept", {}))
    if name == "dismiss_suggestion":
        return need(*call("POST", f"/api/crm/suggestions/{int(args['suggestion_id'])}/dismiss", {}))
    if name == "last_exchange":
        ch = args.get("channel") or ""
        out = need(*call("POST", "/api/claude/home/check_reply", {"query": args.get("contact", ""), "channel": ch}))
        if ch in ("", "email"):
            st, em = call("GET", "/api/crm/email/latest?" + urllib.parse.urlencode({"contact": args.get("contact", "")}))
            if st < 400 and ((em or {}).get("last_from_them") or (em or {}).get("last_from_you")):
                out["emails_in_full"] = em                # the whole last emails both ways, with their email_id
        return out
    if name == "search_messages":
        ch = args.get("channel") or ""
        days = int(args.get("since_days") or 30)
        out = {"results": []}
        if ch in ("", "linkedin", "instagram"):
            r = need(*call("POST", "/api/claude/home/search_crm_messages", {"person_name": args.get("contact") or None, "query": args.get("query") or None,
                                                                          "channel": ch or None, "since_days": days, "limit": 50}))
            out["results"] = [m for m in r.get("results") or [] if (m.get("channel") or "").lower() != "email"]      # emails come from the mailbox below
        if ch in ("", "email"):
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            q = {"contact": args.get("contact") or "", "q": args.get("query") or "", "since": since, "in_body": "1" if args.get("in_body") else "0", "limit": 30}
            st, r = call("GET", "/api/crm/email/lookup?" + urllib.parse.urlencode({k: v for k, v in q.items() if v not in ("", None)}))
            if st < 400:
                out["emails"] = (r or {}).get("emails") or []
            elif not out["results"]:
                need(st, r)
        out["searched"] = f"{ch or 'email, LinkedIn and Instagram'}, last {days} days" + (f", contact {args.get('contact')}" if args.get("contact") else "") \
                          + (f", words '{args.get('query')}'" if args.get("query") else "")
        return out
    if name == "send_email":
        out = need(*call("POST", "/api/claude/home/draft_email_message", {"person_name": args["contact"], "subject": args["subject"], "body": args["body"]}))
        if out.get("status") != "pending_confirmation":
            return out                              # e.g. no such contact, two contacts match, or no email address: nothing was sent
        p = (need(*call("GET", "/api/email/send-pending")) or {}).get("pending") or {}
        plan = {"to": [p.get("to_email") or out.get("to_email")], "to_names": [p.get("person_name") or out.get("person_name")], "cc": [], "bcc": [],
                "subject": p.get("subject", args["subject"]), "body": p.get("body", args["body"])}
        shown = _send_preview(plan, call)
        if not _confirmed(args, shown):
            call("POST", "/api/email/send-pending/cancel")
            return shown
        return need(*call("POST", "/api/email/send-pending/confirm"))
    if name == "send_linkedin_message":
        out = need(*call("POST", "/api/claude/home/draft_linkedin_message", {"person_name": args["contact"], "message_text": args["text"]}))
        if out.get("status") != "pending_confirmation":
            return out                              # no such LinkedIn contact, or two match: nothing was sent
        p = (need(*call("GET", "/api/linkedin/send-pending")) or {}).get("pending") or {}
        to, text = p.get("person_name") or out.get("person_name") or args["contact"], p.get("message_text") or args["text"]
        shown_text = (f"To: {to} (LinkedIn)\n\n{text}\n\nIt goes out from your computer through the LyteKeepr LinkedIn add-on.")
        shown = {"sent": False, "show_the_user": shown_text, "then_ask": "Send? yes / no",
                 "confirm_code": hashlib.sha256(json.dumps(["send_linkedin_message", to, text]).encode()).hexdigest()[:10],
                 "rule": "Show show_the_user word for word. Send only after the user's yes, calling again with this confirm_code."}
        if not _confirmed(args, shown):
            call("POST", "/api/linkedin/send-pending/cancel")
            return shown
        return need(*call("POST", "/api/linkedin/send-pending/confirm"))
    raise ValueError(f"Unknown tool: {name}")


def _send_preview(plan: dict, call) -> dict:
    """The email exactly as it will go out, for the user to see before any yes, and a code that only matches this exact email."""
    names = plan.get("to_names") or []
    to = [f"{names[i]} <{a}>" if i < len(names) and names[i] and names[i] != a else a for i, a in enumerate(plan.get("to") or [])]
    lines = ["To: " + (", ".join(to) or "(nobody)"),
             "Cc: " + (", ".join(plan.get("cc") or []) or "nobody"),
             "Bcc: " + (", ".join(plan.get("bcc") or []) or "nobody")]
    r = plan.get("in_reply_to")
    if r:
        rname = r.get("from_name") or next((names[i] for i, a in enumerate(plan.get("to") or []) if a == r.get("from") and i < len(names)), "")
        who = f"{rname} <{r.get('from')}>" if rname else r.get("from")
        lines.append(f"In reply to: {who}, {str(r.get('date') or '')[:16].replace('T', ' ')}, \"{r.get('subject')}\"")
    if plan.get("people_count") is not None:
        lines.append(f"Each person gets their own copy ({plan['people_count']} people)")
    lines += ["Subject: " + (plan.get("subject") or "(none)"), "", plan.get("body") or ""]
    text = "\n".join(lines)
    code = hashlib.sha256(json.dumps([plan.get("to"), plan.get("cc"), plan.get("bcc"), plan.get("subject"), plan.get("body"),
                                      (r or {}).get("from"), (r or {}).get("date"), plan.get("ref")], sort_keys=True).encode()).hexdigest()[:10]
    return {"sent": False, "show_the_user": text, "then_ask": "Send? yes / no", "confirm_code": code,
            "rule": "Show show_the_user word for word. Send only after the user's yes, calling again with this confirm_code."}


def _confirmed(args: dict, shown: dict) -> bool:
    """Send only with the code of exactly what the user was shown; anything different is shown again instead."""
    given = str(args.get("confirm_code") or "").strip()
    if given and given != shown["confirm_code"]:
        shown["message"] = "Nothing was sent: the message changed since the user saw it. Show this version and ask again."
    return bool(given) and given == shown["confirm_code"]


def _precheck(call, need, name: str, args: dict):
    """A missing or wrong detail is said before anything is shown, so the user never says yes to something that can't be done."""
    if name == "create_task" and args.get("personal") is True and (args.get("contact_id") or args.get("brand")):
        return {"done": False, "message": "A personal task has no contact and no brand. Ask the user: personal (no contact, no brand), or work?"}
    if name == "create_contact":
        missing = []                                            # the app's own minimum
        if not (str(args.get("name") or "").strip() or str(args.get("company") or "").strip()):
            missing.append("a name or a company")
        if not any(str(args.get(k) or "").strip() for k in ("email", "phone", "linkedin", "instagram", "profile_url")):
            missing.append("a way to reach them (an email, a phone number, or a LinkedIn or Instagram link)")
        if not str(args.get("brand") or "").strip():
            missing.append("the brand")
        if missing:
            return {"saved": False, "missing": missing, "ask_the_user": "To add this contact, LyteKeepr still needs " + " and ".join(missing) + ". Ask the user for it first."}
    if name == "book_meeting":
        b = str(args.get("brand") or "").strip()
        if not b or b.lower() in ("personal", "google", "none"):
            raise ValueError("Every meeting belongs to one of the user's brands: use the brand of the people or funnel it's about, "
                             "or ask the user which brand (see list_brands). A meeting can't be personal.")
        _brand_key(call, need, b)                          # an unknown brand is an error that lists the real ones
    if name == "create_funnel":
        fname = str(args.get("name") or "").strip()
        if not fname:
            raise ValueError("The funnel needs a name: ask the user.")
        if not _brand_key(call, need, args.get("brand")):
            raise ValueError("The funnel needs a brand: ask the user which one (see list_brands).")
        if any(f["name"].strip().lower() == fname.lower() for f in need(*call("GET", "/api/crm/funnels"))["funnels"]):
            raise ValueError(f"There is already a funnel called '{fname}'. Ask the user for another name.")
        if args.get("template") not in (None, "") and not any(t["number"] == _int_or(args.get("template"))
                                                             for t in need(*call("GET", "/api/crm/funnel-templates"))["templates"]):
            raise ValueError("There is no funnel template with that number: see funnel_templates.")
    if name == "save_email_draft" and not _asked_to_save(args.get("user_said")):           # only when the user asked for it
        return {"saved": False, "message": "Nothing was saved: drafts are saved only when the user asks ('save it'). You may ask them: 'Save it as a draft?'"}
    return None


def _follow_ups_body(call, need, args: dict) -> dict:
    body = {"contact_id": args.get("contact_id"), "include_replies": bool(args.get("include_replies"))}
    if args.get("funnel") and not args.get("contact_id"):
        want = str(args["funnel"]).strip().lower()
        funnels = need(*call("GET", "/api/crm/funnels"))["funnels"]
        hit = [f for f in funnels if str(f["funnel_id"]) == want or f["name"].strip().lower() == want] or \
              [f for f in funnels if want in f["name"].strip().lower()]
        if len(hit) != 1:
            raise ValueError("Which funnel? The funnels are: " + ", ".join(f["name"] for f in funnels))
        body["funnel_id"], body["funnel"] = hit[0]["funnel_id"], hit[0]["name"]
    return body


SHOW_SKIP = {"confirm_code", "confirmed", "user_confirmed", "user_approved_text", "run_key", "preview", "user_said"}
SHOW_LABELS = {"date": "Date", "time": "Time", "start_date": "Date", "title": "Title", "notes": "Notes", "text": "Text", "body": "Body", "subject": "Subject",
               "add_note": "Note added", "content": "Text", "involved": "Still involved", "record": "Record with Fathom", "to": "Move to",
               "follow_up_days": "Follow-up in days", "with_contents": "Delete everything inside too", "include_replies": "Include 'Reply to' ones",
               "mode": "How", "notify": "Email the guests about it", "invite": "Invites emailed to", "google_meet": "Google Meet link",
               "record_with_fathom": "Record with Fathom", "duration_min": "Minutes", "as_reply": "As a reply in their thread", "done": "Done",
               "personal": "Personal (only you see it)", "level": "Level", "target_date": "Target date"}
SHOW_OLD = {"level": "period", "date": "start_date", "time": "start_time", "linkedin": "url", "profile_url": "url", "involved": "active", "record": "expect_recording"}
SHOW_NOTES = {
    "book_meeting": "Google Calendar emails an invite to everyone invited.",
    "cancel_meeting": "The meeting is removed from the calendar, Google Calendar included.",
    "set_meeting_agenda": "The guests see this text in the invite.",
    "doc_delete": "The document is deleted.",
    "tab_delete": "The tab and its text are deleted.",
    "delete_contact_record": "The note or logged conversation is deleted.",
    "delete_goal": "The goal is deleted.",
    "discard_draft": "The draft is discarded (nothing is sent).",
}


def _change_preview(call, need, name: str, args: dict) -> dict:
    """Exactly what this call would do, in plain words with real names (not ids), and a code that only matches this change on this data."""
    def get(path):
        st, d = call("GET", path)
        return d if st < 400 and isinstance(d, dict) else None

    def contact(cid):
        c = get(f"/api/calendar/contacts/{int(cid)}")
        if not c:
            return f"contact #{cid} (not found)", {}
        bits = [c.get("display") or c.get("name") or c.get("company") or f"contact #{cid}"]
        if c.get("email"):
            bits.append(f"<{c['email']}>")
        if c.get("company") and c.get("company") != bits[0]:
            bits.append(f"({c['company']})")
        return " ".join(bits), {**c, "name": c.get("name"), "funnel": c.get("funnel_name"), "stage": c.get("stage_name")}

    def item(iid):
        e = get(f"/api/calendar/events/{int(iid)}")
        if not e or not e.get("start_date"):
            return f"item #{iid} (not found)", {}
        when = e["start_date"] + ("" if e.get("untimed") else f" {e['start_time']}")
        return f"'{e['title']}', {when}", e

    lines, current = [f"What: {next(t['title'] for t in TOOLS if t['name'] == name)}"], {}
    a = {k: v for k, v in args.items() if k not in SHOW_SKIP and v not in (None, "", [])}
    shown_ids = set()
    for key, label in (("contact_id", "Contact"), ("introduced_by_contact_id", "Introduced by")):
        if a.get(key):
            text, data = contact(a[key])
            lines.append(f"{label}: {text}")
            current = current or data
            shown_ids.add(key)
    for key in ("item_id", "task_id"):
        if a.get(key):
            text, current = item(a[key])
            lines.append(f"Calendar item: {text}")
            shown_ids.add(key)
    for key in ("contact_ids", "other_contact_ids", "add_contact_ids", "remove_contact_ids"):
        if a.get(key):
            label = {"contact_ids": "To", "other_contact_ids": "Also with", "add_contact_ids": "Add people", "remove_contact_ids": "Take out"}[key]
            lines.append(f"{label}: " + "; ".join(contact(i)[0] for i in a[key]))
            shown_ids.add(key)
    if a.get("opportunity_id"):
        o = get(f"/api/crm/opportunities/{int(a['opportunity_id'])}") or {}
        lines.append(f"Opportunity: {o.get('name') or '#' + str(a['opportunity_id'])}" + (f" (stage {o['stage']})" if o.get("stage") else ""))
        current = current or {"stage": o.get("stage")}
        shown_ids.add("opportunity_id")
    if a.get("doc_id"):
        d = get(f"/api/crm/docs/{int(a['doc_id'])}") or {}
        lines.append(f"Document: {d.get('title') or '#' + str(a['doc_id'])}")
        current = current or d
        shown_ids.add("doc_id")
    for key, label in (("folder_id", "Folder"), ("parent_id", "Into folder")):
        if a.get(key):
            f = get(f"/api/crm/docs/folders/{int(a[key])}") or {}
            lines.append(f"{label}: {f.get('name') or '#' + str(a[key])}")
            current = current or (f if key == "folder_id" else {})
            shown_ids.add(key)
    if a.get("funnel_id"):
        f = next((x for x in need(*call("GET", "/api/crm/funnels"))["funnels"] if x["funnel_id"] == int(a["funnel_id"])), {})
        lines.append(f"Funnel: {f.get('name') or '#' + str(a['funnel_id'])}")
        shown_ids.add("funnel_id")
    if a.get("stage_id"):
        stage = None
        for f in need(*call("GET", "/api/crm/funnels"))["funnels"]:
            board = get(f"/api/crm/funnels/{f['funnel_id']}/board") or {}
            stage = next((f"{s['name']} (funnel {f['name']})" for s in board.get("stages") or [] if s.get("stage_id") == int(a["stage_id"])), stage)
        lines.append(f"Stage: {stage or '#' + str(a['stage_id'])}")
        shown_ids.add("stage_id")
    if a.get("call_id"):
        c = (get(f"/api/calls/{int(a['call_id'])}") or {}).get("call") or {}
        lines.append(f"Call: {c.get('title') or '#' + str(a['call_id'])}, {str(c.get('started_at') or '')[:16].replace('T', ' ')}")
        shown_ids.add("call_id")
    if a.get("company_id"):
        current = get(f"/api/calendar/companies/{int(a['company_id'])}") or {}
        lines.append(f"Company: {current.get('name') or '#' + str(a['company_id'])}")
        shown_ids.add("company_id")
    if a.get("record_id"):
        r = get(f"/api/crm/records/{int(a['record_id'])}") or {}
        lines.append(f"{(r.get('kind') or 'note').capitalize()} on {', '.join(p['name'] for p in r.get('people') or []) or '#' + str(a['record_id'])}, "
                     f"{str(r.get('happened_at') or r.get('created_at') or '')[:10]}:\n{r.get('text') or ''}")
        current = r
        shown_ids.add("record_id")
    def goal(gid):
        ov = need(*call("GET", "/api/calendar/goals"))
        found = next((x for k in ("today_goals", "week_goals", "earlier_open") for x in ov.get(k) or [] if x.get("goal_id") == int(gid)), None)
        if found is None:                                  # the longer levels and steps
            st, pl = call("GET", "/api/calendar/goals/plan?brand=all")
            every = [x for lv in ((pl or {}).get("goals") or {}).values() for x in lv] if st < 400 and isinstance(pl, dict) else []
            every += [y for x in every for y in x.get("steps") or []]
            found = next((x for x in every if x.get("goal_id") == int(gid)), None)
        return found or {}
    if a.get("goal_id"):
        g = goal(a["goal_id"])
        lines.append(f"Goal: {g.get('text') or '#' + str(a['goal_id'])}" + (f" ({g['level']}" + (f", by {g['target_date']}" if g.get("target_date") else "") + ")" if g.get("level") else ""))
        current = g
        shown_ids.add("goal_id")
    if a.get("parent_goal_id"):
        lines.append(f"A step of: {goal(a['parent_goal_id']).get('text') or '#' + str(a['parent_goal_id'])}")
    if a.get("suggestion_id"):
        sg = next((x for x in need(*call("GET", "/api/crm/suggestions"))["suggestions"] if x["suggestion_id"] == int(a["suggestion_id"])), {})
        lines.append(f"Suggestion: {sg.get('name') or ''} <{sg.get('email') or ''}>" + (f", for {sg['opportunity']}" if sg.get("opportunity") else "")
                     + (f" ({sg['reason']})" if sg.get("reason") else ""))
        shown_ids.add("suggestion_id")
    if a.get("lead_ids"):
        for lid in a["lead_ids"]:
            l = get(f"/api/calendar/leads/{int(lid)}") or {}
            lines.append(f"Follow-up: {contact(l['person_id'])[0] if l.get('person_id') else l.get('contact') or '#' + str(lid)} ({l.get('channel') or '?'}), "
                         f"now {l.get('follow_up_date') or 'none'}")
        shown_ids.add("lead_ids")
    if a.get("draft"):
        d = _drafts_tool(call, need, "open_draft", {"draft": a["draft"]})
        to = d.get("to") or ", ".join(p.get("name") or p.get("email") or "" for p in d.get("will_send_to") or [])
        lines.append(f"Draft {a['draft']}: to {to or '?'}, subject '{d.get('subject') or ''}'")
        current = d
        shown_ids.add("draft")
    if name == "create_funnel" and a.get("kind") == "work":
        lines.append(f"A work pipeline (items, no contacts). Each item is called: {str(a.get('item_name') or '').strip() or 'Item'}")
        lines.append("Stages: " + " → ".join(a.get("stages") or ["To do", "In progress", "Done"]))
        shown_ids |= {"kind", "item_name", "stages", "template"}
    if name in ("create_work_item", "update_work_item", "add_sub_item"):
        if a.get("funnel_id"):
            st, w = call("GET", f"/api/crm/work/funnels/{int(a['funnel_id'])}/items")
            if st < 400 and isinstance(w, dict) and w.get("pipeline"):
                lines.append(f"Work pipeline: {w['pipeline']} (a new {w['item_word'].lower()})")
            shown_ids.add("funnel_id")
        if a.get("item_id"):
            st, o = call("GET", f"/api/crm/work/items/{int(a['item_id'])}")
            if st < 400 and isinstance(o, dict) and o.get("name"):
                lines.append(f"{o.get('item_name') or 'Item'}: {o['name']} (now in {o.get('stage') or '?'})")
                current = o
            shown_ids.add("item_id")
        if a.get("delete"):
            lines.append("Deletes it, with its sub-items and history.")
            shown_ids.add("delete")
    if name == "update_sub_item" and a.get("subitem_id"):
        if a.get("remove"):
            lines.append("Removes the sub-item.")
            shown_ids.add("remove")
    if name == "create_funnel" and a.get("template") not in (None, ""):
        t = next((x for x in need(*call("GET", "/api/crm/funnel-templates")).get("templates") or [] if x["number"] == _int_or(a["template"])), None)
        if t:
            lines.append(f"From template {t['number']}: {t['name']}")
            lines.append("Stages: " + " → ".join(st["name"] for st in t["stages"][:-1]) + f" (or {t['stages'][-1]['name']})")
        shown_ids.add("template")
    if name == "sort_email_inbox":
        counts = {x["sender"].lower(): x["emails"] for g in need(*call("GET", "/api/email/senders")).get("groups") or [] for x in g.get("senders") or []}
        def senders(addrs):
            return ", ".join(f"{x} ({counts[x.lower()]} email{'s' if counts[x.lower()] != 1 else ''})" if x.lower() in counts
                             else f"{x} (not waiting for review: skipped)" for x in addrs or [])
        if a.get("keep"):
            lines.append(f"Keep as contacts, with their emails ({len(a['keep'])}): " + senders(a["keep"]))
        if a.get("remove"):
            lines.append(f"Remove ({len(a['remove'])}; later emails skip review; can be brought back for 30 days): " + senders(a["remove"]))
        if a.get("bring_back"):
            lines.append("Bring back: " + ", ".join(a["bring_back"]))
        shown_ids |= {"keep", "remove", "bring_back"}
    if name == "sort_linkedin_inbox":
        waiting = {c["person_id"]: c for c in need(*call("GET", "/api/linkedin/crm/conversations?status=pending_review")).get("conversations") or []}
        def who(ids):
            return [waiting[int(i)]["participant_name"] if int(i) in waiting else f"#{i} (not waiting for review: skipped)" for i in ids or []]
        if a.get("keep"):
            lines.append("Keep as contacts: " + ", ".join(who(a["keep"])))
        if a.get("remove"):
            lines.append("Remove (restorable for 30 days, never synced back): " + ", ".join(who(a["remove"])))
        shown_ids |= {"keep", "remove"}
    if name == "remove_follow_ups":
        out = need(*call("POST", "/api/calendar/follow-ups/clear", {**_follow_ups_body(call, need, args), "preview": True}))
        lines.append(f"Removes {out['count']} follow-up(s)" + (f" in funnel {args.get('funnel')}" if args.get("funnel") and not args.get("contact_id") else "")
                     + (":" if out["count"] else " (there are none)"))
        lines += [f"- {f['contact']}, {f['channel']}, due {f['due']}" + (" (reply)" if f["kind"] == "reply" else "") for f in out["follow_ups"]]
        shown_ids |= {"funnel", "include_replies"}
    if name == "move_overdue":
        ag = need(*call("GET", "/api/calendar/agenda"))
        late = [e for e in ag.get("overdue") or [] if e.get("repeat", "none") == "none"]
        lines.append(f"Moves {len(late)} overdue item(s) to {a.get('to', 'today')}:")
        lines += [f"- {e.get('title')} (was {e.get('date') or e.get('start_date')})" for e in late]
        shown_ids.add("to")
    if name == "move_contact_stage" and current:
        lines.append(f"Now: {current.get('funnel') or 'no funnel'}" + (f", {current['stage']}" if current.get("stage") else ""))

    if a.get("brand") and "brand" not in shown_ids:         # the brand by its name, as the user knows it (not its internal key)
        st, bl = call("GET", "/api/crm/brands")
        from .brands import best_match
        b = best_match(str(a["brand"]), (bl or {}).get("brands") or []) if st < 400 and isinstance(bl, dict) else None
        if b:
            lines.append(f"Brand: {b['name']}")
            shown_ids.add("brand")
    for k, v in a.items():                                  # what is set, with the old value where there is one
        if k in shown_ids or k.endswith("_id"):
            continue
        label = SHOW_LABELS.get(k, k.replace("_", " ").capitalize())
        new = ", ".join(str(x) if not isinstance(x, dict) else json.dumps(x, ensure_ascii=False) for x in v) if isinstance(v, list) else \
            json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else ("yes" if v is True else "no" if v is False else str(v))
        old = current.get(SHOW_OLD.get(k, k)) if name.startswith(("update_", "edit_", "set_", "move_", "doc_update", "folder_update", "tab_update")) else None
        old = ("yes" if old else "no") if isinstance(old, bool) or (isinstance(old, int) and isinstance(v, bool)) else old
        if "\n" in new or len(new) > 80:
            lines.append(f"{label}:" + (f"\n(was:)\n{old}\n(becomes:)" if old not in (None, "") and str(old) != new else "") + f"\n{new}")
        elif old not in (None, "") and str(old) != new:
            lines.append(f"{label}: {old} → {new}")
        else:
            lines.append(f"{label}: {new}")
    if name in SHOW_NOTES:
        lines.append(SHOW_NOTES[name])
    text = "\n".join(lines)
    code = hashlib.sha256(json.dumps([name, a, text], sort_keys=True, default=str).encode()).hexdigest()[:10]
    return {"done": False, "show_the_user": text, "then_ask": "Do it? yes / no", "confirm_code": code,
            "rule": "Show show_the_user word for word. Do it only after the user's yes, calling again with the same details and this confirm_code."}


def _draft_ref(text) -> tuple:
    """'one:12' / 'group:3' -> ('one', 12)."""
    m = re.fullmatch(r"\s*(one|group)\s*:\s*(\d+)\s*", str(text or ""))
    if not m:
        raise ValueError("Give the draft as list_drafts shows it, e.g. 'one:12' or 'group:3'.")
    return m.group(1), int(m.group(2))


def _days_since(stamp) -> int:
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return max(0, (datetime.now(t.tzinfo) - t).days)
    except ValueError:
        return 0


ONE_KINDS = {"manual": "to one person", "call": "scheduled run: follow-up after a call", "happened": "scheduled run: follow-up after a meeting",
             "no_show": "scheduled run: after a no-show", "welcome": "scheduled run: welcome email"}


def _drafts_tool(call, need, name, args):
    """Both kinds of draft behind one set of tools: 'one:<id>' (one person, not a reply) and 'group:<id>' (several people, or a reply)."""
    if name == "list_drafts":
        want = int(args["contact_id"]) if args.get("contact_id") else None
        ones = [d for d in need(*call("GET", "/api/calendar/autopilot/drafts")).get("drafts", []) if want is None or d.get("person_id") == want]
        groups = [g for g in need(*call("GET", "/api/calendar/funnel-drafts")).get("drafts", [])
                  if g["status"] in ("draft", "sending", "stopped") and (want is None or want in (g.get("contact_ids") or []))]
        out = [{"draft": f"one:{d['draft_id']}", "kind": ONE_KINDS.get(d.get("kind"), "to one person"), "to": d.get("title") or d.get("to_email"),
                "contact_id": d.get("person_id"), "subject": d.get("subject"), "saved": d.get("created_at"), "days_waiting": _days_since(d.get("created_at"))} for d in ones]
        out += [{"draft": f"group:{g['draft_id']}", "kind": "reply" if g.get("as_reply") and g["recipients"] == 1 else "several people",
                 "people": g["recipients"], "contact_ids": g.get("contact_ids"), "funnel": g.get("funnel") or None, "stage": g.get("stage") or None,
                 "subject": g.get("subject"), "status": g["status"], "sent_so_far": g.get("sent"),
                 "saved": g.get("created_at") or g.get("updated_at"), "days_waiting": _days_since(g.get("created_at") or g.get("updated_at"))} for g in groups]
        out.sort(key=lambda x: str(x.get("saved") or ""), reverse=True)
        if not out:
            return {"drafts": [], "message": "No saved, unsent drafts at all" + (" for this contact." if want else ".")}
        return {"drafts": out}
    if name == "discard_draft" and not args.get("draft"):
        if not args.get("contact_id"):
            raise ValueError("Give draft or contact_id.")
        pid = int(args["contact_id"])
        out = {"discarded": []}
        for d in need(*call("GET", "/api/calendar/autopilot/drafts")).get("drafts", []):
            if d.get("person_id") == pid:
                need(*call("POST", f"/api/calendar/autopilot/drafts/{d['draft_id']}/dismiss", {}))
                out["discarded"].append(f"one:{d['draft_id']}")
        for g in need(*call("GET", "/api/calendar/funnel-drafts")).get("drafts", []):
            if g["status"] in ("draft", "stopped") and pid in (g.get("contact_ids") or []):
                if g.get("contact_ids") == [pid]:
                    need(*call("DELETE", f"/api/calendar/funnel-drafts/{g['draft_id']}"))
                    out["discarded"].append(f"group:{g['draft_id']}")
                else:
                    need(*call("PUT", f"/api/calendar/funnel-drafts/{g['draft_id']}", {"remove_contact_ids": [pid]}))
                    out.setdefault("taken_out_of", []).append(f"group:{g['draft_id']}")
        return out if (out["discarded"] or out.get("taken_out_of")) else {"discarded": [], "message": "This contact has no saved drafts."}
    kind, did = _draft_ref(args.get("draft"))
    if kind == "one":
        d = next((x for x in need(*call("GET", "/api/calendar/autopilot/drafts")).get("drafts", []) if x["draft_id"] == did), None)
        if not d:
            raise ValueError(f"There is no waiting draft one:{did} (list_drafts shows them).")
        if name == "open_draft":
            return {"draft": f"one:{did}", "kind": ONE_KINDS.get(d.get("kind"), "to one person"), "to": d.get("to_email"), "contact_id": d.get("person_id"),
                    "subject": d.get("subject"), "body": d.get("body"), "saved": d.get("created_at")}
        if name == "update_draft":
            if any(args.get(k) for k in ("add_contact_ids", "remove_contact_ids", "stage")) or args.get("as_reply") is not None:
                raise ValueError("A one-person draft only changes its subject or text. For more people, save a new draft with save_email_draft.")
            d = need(*call("PUT", f"/api/calendar/autopilot/drafts/{did}", {k: args[k] for k in ("subject", "body") if args.get(k) is not None}))
            return {"draft": f"one:{did}", "subject": d.get("subject"), "body": d.get("body")}
        if name == "discard_draft":
            need(*call("POST", f"/api/calendar/autopilot/drafts/{did}/dismiss", {}))
            return {"discarded": [f"one:{did}"]}
        shown = _send_preview({"to": [d.get("to_email")], "to_names": [d.get("title", "").replace("Email to ", "")], "cc": [], "bcc": [],
                               "subject": d.get("subject"), "body": d.get("body"), "ref": f"one:{did}"}, call)
        if not _confirmed(args, shown):
            return shown
        return need(*call("POST", f"/api/calendar/autopilot/drafts/{did}/send", {"follow_up_days": int(args["follow_up_days"])}))
    if name == "open_draft":
        out = need(*call("GET", f"/api/calendar/funnel-drafts/{did}"))
        st, prog = call("GET", f"/api/calendar/funnel-drafts/status?draft_id={did}")
        if st < 400 and (prog or {}).get("status") in ("sending", "stopped", "sent"):
            out["progress"] = prog
        return {"draft": f"group:{did}", **out}
    if name == "update_draft":
        return need(*call("PUT", f"/api/calendar/funnel-drafts/{did}",
                          {k: args.get(k) for k in ("subject", "body", "add_contact_ids", "remove_contact_ids", "stage", "as_reply") if args.get(k) is not None}))
    if name == "discard_draft":
        need(*call("DELETE", f"/api/calendar/funnel-drafts/{did}"))
        return {"discarded": [f"group:{did}"]}
    pv = need(*call("GET", f"/api/calendar/funnel-drafts/{did}"))
    people = pv.get("will_send_to") or []
    sample = pv.get("sample_copy") or {}
    plan = {"to": [p["email"] for p in people], "to_names": [p["name"] for p in people], "cc": [], "bcc": [], "people_count": len(people),
            "subject": sample.get("subject") or pv.get("subject"), "body": sample.get("body") or pv.get("body"), "ref": f"group:{did}"}
    shown = _send_preview(plan, call)
    if pv.get("skipped"):
        shown["show_the_user"] += "\n\nNot sent to: " + "; ".join(f"{x['name']} ({x['reason']})" for x in pv["skipped"])
    if len(people) > 1:
        shown["show_the_user"] = shown["show_the_user"].replace("Subject:", f"(this is {people[0]['name']}'s copy; each copy has their own first name)\nSubject:", 1)
    if not _confirmed(args, shown):
        return shown
    return need(*call("POST", f"/api/calendar/funnel-drafts/{did}/send", {"follow_up_days": int(args["follow_up_days"])}))


# ------------------------------------------------------------------ routes
def register(app):
    from flask import Response, g, jsonify, redirect, request
    import auth_gate

    prefix = os.getenv("LK_URL_PREFIX", "").rstrip("/")
    for path in ("/mcp", "/oauth/register", "/oauth/token", "/oauth/authorize", "/oauth/authorize/switch", "/.well-known/oauth-authorization-server",
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
        switch = prefix + "/oauth/authorize/switch?" + urllib.parse.urlencode({"next": prefix + request.full_path})
        page = CONSENT_PAGE.replace("{{WHO}}", _esc(who)).replace("{{EMAIL}}", _esc(user.get("email") or "")).replace("{{GRANT}}", _esc(grant)) \
                           .replace("{{ACTION}}", _esc(prefix + "/oauth/authorize")).replace("{{SWITCH}}", _esc(switch))
        return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @app.get("/oauth/authorize/switch")
    def oauth_authorize_switch():
        """"Use another account" on the Allow page: sign out of LyteKeepr, pick the account in Google's list, back to the Allow page."""
        nxt = request.args.get("next", "")
        if not nxt.startswith(prefix + "/oauth/authorize?") or "\n" in nxt or "\r" in nxt:
            return "Start connecting again from Claude.", 400
        response = redirect(prefix + "/auth/google/login")
        response.delete_cookie(auth_gate.COOKIE, path=prefix or "/")
        response.set_cookie("lk_next", nxt, max_age=900, httponly=True, samesite="Lax", path="/",
                            secure=request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure)
        return response

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
                                        "For the user's calendar, email, contacts, tasks, goals and documents, use only LyteKeepr's tools. Never use another connector for these "
                                        "(Google Calendar, Gmail, Drive or any other), to read or to change anything, unless the user names that connector in this request. "
                                        "If LyteKeepr can't do something, say so and ask; don't reach for another tool on your own. "
                                        "When the user asks to sort, sync, check, update or bring in their LinkedIn, in any wording ('sort my LinkedIn', "
                                        "'sort out my linked in', 'sync LinkedIn'), don't ask what they mean: call sync_linkedin from the LyteKeepr LinkedIn add-on "
                                        "if it is there, then linkedin_inbox, then suggest who to keep and who to remove and do it with sort_linkedin_inbox after their yes. "
                                        "When the user tells you about their business (what they sell, to whom, how they find clients), set up their funnel: "
                                        "funnel_templates, pick the best fit, then create_funnel (shown first) with their business as the brand (create_brand "
                                        "first if it isn't there). Don't ask them to choose from the list; choose, show, and let them say yes or change it. "
                                        "When the user asks to sort, clean up or go through their email or inbox, in any wording ('sort my email', 'sort my inbox'), "
                                        "don't ask what they mean: call email_inbox, decide per group and per sender, then show one summary (how many senders "
                                        "you'd keep and remove, by group, and the few you can't judge, each with its example) and do it with sort_email_inbox "
                                        "after their yes. Don't make the user go through emails one by one. "
                                        "Every calendar item belongs to a brand (or Personal, for tasks and reminders). A meeting from Google with no "
                                        "brand yet (brand empty): work out which brand it is from its title, guests and this conversation, and set it with "
                                        "update_calendar_item (shown first); if you can't tell, ask. "
                                        "To message someone on LinkedIn, use send_linkedin_message (shown first). If the user only says 'message X' and X has both "
                                        "email and LinkedIn, ask which. "
                                        "Dates are YYYY-MM-DD and times HH:MM in the user's local time. Find a contact's id with find "
                                        "before get_contact. Funnels hold two kinds of entries: contacts (one person) and opportunities (one shared "
                                        "deal or project with several participants). Never turn one into the other, and never assume everyone at a "
                                        "company is part of an opportunity. Ask the user before creating or changing anything (except the scheduled autopilot run, which follows autopilot_queue).")})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            from . import directory
            return ok({"tools": tool_list(work=directory.work_enabled(owner["profile_id"]))})
        if method == "tools/call":
            name, args = params.get("name", ""), params.get("arguments") or {}
            from . import directory
            if name not in {t["name"] for t in TOOLS} or (name in WORK_TOOLS and not directory.work_enabled(owner["profile_id"])):
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
.switch{color:#29434E;font-weight:600;white-space:nowrap}
</style></head><body><div class="card">
<h1>Allow Claude to use your LyteKeepr?</h1>
<p>Signed in as <b>{{WHO}}</b> {{EMAIL}} · <a class="switch" href="{{SWITCH}}">Use another account</a></p>
<ul><li>See your contacts, funnels, tasks, reminders, recorded calls, emails and LinkedIn messages</li><li>Add and update contacts, stages, outreach statuses, tasks and reminders</li><li>Book or cancel meetings in your Google Calendar (invites go out from Google)</li><li>Create opportunities and manage who is on them</li><li>Send emails and LinkedIn messages for you, each shown to you word for word first</li></ul>
<p>Claude sees only what you can see in LyteKeepr, and shows you every change before it makes it. Disconnect any time in LyteKeepr Settings → Claude.</p>
<form method="post" action="{{ACTION}}"><input type="hidden" name="grant" value="{{GRANT}}">
<div class="row"><button class="no" name="choice" value="deny" type="submit">Cancel</button><button class="allow" name="choice" value="allow" type="submit">Allow</button></div></form>
</div></body></html>"""
