"""Chat assistant for the calendar ("Ask LyteKeepr" tab).

Deliberately separate from assistant/: it reuses only the OpenAI wrapper
(services.openai.client) and the model/key config, and brings its own
system prompt and tools, so it can't disturb the existing chat. Tools call
lk_calendar.store / lk_calendar.leads — the same functions the UI uses.

Reads and edits apply immediately and are reported. Deleting an event is
staged and only happens when the user presses Confirm in the UI.
"""

import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import date as _date
from assistant.config import AssistantConfig
from services.openai.client import OpenAIServiceError, generate_reply

from . import google_sync, leads, store

MAX_MESSAGES = 40
MAX_CHARS = 20000
CHAT_PER_HOUR = int(os.getenv("LK_CHAT_PER_HOUR", "60"))
_sent: deque = deque()
_sent_lock = threading.Lock()
_pending: dict[str, dict] = {}   # confirmation id -> staged action (single user, in memory)

PROJECT_ENUM = [p for p in store.PROJECTS if p != 'google']   # 'google' rows only come from the Google sync
_DATE = {"type": "string", "description": "Date as YYYY-MM-DD (Montreal time)."}
_TIME = {"type": "string", "description": "24-hour time HH:MM, e.g. 14:30."}


def _tool(name, description, properties, required=()):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


TOOLS = [
    _tool("get_agenda", "What needs doing: overdue items, today's items and the next 7 days.", {}),
    _tool("list_events", "List calendar items between two dates (inclusive, max 62 days apart), repeats expanded.",
          {"start_date": _DATE, "end_date": _DATE}, ["start_date", "end_date"]),
    _tool("create_event",
          "Create a task, meeting or reminder. Use repeat 'daily', 'weekdays' or 'weekly' (with repeat_days) for recurring items.",
          {"title": {"type": "string"},
           "project": {"type": "string", "enum": PROJECT_ENUM,
                       "description": "lytekeepr, aspan (Aspan CRM) or nexy. Default lytekeepr if not stated."},
           "kind": {"type": "string", "enum": list(store.KINDS)},
           "date": _DATE, "time": _TIME,
           "no_specific_time": {"type": "boolean", "description": "True for to-dos that belong to the day but have no clock time (shown in the top strip). Time is then ignored."},
           "duration_min": {"type": "integer", "description": "Length in minutes (default 30 task, 60 meeting, 15 reminder)."},
           "repeat": {"type": "string", "enum": list(store.REPEATS)},
           "repeat_days": {"type": "array", "items": {"type": "integer"}, "description": "For weekly: 0=Mon … 6=Sun."},
           "notes": {"type": "string"}},
          ["title", "date", "time"]),
    _tool("update_event",
          "Change an existing item (get its id from list_events/get_agenda). Only pass fields to change. "
          "For a repeating item this changes the whole series; date is the series start.",
          {"event_id": {"type": "integer"}, "title": {"type": "string"},
           "project": {"type": "string", "enum": PROJECT_ENUM}, "kind": {"type": "string", "enum": list(store.KINDS)},
           "date": _DATE, "time": _TIME, "duration_min": {"type": "integer"},
           "repeat": {"type": "string", "enum": list(store.REPEATS)},
           "repeat_days": {"type": "array", "items": {"type": "integer"}}, "notes": {"type": "string"}},
          ["event_id"]),
    _tool("move_overdue", "Move ALL overdue items (past-due one-off tasks and follow-ups) to today or tomorrow, keeping their times.",
          {"to": {"type": "string", "enum": ["today", "tomorrow"]}}, ["to"]),
    _tool("sync_google_calendar", "Pull the user's primary Google Calendar (timed events only) into the calendar. Read-only; never changes their own notes or statuses.", {}),
    _tool("update_meeting",
          "Set the user's own meeting status and/or notes on an event (works on read-only Google events too). Statuses: scheduled, completed, no show, cancelled. "
          "Marking a conference call completed automatically adds a follow-up reminder 24 hours after it ended.",
          {"event_id": {"type": "integer"}, "meeting_status": {"type": "string"}, "my_notes": {"type": "string", "description": "Replaces the user's notes on that event."}},
          ["event_id"]),
    _tool("delete_event",
          "Ask to delete an item (and all its repeats). This does NOT delete: it shows the user a Confirm button.",
          {"event_id": {"type": "integer"}}, ["event_id"]),
    _tool("propose_complete_tasks",
          "Ask the user to confirm marking calendar TASKS as done (use when they say they finished / are done with something, e.g. \"I'm done with the Instagram task\"). "
          "First look at the tasks (get_agenda or list_events), match the user's words to the exact tasks, then call this with their ids and dates. "
          "It does NOT complete anything: it shows the user a Confirm button naming the exact tasks, and nothing changes until they press it. It never changes lead statuses.",
          {"understood": {"type": "string", "description": "One plain sentence: how you understood the request."},
           "items": {"type": "array", "description": "The tasks to complete.",
                     "items": {"type": "object", "properties": {"event_id": {"type": "integer"}, "date": _DATE}, "required": ["event_id", "date"]}}},
          ["understood", "items"]),
    _tool("list_leads",
          "Look up or list outreach leads and their status. To check specific leads (e.g. what some pasted profile links are marked as) "
          "pass them in `contacts`: names, @handles or full profile links (tracking bits like ?isSelfProfile=false are ignored). "
          "Without `contacts` it lists leads, filtered by channel / status / words in the name, note or link. "
          "Each result has status, status date, follow-up date, link, note and its calendar reminder.",
          {"contacts": {"type": "array", "items": {"type": "string"}, "description": "Names, @handles or profile links to look up."},
           "channel": {"type": "string", "enum": list(leads.CHANNELS)},
           "status": {"type": "string", "description": "A status label as the user says it, e.g. 'dm sent', 'connection accepted', or a custom one."},
           "query": {"type": "string", "description": "Words in the name, note or link (e.g. a business type or country)."},
           "updated_on": {"type": "string", "description": "Only leads whose status was set on this day: YYYY-MM-DD or 'today'."},
           "limit": {"type": "integer", "description": "Max rows (default 25, up to 300)."}}),
    _tool("undo_last_lead_change",
          "Undo the most recent batch of lead changes (statuses or follow-up dates: whatever the last tool call or click changed), "
          "restoring each lead's previous status/date and its calendar reminder.", {}),
    _tool("lead_summary",
          "Overview of the lead lists: for each channel (linkedin, instagram) how many leads in total and how many in each status.",
          {}),
    _tool("list_statuses", "The statuses that exist for each channel, including custom ones the user added.", {}),
    _tool("add_custom_status",
          "Add a custom status label (just a label: no reminder attached). Leave channel out to add it for both channels.",
          {"label": {"type": "string"}, "channel": {"type": "string", "enum": list(leads.CHANNELS)}}, ["label"]),
    _tool("next_leads",
          "Get the next leads to contact, in the order they were imported, with their profile links. "
          "Use when the user asks for leads to message / reach out to / send to.",
          {"channel": {"type": "string", "enum": list(leads.CHANNELS)},
           "count": {"type": "integer", "description": "How many (default 20). For 'all' pass 500."},
           "status": {"type": "string", "description": "Default 'new'."}},
          ["channel"]),
    _tool("set_lead_status",
          "Set the status of leads, dated today. If a lead already has that status nothing changes and the result says so. Pass the status exactly as the user says it (e.g. 'dm sent', 'customer replied', "
          "'connection accepted', 'message sent', or a custom label). Any status can be set at any time. Two ways: (a) name them in "
          "`contacts` (names, @handles or profile links); or (b) in bulk with `from_status` (every lead currently in that status, "
          "optionally one `channel`, optionally only the first `count` in file order).",
          {"contacts": {"type": "array", "items": {"type": "string"}, "description": "Names/handles/links. Omit when using from_status."},
           "status": {"type": "string", "description": "The new status, as the user words it."},
           "channel": {"type": "string", "enum": list(leads.CHANNELS), "description": "Narrow the match / limit a bulk change to one channel."},
           "from_status": {"type": "string", "description": "Bulk: change every lead currently in this status."},
           "updated_on": {"type": "string", "description": "Bulk: only leads whose status was set on this day (YYYY-MM-DD or 'today')."},
           "count": {"type": "integer", "description": "Bulk: only the first N matching leads, in the order they were imported."}},
          ["status"]),
    _tool("set_status_bucket",
          "Change how a status is treated: 'cold' = reached out, no reply (grouped follow-up task), 'warm' = they replied (own task per person), "
          "'closed' = nothing more to do (task closed), 'none' = no task. Applies to the leads already in that status. Leave channel out to change it on both channels.",
          {"status": {"type": "string", "description": "The status label as the user says it, e.g. 'call booked'."},
           "bucket": {"type": "string", "enum": list(store.BUCKETS)},
           "channel": {"type": "string", "enum": list(leads.CHANNELS)}}, ["status", "bucket"]),
    _tool("set_follow_up",
          "Set a custom follow-up date for specific leads: their calendar reminder moves to that date (or is created).",
          {"contacts": {"type": "array", "items": {"type": "string"}}, "date": _DATE,
           "channel": {"type": "string", "enum": list(leads.CHANNELS)}}, ["contacts", "date"]),
    _tool("reschedule_follow_ups",
          "Move open lead follow-up reminders to another date. Filter with `from_date` (a date, or 'overdue') and/or `contacts`; "
          "with neither, it moves every open lead reminder.",
          {"to_date": _DATE, "from_date": {"type": "string", "description": "A YYYY-MM-DD date, or 'overdue'."},
           "contacts": {"type": "array", "items": {"type": "string"}}}, ["to_date"]),
]


def _system_prompt() -> str:
    now = store.now_local()
    return (
        "You are the calendar assistant inside LyteKeepr, a personal tool for one user who runs three projects: "
        "lytekeepr (LinkedIn outreach), aspan (Aspan CRM, Instagram outreach) and nexy. You manage their calendar "
        "(tasks, meetings, reminders, repeating activities) and their outreach lead list through your tools.\n"
        f"Right now it is {now.strftime('%A %Y-%m-%d %H:%M')} in Montreal (America/Toronto). Resolve 'today', 'tomorrow', "
        "'next Tuesday' etc. from that. Weeks start on Monday.\n"
        "Only change the calendar or leads when the user's LATEST message asks for that change. Questions (what's today, what do I have, show me…) are read-only: use get_agenda/list_events/list_leads and never create, update, mark or delete anything. Never edit notes or any other field unless asked. "
        "Rules: be brief and plain — say what you did or found, nothing more. Never claim something was added, changed "
        "or marked unless the tool result says so; if a tool returns an error, say so. If a request is missing something "
        "you truly need (like a time), ask one short question; otherwise pick sensible defaults and state them. "
        "If the user names no project, use lytekeepr unless context points elsewhere, and mention which you used. "
        "To find an item to change, call list_events or get_agenda first and use the real id. "
        "You cannot delete directly: delete_event only shows the user a Confirm button — tell them to press it. "
        "Events from Google Calendar (source google) are read-only; the user can still add their own notes and meeting status with update_meeting. Marking a conference call completed adds a follow-up 24 hours after it ended. Google also syncs by itself every hour; if a sync says Google needs reconnecting, tell the user to click 'Reconnect Google' in the calendar sidebar (or open /auth/google/start).\n"
        "When listing, show time and title, grouped by day, in 12-hour format. You only know about the calendar and leads.\n"
        "Leads to contact: when the user asks for leads to message (e.g. 'give me 20 instagram leads'), call next_leads and reply with one short "
        "heading line such as '5 Instagram leads:', then ONLY the profile links, one per line, no bullets, numbers or extra words, so they can be copy-pasted, "
        "then the single line 'Tell me when you\'ve messaged them.' and NOTHING after it. If fewer are available than asked, say so. "
        "When the user says they messaged those leads (or pastes links or names back), call set_lead_status using the links or names from your latest list "
        "(or from what they pasted), with status 'dm sent' for Instagram leads or 'connection request sent' for LinkedIn leads, then say how many changed and that "
        "follow-ups are scheduled 3 days out. For replies use 'customer replied' (Instagram) or 'replied' (LinkedIn). Change a lead's status only when the user says "
        "they did that action. The bare word 'sent' is only casual shorthand: for Instagram it means 'dm sent', for LinkedIn 'connection request sent'. It is the SAME "
        "status, never a different one.\n"
        "LOOK FIRST: when a request points at leads by description ('today's', 'those', 'the 24', 'the Instagram ones'), call list_leads FIRST (updated_on='today' for "
        "today's) and read their current status before answering or asking anything. If they already have the status the user wants, say so and change nothing; "
        "set_lead_status also tells you when a lead already had the status.\n"
        "FINISHED TASKS: when the user says they finished or are done with something ('I'm done with the Instagram task'), they mean CALENDAR TASKS, not lead statuses. "
        "Call get_agenda to see today's (and overdue) tasks, match the user's words to the exact tasks, then call propose_complete_tasks with their ids and dates and one "
        "plain sentence of how you understood the request. You cannot complete tasks yourself: the user must press Confirm. If several tasks could match, list them and "
        "ask which. Never change lead statuses unless the user says they messaged people.\n"
        "Any number works: 'give me 5', 'give me 40', 'give me all' (pass count 500 for all). "
        "If the user asks what leads they have / what's in the list (or is unsure), call lead_summary and answer with the counts per channel and status, "
        "and if they want to see them, call list_leads and show one per line as '@handle - about' (the notes tell what each business is), with the link only if asked. "
        "To find leads of a kind (e.g. 'agencies', 'Kazakhstan'), use list_leads with query.\n"
        "LEAD FACTS (this is all you know about leads): each lead has a name or @handle, a channel (linkedin or instagram), a status, the date of that status, "
        "an optional follow-up date, a profile link and a short note. Nothing else is stored; never invent other fields.\n"
        "STATUSES (pass them as the user words them): Instagram: new, dm sent, customer replied, customer not interested. "
        "LinkedIn: new, connection request sent, connection accepted, message sent, replied. The user can add custom statuses (add_custom_status): plain labels "
        "with no reminder. Call list_statuses if unsure what exists. To tell the user what specific leads are marked as (names, handles or pasted links), call list_leads with `contacts`; never say a lead isn't in the list without having searched with `contacts`. The order of statuses does not matter: any status can be set at any time.\n"
        "TASKS FROM LEADS (they have no clock time and appear in the 'No specific time' strip at the top of each day): each status is cold, warm, closed or none. "
        "COLD (reached out, no reply: dm sent, connection request sent, message sent, connection accepted) -> one follow-up task, grouped on screen per channel per day "
        "('connection accepted' schedules 'Send DM' for the next day; the others a follow-up 3 days later). WARM (customer replied, replied) -> its own 'Reply to <name>' task for the next day, "
        "which stays until the status changes. CLOSED (customer not interested, non customer) -> the task is closed. Custom statuses make no task unless the user says how to treat them: "
        "when you add a custom status, ask once whether it should be cold, warm, closed or none, then use set_status_bucket. "
        "set_follow_up gives specific leads a custom follow-up date; reschedule_follow_ups moves follow-ups to another date (from_date 'overdue' for overdue ones).\n"
        "HONESTY RULES: never guess a status or any value the user did not state. If the user asks for a status that does not exist, say so in one sentence, "
        "name the closest existing statuses, and change nothing until they choose. Ask at most ONE short question per message, never ask something the user "
        "already answered, and never repeat a question. If the request is clear, just do it. After any change, say exactly what changed (how many, which status). "
        "If the user says you got it wrong, offer to undo (undo_last_lead_change) instead of asking again. Do not apologise at length."
    )


def _compact(o: dict) -> dict:
    out = {"id": o["id"], "date": o["date"], "time": "no specific time" if o.get("untimed") else o["start_time"], "minutes": o["duration_min"],
           "title": o["title"], "project": o["project"], "kind": o["kind"], "repeat": o["repeat"], "done": o["done"]}
    if o.get("lead"):
        out["lead"] = {"contact": o["lead"]["contact"], "status": leads.label_for(o["lead"]["channel"], o["lead"]["status"])}
    return out


def _norm_ref(text: str) -> str:
    t = (text or "").strip().lower().split("?")[0].split("#")[0].rstrip("/")
    return t.replace("https://", "").replace("http://", "").replace("www.", "")


def _find_leads(contact: str, channel: str | None) -> list[dict]:
    """Match by name, @handle, or a pasted profile link."""
    wanted = _norm_ref(contact).lstrip("@")
    pool = leads.list_leads(channel)
    exact = [l for l in pool if l["contact"].lstrip("@").lower() == wanted or _norm_ref(l["url"]) == wanted]
    if exact:
        return exact
    if "/" in wanted:   # a link that isn't identical (extra path, tracking bits)
        return [l for l in pool if l["url"] and _norm_ref(l["url"]) in wanted]
    return [l for l in pool if wanted and wanted in l["contact"].lower()]


def _day(text: str) -> str:
    return store.now_local().date().isoformat() if str(text).strip().lower() == "today" else store._parse_date(text, "updated_on").isoformat()


def _describe_tasks(understood: str, occs: list[dict]) -> str:
    """The exact wording shown next to the Confirm button: how the AI understood you + the real task names."""
    lines, groups = [], {}
    for o in occs:
        lead = o.get("lead")
        if lead and lead.get("bucket") == "cold":           # the same grouping the calendar strip shows
            kind = "Send DM" if o["title"].lower().startswith("send dm") else "Follow up"
            groups.setdefault((o["date"], lead["channel"], kind), []).append(lead["contact"])
        else:
            lines.append(f'• "{o["title"]}" ({_date.fromisoformat(o["date"]).strftime("%a %b %d")})')
    for (day, channel, kind), names in groups.items():
        n, label = len(names), leads.CHANNELS[channel]["label"]
        sample = ", ".join(names[:3]) + (f" and {n - 3} more" if n > 3 else "")
        lines.insert(0, f'• {kind} · {n} {label} lead{"" if n == 1 else "s"} ({_date.fromisoformat(day).strftime("%a %b %d")}): {sample}')
    head = (understood + "\n\n") if understood else ""
    return f"{head}Mark done:\n" + "\n".join(lines) + "\n\nThis only closes the tasks; no lead statuses change. Confirm?"


def _status_key(channel: str, text: str):
    """The stored key for a status as the user words it, or None if it isn't one for that channel."""
    try:
        return leads.resolve_status(channel, text)
    except store.ValidationError:
        return None


class _Runner:
    def __init__(self):
        self.changed = False
        self.pending: list[dict] = []
        self.batch = secrets.token_hex(4)   # everything one chat request changes can be undone together

    def __call__(self, name: str, args: dict) -> str:
        try:
            return json.dumps(self._run(name, args or {}))
        except store.ValidationError as err:
            return json.dumps({"error": str(err)})
        except (ValueError, TypeError, KeyError) as err:
            return json.dumps({"error": f"bad arguments: {err}"})

    def _event_fields(self, a: dict) -> dict:
        out = {}
        for src, dst in (("title", "title"), ("project", "project"), ("kind", "kind"), ("date", "start_date"),
                         ("time", "start_time"), ("duration_min", "duration_min"), ("repeat", "repeat"),
                         ("repeat_days", "repeat_days"), ("notes", "notes"), ("no_specific_time", "untimed")):
            if a.get(src) is not None:
                out[dst] = a[src]
        return out

    def _run(self, name: str, a: dict):
        if name == "get_agenda":
            ag = store.agenda()
            return {"now": ag["now"], "overdue": [_compact(o) for o in ag["overdue"]],
                    "today": [_compact(o) for o in ag["today"]], "next_7_days": [_compact(o) for o in ag["upcoming"]]}
        if name == "list_events":
            d0, d1 = store._parse_date(a["start_date"], "start_date"), store._parse_date(a["end_date"], "end_date")
            if (d1 - d0).days > 62:
                return {"error": "range too large, max 62 days"}
            events = store.occurrences(d0.isoformat(), d1.isoformat())
            return {"count": len(events), "events": [_compact(o) for o in events[:80]]}
        if name == "create_event":
            ev = store.create_event(self._event_fields(a))
            self.changed = True
            return {"ok": True, "event": {k: ev[k] for k in ("id", "title", "project", "kind", "start_date", "start_time",
                                                             "duration_min", "repeat", "repeat_days")}}
        if name == "update_event":
            ev = store.update_event(int(a["event_id"]), self._event_fields(a))
            if not ev:
                return {"error": "no such event"}
            self.changed = True
            return {"ok": True, "event": {k: ev[k] for k in ("id", "title", "project", "start_date", "start_time", "repeat")}}
        if name == "move_overdue":
            out = store.move_overdue(a["to"])
            self.changed = self.changed or out["moved"] > 0
            return out
        if name == "sync_google_calendar":
            try:
                out = google_sync.sync(trigger="chat")
            except google_sync.GoogleAuthError as err:
                return {"error": str(err), "needs_reconnect": not err.never_connected}
            except google_sync.GoogleSyncError as err:
                return {"error": str(err)}
            self.changed = True
            return out
        if name == "update_meeting":
            ev = store.set_meeting(int(a["event_id"]), a.get("meeting_status"), a.get("my_notes"))
            if not ev:
                return {"error": "no such event"}
            self.changed = True
            return {"ok": True, "event": {k: ev[k] for k in ("id", "title", "meeting_status", "my_notes", "has_call")}}
        if name == "propose_complete_tasks":
            by_day = {}
            for it in (a.get("items") or [])[:500]:
                by_day.setdefault(str(it["date"]), set()).add(int(it["event_id"]))
            open_occ, already, missing = [], 0, 0
            for day, ids in by_day.items():
                occs = {o["id"]: o for o in store.occurrences(day, day)}
                for i in ids:
                    o = occs.get(i)
                    if not o:
                        missing += 1
                    elif o["done"]:
                        already += 1
                    else:
                        open_occ.append(o)
            if not open_occ:
                return {"error": "None of those tasks are open (already done or not found), so there is nothing to confirm.",
                        "already_done": already, "not_found": missing}
            token = secrets.token_urlsafe(8)
            _pending[token] = {"kind": "complete", "items": [(o["id"], o["date"]) for o in open_occ]}
            self.pending.append({"id": token, "summary": _describe_tasks(str(a.get("understood") or "").strip(), open_occ)})
            return {"status": "awaiting_user_confirmation", "tasks_listed": len(open_occ), "already_done": already, "not_found": missing,
                    "note": "A Confirm button was shown to the user. Nothing is done yet: do not say it is done."}
        if name == "delete_event":
            ev = store.get_event(int(a["event_id"]))
            if not ev:
                return {"error": "no such event"}
            token = secrets.token_urlsafe(8)
            _pending[token] = {"kind": "delete", "event_id": ev["id"], "title": ev["title"]}
            repeats = "" if ev["repeat"] == "none" else " and all its repeats"
            self.pending.append({"id": token, "summary": f'Delete "{ev["title"]}"{repeats}?'})
            return {"status": "awaiting_user_confirmation", "note": "A Confirm button was shown to the user. Nothing is deleted yet."}
        if name == "undo_last_lead_change":
            out = leads.undo_last_change()
            self.changed = self.changed or out.get("reverted", 0) > 0
            return out
        if name == "list_statuses":
            return {ch: leads.valid_labels(ch) for ch in leads.CHANNELS}
        if name == "add_custom_status":
            out = leads.add_custom_status(a["label"], a.get("channel"))
            self.changed = True
            return out
        if name == "lead_summary":
            out = {}
            for l in leads.list_leads():
                ch = out.setdefault(l["channel"], {"total": 0, "by_status": {}})
                label = leads.label_for(l["channel"], l["status"])
                ch["total"] += 1
                ch["by_status"][label] = ch["by_status"].get(label, 0) + 1
            return out or {"note": "No leads imported yet."}
        if name == "next_leads":
            want = _status_key(a["channel"], a.get("status") or "new")
            if want is None:
                return {"error": f'"{a.get("status")}" is not a status for {a["channel"]}. Statuses: ' + ", ".join(leads.valid_labels(a["channel"]))}
            count = min(max(int(a.get("count") or 20), 1), 500)
            pool = [l for l in leads.list_leads(a["channel"], file_order=True) if l["status"] == want]
            return {"available": len(pool), "leads": [{"contact": l["contact"], "url": l["url"], "about": l["notes"][:80]}
                                                      for l in pool[:count]]}
        if name == "list_leads":
            rows, not_found = leads.list_leads(a.get("channel"), file_order=True), []
            if a.get("contacts"):
                picked = {}
                for contact in a["contacts"][:200]:
                    found = _find_leads(str(contact), a.get("channel"))
                    if not found:
                        not_found.append(contact)
                    for l in found:
                        picked[l["id"]] = l
                rows = [r for r in rows if r["id"] in picked]
            if a.get("status"):
                rows = [r for r in rows if _status_key(r["channel"], a["status"]) == r["status"]]
            if a.get("updated_on"):
                day = _day(a["updated_on"])
                rows = [r for r in rows if r["status_date"] == day]
            if a.get("query"):
                q = a["query"].lower()
                rows = [r for r in rows if q in (r["contact"] + " " + r["notes"] + " " + r["url"]).lower()]
            limit = min(max(int(a.get("limit") or 25), 1), 300)
            reminders = leads.reminders_by_lead()
            def _rem(r):
                x = reminders.get(r["id"])
                return None if not x else ("closed" if x["closed"] else "due " + x["date"])
            out = {"matching": len(rows), "shown": min(len(rows), limit),
                   "leads": [{"contact": r["contact"], "channel": r["channel"], "status": leads.label_for(r["channel"], r["status"]),
                              "status_date": r["status_date"], "follow_up_date": r["follow_up_date"], "reminder": _rem(r),
                              "about": r["notes"][:100], "url": r["url"]} for r in rows[:limit]]}
            if not_found:
                out["not_found"] = not_found
            return out
        if name == "set_lead_status":
            status = str(a.get("status") or "")
            if a.get("from_status"):
                pool = [l for l in leads.list_leads(a.get("channel"), file_order=True)
                        if _status_key(l["channel"], a["from_status"]) == l["status"]]
                if a.get("updated_on"):
                    day = _day(a["updated_on"])
                    pool = [l for l in pool if l["status_date"] == day]
                if a.get("count"):
                    pool = pool[:max(0, int(a["count"]))]
                changed_n, already_n, skipped, why = 0, 0, 0, ""
                for lead in pool[:300]:
                    try:
                        res = leads.set_status(lead["id"], status, batch=self.batch)
                        if res and res.get("changed"):
                            changed_n += 1
                        else:
                            already_n += 1
                    except store.ValidationError as err:
                        skipped, why = skipped + 1, str(err)
                self.changed = self.changed or changed_n > 0
                out = {"changed": changed_n, "already_had_that_status": already_n, "to_status": status, "first_few": [l["contact"] for l in pool[:5]]}
                if skipped:
                    out.update({"skipped_invalid_status_for_channel": skipped, "why": why})
                return out
            results = []
            for contact in (a.get("contacts") or [])[:100]:
                matches = _find_leads(str(contact), a.get("channel"))
                if not matches:
                    results.append({"contact": contact, "result": "not_found"})
                elif len(matches) > 1:
                    results.append({"contact": contact, "result": "ambiguous",
                                    "matches": [f'{m["contact"]} ({m["channel"]})' for m in matches[:5]]})
                else:
                    try:
                        lead = leads.set_status(matches[0]["id"], status, batch=self.batch)
                        if lead.get("changed"):
                            results.append({"contact": lead["contact"], "result": "updated", "status": lead["status_label"]})
                            self.changed = True
                        else:
                            results.append({"contact": lead["contact"], "result": "already had this status, nothing changed", "status": lead["status_label"]})
                    except store.ValidationError as err:
                        results.append({"contact": contact, "result": "invalid_status", "error": str(err)})
            return {"results": results}
        if name == "set_status_bucket":
            out = leads.set_status_bucket(a["status"], a["bucket"], a.get("channel"))
            self.changed = True
            return out
        if name == "set_follow_up":
            results = []
            for contact in (a.get("contacts") or [])[:100]:
                matches = _find_leads(str(contact), a.get("channel"))
                if len(matches) != 1:
                    results.append({"contact": contact, "result": "not_found" if not matches else "ambiguous"})
                    continue
                lead = leads.set_follow_up(matches[0]["id"], a["date"], batch=self.batch)
                results.append({"contact": lead["contact"], "result": "follow-up set", "date": a["date"]})
                self.changed = True
            return {"results": results}
        if name == "reschedule_follow_ups":
            ids = None
            if a.get("contacts"):
                ids = []
                for contact in a["contacts"][:100]:
                    found = _find_leads(str(contact), None)
                    if len(found) == 1:
                        ids.append(found[0]["id"])
            out = leads.reschedule_follow_ups(a["to_date"], ids, a.get("from_date"), batch=self.batch)
            self.changed = self.changed or out["moved"] > 0
            return out
        return {"error": f"unknown tool {name}"}


def clean_messages(raw) -> list[dict] | None:
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for m in raw[-MAX_MESSAGES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            return None
        out.append({"role": m["role"], "content": m["content"][:MAX_CHARS]})
    return out if out and out[-1]["role"] == "user" else None


class RateLimited(Exception):
    pass


def _check_rate_limit() -> None:
    now = time.time()
    with _sent_lock:
        while _sent and now - _sent[0] > 3600:
            _sent.popleft()
        if len(_sent) >= CHAT_PER_HOUR:
            wait = int((3600 - (now - _sent[0])) // 60) + 1
            raise RateLimited(f"Chat limit reached ({CHAT_PER_HOUR} messages per hour). Try again in about {wait} min.")
        _sent.append(now)


def reply(messages: list[dict]) -> dict:
    _check_rate_limit()
    config = AssistantConfig.from_env()
    runner = _Runner()
    text = generate_reply(
        [{"role": "system", "content": _system_prompt()}] + messages,
        api_key=config.api_key, model=config.model, tools=TOOLS, tool_runner=runner,
        max_iterations=10, final_summary=True, reasoning_effort="low",       # a request can need several steps (look up, create, set, check)
    )
    if text.startswith("I wasn't able to finish that"):
        text = "I ran out of steps before I could finish my reply. Some changes may already be done: check the calendar or leads, then ask me to continue."

    return {"reply": text, "changed": runner.changed, "pending": runner.pending}


def resolve_pending(token: str, approve: bool) -> dict:
    action = _pending.pop(token, None)
    if not action:
        return {"ok": False, "message": "That request has expired."}
    if action.get("kind") == "complete":
        if not approve:
            return {"ok": True, "changed": False, "message": "Okay, I won't mark them done."}
        n = sum(1 for event_id, day in action["items"] if store.set_done(event_id, day, True))
        return {"ok": True, "changed": n > 0, "message": f"Marked {n} task{'' if n == 1 else 's'} done."}
    if not approve:
        return {"ok": True, "changed": False, "message": f'Okay, I won\'t delete "{action["title"]}".'}
    if store.delete_event(action["event_id"]):
        return {"ok": True, "changed": True, "message": f'Deleted "{action["title"]}".'}
    return {"ok": False, "message": "That item no longer exists."}


__all__ = ["reply", "resolve_pending", "clean_messages", "OpenAIServiceError"]
