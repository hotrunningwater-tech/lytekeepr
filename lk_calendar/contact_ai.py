"""The contact assistant: paste messy notes, get a proposed update to the contact's fields and a structured summary.
It never saves anything: the screen shows the proposal and the user applies it."""

import json
import re

from assistant.config import AssistantConfig
from services.openai.client import generate_reply

from . import chat, contacts, profile

FIELDS = ["name", "company", "job_title", "email", "phone", "profile_url"]

TOOL = chat._tool(
    "propose_contact_update",
    "Show the user a proposed update to this contact. Include ONLY fields whose value is actually written in the text the user gave. "
    "Leave a field out when it is not there. Nothing is saved: the user reviews and applies it.",
    {**{f: {"type": "string"} for f in FIELDS if f != "profile_url"},
     "profile_url": {"type": "string", "description": "A LinkedIn or Instagram profile link, only if written in the text."},
     "notes": {"type": "string", "description": "A structured, readable summary of the notes (short headings you choose to fit the content, bullet points). "
                                                "Keep the user's facts and wording; add nothing that is not in the text."},
     "evidence": {"type": "array", "description": "For each field you filled, the short quote from the text it came from.",
                  "items": {"type": "object", "properties": {"field": {"type": "string"}, "quote": {"type": "string"}}}}},
    [])

CONFIRM_TOOL = chat._tool("confirm_save", "Call this when a proposed update is waiting and the user approves it (yes, good, confirmed, ok, save it, looks right, in any language). It applies and saves it.", {}, [])

SYSTEM = """You are a friendly, helpful assistant sitting next to one CRM contact. You can chat normally (greet back, answer questions, help think about this person or what to do next), and your special skill is keeping the contact up to date: the user pastes notes, emails or messages about ONE person (often messy, in any order) and you turn them into a proposed update.

When the user pastes notes or gives you facts about the person, read them and call propose_contact_update with what you can find. When the user is just talking (a greeting, a question, thinking out loud), do NOT call the tool: reply naturally like a person would, briefly, and where it fits invite them to paste notes whenever they have them. Then reply with ONE or TWO short sentences: what you found (e.g. "I found his first name and that he works as a voice talent") and what is missing (e.g. "no surname, email or company"), and ask whether to apply it. Do not repeat the whole proposal in your reply; the screen shows it.

Rules:
- Fields you can fill: name, company, job_title, email, phone, profile_url (a LinkedIn or Instagram link), and notes. Nothing else. Everything else you find (industry, location, website, tools, needs, plans...) goes into the notes, never into a field.
- It is completely fine if some fields are missing. Fill what is written, leave the rest out, and just mention in one short phrase what is not there (for example "no company or email in these notes"). Never ask the user to supply them and never complain.
- Use ONLY what is written in the text. Never guess or infer a name, company or any value. A first name alone is a valid name.
- job_title (shown to the user as "Job title / vocation") is broad: a job title, an occupation, or a life situation (student, retired, between jobs, freelancer). Some people have none: then leave it out. Fill it with the ONE you think is correct and current. If the text mentions several, mark EVERY one in the notes as [[role:Voice talent]] so the user can add or swap them; keep it short, as the person would say it.
- notes: write a structured summary that fits what was pasted. Choose your own short headings (for example: Who, Background, What they want, Tools or platform, Next steps). Keep every point as its own short bullet in the person's own wording; do not merge, reinterpret or add anything. Keep every date and time that appears (when an email or message was sent, dates of meetings, deadlines) next to the point it belongs to, e.g. "Email Oct 16, 2:32 AM: ..."; never drop a date. If the notes name a product (for example Nexy), put what relates to that product together under that product's name, as written.
- In the notes, mark the exact words of any person's name, company name or software/tool name with [[name:Anna Weber]], [[company:Windowly]], [[software:Reaper]], [[role:Voice talent]] (use [[other:...]] for anything else you are unsure about). Copy the words exactly as written in the pasted text. Do NOT mark emails, phone numbers or links: code has already found those and marks them itself.
- If the notes contain more than one candidate for a field (two companies, two people, two emails), leave that field out and let the user choose from the marked words.
- The contact may already have values (shown below). Do not change a field that already has a value unless the user clearly asks. Only fill what is empty, or add to notes.
- If the user gives a single fact ("their email is x"), call the tool with just that field.
- You may use what is already on the contact (shown below) to answer questions about this person, but never invent facts.
- Keep replies short and plain, in the user's language. No long explanations."""


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LINK_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]]+|(?:linkedin|instagram)\.com/[^\s)>\]]+", re.I)
PHONE_RE = re.compile(r"(?<![\w@])\+?\d[\d\s().-]{5,}\d(?!\w)")
MARK_RE = re.compile(r"\[\[(name|company|software|role|other):([^\]]+?)\]\]")


def find_by_code(text: str) -> list:
    """Emails, phone numbers and links, found with fixed patterns (never by the AI)."""
    out, seen = [], set()
    def add(kind, value):
        value = value.strip().rstrip(".,;")
        if value and (kind, value.lower()) not in seen:
            seen.add((kind, value.lower()))
            out.append({"type": kind, "text": value})
    for m in EMAIL_RE.finditer(text):
        add("email", m.group(0))
    for m in LINK_RE.finditer(text):
        add("link", m.group(0))
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 7 <= len(digits) <= 15 and not re.fullmatch(r"\d{4}[-\s]\d{1,2}[-\s]?\d{0,2}", m.group(0).strip()):
            add("phone", m.group(0))
    return out


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


def finish_notes(notes: str, original: str, found: list) -> tuple:
    """Keep only the AI's marks that really appear in the pasted text, add the code-found marks, and list every chip."""
    haystack = _norm(original)
    entities, seen = [], set()
    def keep(m):
        kind, word = m.group(1), m.group(2).strip()
        if _norm(word) and _norm(word) in haystack:
            if (kind, _norm(word)) not in seen:
                seen.add((kind, _norm(word)))
                entities.append({"type": kind, "text": word})
            return f"[[{kind}:{word}]]"
        return word                                   # not in the original: drop the mark, keep the words
    notes = MARK_RE.sub(keep, notes or "")
    for item in sorted(found, key=lambda i: -len(i["text"])):
        if item["text"] in notes and f"[[{item['type']}:{item['text']}]]" not in notes:
            notes = notes.replace(item["text"], f"[[{item['type']}:{item['text']}]]")
    for item in found:
        if ("x", _norm(item["text"])) not in seen:
            seen.add(("x", _norm(item["text"])))
            entities.append(dict(item))
    return notes, entities


class _Runner:
    def __init__(self):
        self.proposal = None
        self.confirm = False

    def __call__(self, name, args):
        if name == "confirm_save":
            self.confirm = True
            return json.dumps({"saving": True})
        if name != "propose_contact_update":
            return json.dumps({"error": "unknown tool"})
        fields = {f: str(args[f]).strip() for f in FIELDS if str(args.get(f, "")).strip()}
        if "email" in fields and not contacts._EMAIL_OK.match(fields["email"]):
            del fields["email"]
        if "profile_url" in fields and not contacts._LINK_OK.match(fields["profile_url"]):
            del fields["profile_url"]
        if "phone" in fields and not 7 <= len(re.sub(r"\D", "", fields["phone"])) <= 15:
            del fields["phone"]
        notes = str(args.get("notes", "")).strip()
        evidence = {str(e.get("field", "")): str(e.get("quote", ""))[:200] for e in (args.get("evidence") or []) if isinstance(e, dict)}
        self.proposal = {"fields": fields, "notes": notes, "evidence": evidence}
        return json.dumps({"shown_to_user": True})


def reply(messages: list, current: dict, pending: bool = False) -> dict:
    chat._check_rate_limit()
    original = messages[-1]["content"]
    found = find_by_code(original)
    config = AssistantConfig.from_env()
    runner = _Runner()
    have = {k: v for k, v in (current or {}).items() if k in FIELDS + ["notes", "url"] and str(v or "").strip()}
    system = SYSTEM + ("\n\nA proposed update is waiting for the user's approval. If the user approves it (yes, good, confirmed, ok, save it, in any language), call confirm_save; do not propose it again." if pending else "") + ("\n\nAlready found by code in the user's latest message (do not mark these): " + json.dumps(found, ensure_ascii=False) if found else "") + "\n\nCurrent values on this contact (empty fields are not listed):\n" + (json.dumps(have, ensure_ascii=False) if have else "(a new contact: nothing yet)")
    text = generate_reply([{"role": "system", "content": system}] + messages, api_key=config.api_key, model=config.model,
                          tools=[TOOL, CONFIRM_TOOL] if pending else [TOOL], tool_runner=runner, max_iterations=3, final_summary=True, reasoning_effort="low")
    proposal = runner.proposal
    if proposal and not (proposal["fields"] or proposal["notes"]):
        proposal = None
    if proposal:
        proposal["notes"], proposal["entities"] = finish_notes(proposal["notes"], original, found)
        # exactly one email / phone / link in the text: prefill it (several: the user picks from the chips)
        for kind, field in (("email", "email"), ("phone", "phone"), ("link", "profile_url")):
            hits = [i["text"] for i in found if i["type"] == kind]
            if len(hits) == 1 and field not in proposal["fields"]:
                proposal["fields"][field] = hits[0]
    note = ""
    if proposal and proposal["fields"].get("email"):
        try:
            with contacts._crm() as c:
                row = c.execute("SELECT p.person_id, p.name FROM channel_accounts a JOIN people p ON p.person_id = a.person_id WHERE a.channel = 'email' AND a.channel_identifier = ?",
                                (proposal["fields"]["email"].strip().lower(),)).fetchone()
            if row and row["person_id"] != (current or {}).get("id"):
                note = f"That email already belongs to {row['name'] or 'another contact'} in your CRM."
        except Exception:
            pass
    return {"reply": profile.sign(text), "proposal": proposal, "duplicate": note, "confirm": runner.confirm}


ASK_SYSTEM = """You are a helpful assistant sitting next to ONE CRM contact. The user asks you questions about this person: who they are, what was said, what is in the notes or the messages, where they stand in the funnel, what to do or say next. You may also help think things through or draft a short message.

Rules:
- Answer ONLY from the information below (the contact's details, notes, raw notes, funnel and statuses, and messages). If the answer is not there, say so plainly. Never invent facts about the person.
- You cannot change anything yourself. If the user's message implies they want to ADD or CHANGE information about this person, even without saying "edit" (they paste notes or an email about the person, say "he now works at Acme", "update his email", "add that he prefers Windows", "change the job title", or give new facts to record), call request_edit. Then reply with ONE short sentence that asks them to confirm opening the editor, for example: "That sounds like an update. Do you want to start edit mode? Please confirm." Do not call request_edit for plain questions or chat.
- This chat only knows THIS contact. A message that names no other person is about this contact (so "I'd like to send an email" means an email to this contact).
- If the user wants an email or message written, drafted, sent or replied to (to this contact or to anyone), or asks about a DIFFERENT person, do NOT write or answer it yourself: call hand_over_to_home (contact = the other person's name exactly as the user said it, or empty when it is about this contact; request = what they want, in a short sentence), then reply with ONE short sentence asking them to confirm that you will open the Home chat and pass it on, for example: "That's done in the Home chat. Want me to open it and hand this over? Please confirm."
- Chat normally when the user is just talking. Keep answers short and plain, in the user's language."""


def _ask_context(person_id: int) -> str:
    from crm import db as crm_db
    c = contacts.get_contact(person_id)
    if not c:
        return "(this contact no longer exists)"
    out = ["CONTACT", json.dumps({k: c.get(k) for k in ("name", "company", "job_title", "email", "phone", "telegram", "url", "location", "industry", "website", "headline", "batch") if c.get(k)}, ensure_ascii=False)]
    out += ["NOTES (structured)", (c.get("notes") or "(none)")[:6000], "RAW NOTES (what the user pasted)", (c.get("raw_notes") or "(none)")[:8000]]
    if c.get("funnels"):
        f = c["funnels"][0]
        out += ["FUNNEL", f"{f['channel']}: stage {f['status'] or '(none)'}"]
    else:
        out += ["FUNNEL", "not in a funnel"]
    try:
        info = contacts.outreach(person_id)
        lines = [f"{ch['label']}: {ch['status'] if ch['lead_id'] else 'not started'}" + (f" since {ch['status_date']}" if ch['status_date'] else "") + (f", follow-up {ch['follow_up_date']}" if ch['follow_up_date'] else "") for ch in info["channels"]]
        out += ["STATUS PER CHANNEL", "\n".join(lines) or "(none)"]
    except Exception:
        pass
    try:
        with contacts._crm() as conn:
            detail = crm_db.get_person_detail(conn, person_id) or {}
        msgs = contacts.messages_with_saved_emails(person_id, detail.get("messages") or [])[-30:]
        out += ["MESSAGES (oldest first, last 30)", "\n".join(f"[{m['timestamp'][:16]} {m['channel']} {m['direction']}] {(m['message_text'] or '')[:400]}" for m in msgs) or "(no messages yet)"]
    except Exception:
        pass
    try:                                     # recorded calls (Fathom): the summary when there is one, else the start of the transcript
        from . import calls
        recent = calls.calls_for_person(person_id)[:5]
        if recent:
            lines = []
            for c in recent:
                body = c["summary"] or calls.transcript_text(c, 4000)
                lines.append(f"[{c['started_at']}] call '{c['title']}' ({c['duration_min']} min, {c['status']})\n{body}")
            out += ["CALLS (newest first)", "\n\n".join(lines)]
    except Exception:
        pass
    return "\n".join(out)


EDIT_TOOL = chat._tool("request_edit", "The user wants to add or change information about this contact. The user will be asked to confirm before the editor opens. Call it, then reply with one short confirmation question.",
                       {"summary": {"type": "string", "description": "What the user wants to change, in a few words."}}, [])


HANDOVER_TOOL = chat._tool("hand_over_to_home", "The user wants an email or message written or sent (to this contact or to someone else), or asks about a different person. The Home chat does that, so this hands the request over after the user confirms. Call it instead of writing or answering anything yourself, then reply with one short confirmation question.",
                           {"contact": {"type": "string", "description": "The other person's name exactly as the user said it; empty when the request is about this contact."},
                            "channel": {"type": "string", "description": "The channel the user asked for (email, LinkedIn, Instagram, WhatsApp...). Empty if they did not say."},
                            "request": {"type": "string", "description": "What the user wants, in one short sentence."}}, [])


class _AskRunner:
    def __init__(self):
        self.offer = False
        self.handover = None

    def __call__(self, name, args):
        if name == "hand_over_to_home":
            self.handover = {"contact": str(args.get("contact", "")).strip(), "channel": str(args.get("channel", "")).strip(), "request": str(args.get("request", "")).strip()}
            return json.dumps({"ok": True, "note": "Now reply with one short sentence asking the user to confirm opening the Home chat. Do not write or answer it yourself."})
        if name == "request_edit":
            self.offer = True
            return json.dumps({"ok": True, "note": "Now reply with one short sentence asking the user to confirm opening the editor."})
        return json.dumps({"error": "unknown tool"})


def ask(messages: list, person_id: int) -> dict:
    chat._check_rate_limit()
    config = AssistantConfig.from_env()
    runner = _AskRunner()
    text = generate_reply([{"role": "system", "content": ASK_SYSTEM + "\n\n" + _ask_context(person_id)}] + messages,
                          api_key=config.api_key, model=config.model, tools=[EDIT_TOOL, HANDOVER_TOOL], tool_runner=runner,
                          max_iterations=3, final_summary=True, reasoning_effort="low")
    note = ""
    if runner.handover:
        h = runner.handover
        if h["contact"]:
            who = h["contact"]
        else:
            me = contacts.get_contact(person_id) or {}
            who = (me.get("name") or me.get("company") or "this contact") + (f" ({me['email']})" if me.get("email") else "")
        if h["channel"]:
            note = f"I want to send a {h['channel']} message to {who}. {h['request'].strip()}".strip()
        else:
            note = f"I want to send a message to {who}. {h['request'].strip()} (I have not chosen the channel yet: ask me which one.)".strip()
    return {"reply": profile.sign(text), "proposal": None, "duplicate": "", "confirm": False, "edit_offer": runner.offer, "handover": {"note": note} if note else None}
