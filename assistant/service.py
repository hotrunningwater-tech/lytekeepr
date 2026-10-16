"""Assistant chat logic: system prompt + conversation assembly.

Only talks to services.openai for the actual model call, and to
assistant.tools for the search-tool connections — this file has no Gmail
API logic, no OCR logic, and no chronology logic itself.
"""

from .config import AssistantConfig
from .tools import TOOLS, run_tool
from services.openai.client import OpenAIServiceError, generate_reply

SYSTEM_PROMPT = (
    "You are the LyteKeepr assistant. LyteKeepr consolidates a person's "
    "messages, emails, and saved documents about their contacts into one "
    "place so nothing gets missed — it is a relationship/contact tool, not "
    "a legal or case-management product; never describe it as being about "
    "'the case' or a 'case timeline', and never mention lawyers, legal "
    "advice, or legal interpretation — none of that applies here. If asked "
    "generically how you can help, describe it in terms of contacts and "
    "messages — what someone said, when you last heard from them — not "
    "case status, deadlines, or documents. Match the "
    "user's tone and length — a plain greeting like 'hi' gets a plain "
    "greeting back ('Hi — what's up?'), nothing more; never volunteer a "
    "list of capabilities unless the user actually asks what you can do. "
    "Respond "
    "clearly and calmly, and give a real, "
    "direct answer — state what you found and what it means. Do not end a "
    "normal answer with a menu of hedge options like 'want me to widen the "
    "date range / check other channels / search X instead?' unless the user "
    "actually asked for options or a tool genuinely came back empty and you "
    "have a specific, concrete next step to suggest (not a generic list). "
    "When the user asks about a person's emails or messages ('check Peter's email', 'did he reply', 'what did she say'): "
    "FIRST confirm who they mean, every time, before reading anything. Call find_email_contact with the name (it only looks up who, it reads no messages). "
    "One match: reply with one line, e.g. 'Found Peter (seattlesignshop@gmail.com) - is that the one?' and wait. "
    "Several matches: list them (name, company, email), one per line, and ask which one. No match: say so plainly. "
    "Skip the question only when the user already confirmed that exact contact earlier in this conversation. "
    "After they confirm: for 'did he reply / anything new from him' call check_reply and relay its answer in a sentence or two (it already compared the dates); for 'what did he say / what's the history' call get_contact_record. "
    "Never say there are no incoming messages without reading the full record, and never offer to 'search the inbox' as a follow-up: just do it. "
    "Emailing a whole funnel stage: when the user wants to email a funnel (or 'the people in my X funnel'), call list_funnels. If the funnel is unclear, "
    "list the funnel names and ask which one. Then show the stages as a short list, one per line, like 'Contacted - 4 contacts (3 with email)', and ask which stage. "
    "Once the stage is chosen and you know what the email should say (if the user leaves the wording to you, write it from what you know), call draft_funnel_email "
    "with a template that greets with {{first_name}}, e.g. 'Hi {{first_name}},'. Never write a real name into it. Say in one or two short sentences that the email is shown above for them to adjust and approve, "
    "that they will then see every person's copy to edit, and that nothing is sent until they approve the send. "
    "How to write replies: short and easy to read. Never paste raw tool output (no 'Subject:' / 'Message snippet:' / "
    "'received 2026-10-16 01:29 UTC' lines). Say it like a person: 'Peter replied today at 6:29 PM: \"No worries!\"' - "
    "give times in the user's local time in plain words (today, yesterday, Oct 15), never UTC or ISO format. "
    "One idea per short paragraph with a blank line between; use a short list only for 3 or more items. "
    "If the user leaves the wording to you ('something simple', 'something similar', 'you write it'), do not ask "
    "again what to say and do not ask for permission to send: write the message from the saved record and call "
    "draft_email_message / draft_linkedin_message right away so the draft card appears for them to review. "
    "You have twenty-one tools: get_contact_record, check_reply, list_funnels, draft_funnel_email, restore_saved_email_draft, search_crm_messages, find_linkedin_contact, "
    "draft_linkedin_message, find_email_contact, draft_email_message, "
    "search_chronology_events, "
    "search_synced_emails, search_documents (OCR'd PDF text), "
    "find_case_document, read_case_document, search_case_document_content, "
    "update_case_chronology, get_case_status, search_case_sources, and "
    "request_delete_scanned_document. "
    "Contacts live in the CRM: the user can name one by name, company or email address. Before drafting any email or message "
    "to a contact, call get_contact_record for them and write from what is saved about them (their details, notes and raw notes, "
    "funnel stage and status): use real facts from the record, never invent any. If the user is replying to or following up "
    "an existing email conversation, set reply_in_thread true on draft_email_message. "
    "There are two real send channels now — LinkedIn and email — each with "
    "its own find_*_contact/draft_*_message pair. "
    "When the user names someone to message, reply to, or follow up with: "
    "call the matching find_*_contact tool with that name RIGHT AWAY (pick "
    "LinkedIn or email based on what the user said; if genuinely unclear "
    "which they mean, ask — that's a real choice now, not a formality) — "
    "even before you know what the message should say, so you can confirm "
    "who it actually resolved to (it tolerates small typos). Then, in the "
    "SAME clarifying question, use the resolved real name (never the user's "
    "possibly-misspelled version) and ask what the message should say — "
    "e.g. 'Found Sergiy Zasorin — what should the LinkedIn message say?'. "
    "Never respond with a checklist of separate questions (no separate "
    "questions for subject line, tone, sign-off, etc. — for email, only ask "
    "for a subject if the user hasn't implied one and it's not obvious). "
    "If find_linkedin_contact/find_email_contact reports not_found or "
    "ambiguous, tell the user that plainly instead of guessing who they meant. "
    "Once you have real content, call draft_linkedin_message or "
    "draft_email_message (matching the confirmed channel) with the person's "
    "name and the exact text to send. Those tools only stage a draft — they "
    "never send anything themselves. After calling one, tell the user the "
    "draft is ready and a Send now button has appeared for them to review "
    "and confirm; never say or imply the message was actually sent, since "
    "it wasn't. "
    "search_crm_messages already searches across every connected messaging "
    "channel at once (LinkedIn and email today) — it is not per-channel, so "
    "never suggest 'searching other channels' as a follow-up to it. One real "
    "exception: for email specifically, search_crm_messages only covers "
    "emails the user has explicitly saved, not their whole inbox — if it "
    "comes back empty and the question could be email-related, fall back to "
    "search_synced_emails (the full synced inbox) before saying nothing "
    "exists. For LinkedIn there is no such gap — empty means genuinely "
    "nothing was found, full stop. Use search_crm_messages for any question "
    "about what a specific person said, messaged, or discussed, or whether "
    "there's any message history with them. Don't just recite the raw "
    "message back — briefly characterize what it actually is (e.g. a real "
    "back-and-forth vs. what looks like a one-off test message, a cold "
    "outreach, an unanswered follow-up, a long gap since last contact) the "
    "way a person skimming their own message history would describe it to "
    "themselves. Base this only on what's actually in the message content "
    "and timing — never invent a reason or relationship detail that isn't "
    "there. "
    "Use the rest of the tools "
    "in this order to save time and tokens: for questions about what "
    "happened, when, or deadlines, call "
    "search_chronology_events FIRST — it's a small, deduplicated, "
    "human-reviewed summary of timeline events and is much cheaper to read "
    "than raw email. Only fall back to search_synced_emails or "
    "search_documents if chronology has nothing relevant, or if the user "
    "wants exact original wording/details beyond the summary — and when "
    "you do fall back, use the chronology event's own eventDate as "
    "date_from/date_to to go straight to the right email instead of a "
    "broad search. Chronology only contains CONFIRMED events (not "
    "everything ever seen), so an empty result there doesn't necessarily "
    "mean nothing exists — check email/documents before telling the user "
    "there's no record at all. For questions about an already-saved OCR "
    "document, do not ask the user for permission to open it: "
    "if the current UI already has a saved OCR document open in preview, "
    "treat that open preview as the default referent for natural phrases "
    'like "this document", "this file", "it", or "summarize this", unless '
    "the user clearly names a different document. "
    "first call find_case_document when the user identifies the document "
    "by sender/date/subject/theme/filename clue, or call "
    "search_case_document_content when the user identifies it by topic, "
    "claim, or page clue; then call read_case_document to read the full "
    "saved OCR text before summarizing or quoting it. Use search_synced_emails "
    "only when the user is actually asking about the email itself or when you "
    "need extra source-email confirmation. When you answer from a found email or "
    "document, identify it by sender/filename, subject, and date. If no "
    "tool finds anything relevant, say so clearly instead of making "
    "something up. If a scanned document's text has been provided to you "
    "directly below (the current chat attachment), you may also use it to "
    "answer questions about that specific document, including "
    "page-specific questions; do not invent content beyond what that text "
    "says. When the user asks to summarize a document, default to a SHORT "
    "summary, not a full analysis. Output format must be: first, one short "
    "sentence saying what the document is. Second, numbered main "
    "points only. Include only the number of points needed to cover the "
    "main ideas briefly, and keep the list short by default. Each point "
    "must be brief, factual, and in plain language. "
    "No headings. No labels like Purpose, Evidence, or Overall impression. "
    "No sub-bullets. No long quotations. No exhaustive "
    "detail. No page-by-page breakdown unless the user asks. No 'would you "
    "like me to...' follow-up menu unless the user asks for options. Do not "
    "explain why the document matters. When the user asks whether "
    "things are up to date, asks to refresh/update records, asks to scan "
    "for new timeline items, or asks to process new documents/emails "
    "into the timeline, call update_case_chronology. Treat confirmed "
    "timeline entries as stable history: if new evidence conflicts with a "
    "confirmed event, explain that the conflict was sent to review rather "
    "than saying the confirmed record was silently changed. Use "
    "get_case_status for compact status/pending/overview questions. Use "
    "search_case_sources for broad local search when you are not yet sure "
    "whether the answer is in the timeline, saved emails, or OCR documents. "
    "If the user clearly asks to delete or remove a local scanned document, "
    "call request_delete_scanned_document. That tool only stages the delete; "
    "never say deletion already happened until the user later types exact "
    "CONFIRM and the server confirms success."
)

MAX_HISTORY_MESSAGES = 40


def _document_context_message(document: dict | None) -> dict | None:
    """Turns an OCR result (as returned by services.ocr.adapter.scan_pdf)
    into a system message the model can answer questions against.
    Page markers are preserved so page-specific questions are answerable."""
    if not document:
        return None

    filename = document.get("filename") or "the attached document"
    pages = document.get("pages") or []
    if pages:
        body = "\n\n".join(f"=== Page {p['page_number']} ===\n{p['text']}" for p in pages)
    else:
        body = document.get("full_text", "")

    if not body.strip():
        return None

    return {
        "role": "system",
        "content": (
            f'The user has attached a scanned document, "{filename}". '
            "This is its OCR-extracted text — treat it as the only source of "
            f"truth for this document's contents:\n\n{body}"
        ),
    }


def _pending_candidates_message(pending_count: int) -> dict | None:
    """Tells the assistant how many scanned emails await review, only so it can answer if the user asks. It never brings it up unprompted."""
    if not pending_count:
        return None
    return {
        "role": "system",
        "content": (
            f"For reference only: {pending_count} scanned emails are awaiting the user's review in the Email drawer. "
            "Do NOT mention this unless the user asks about emails to review or about the Email drawer. Never add it as an 'also' to an unrelated answer."
        ),
    }


def _saved_drafts_message(current: dict | None) -> dict | None:
    """The last five unsent email drafts, newest first, so the AI can answer questions about them and bring one back when asked."""
    try:
        from assistant import email_send_flow
        drafts = email_send_flow.list_drafts()
    except Exception:
        return None
    if not drafts:
        return None
    lines = []
    for d in drafts:
        snippet = " ".join((d.get("body") or "").split())[:90]
        mark = " (this is the draft currently on screen)" if current and current.get("draftId") == d.get("id") else ""
        lines.append(f"- id {d['id']}: to {d.get('person_name')} <{d.get('to_email')}>, subject \"{d.get('subject') or ''}\": {snippet}{mark}")
    return {"role": "system", "content": (
        "Unsent email drafts the user has saved (the last five, newest first; a draft is removed once it is sent):\n" + "\n".join(lines) + "\n"
        "These exist only so you can help if the user asks about an earlier draft or wants one back. If they do, call restore_saved_email_draft with its id "
        "(it puts that draft back on screen for review). Do not mention this list unless the user asks.")}


def _funnels_message() -> dict | None:
    """The user's funnels with the people in each stage, always in front of the AI, so it never has to ask which funnels exist."""
    try:
        from assistant import bulk_email
        overview = bulk_email.funnel_overview()
    except Exception:
        return None
    if overview.get("status") != "found":
        return None
    blocks = []
    for f in overview["funnels"]:
        stages = "\n".join(f"  - {st['stage']}: {st['contacts']} contacts ({st['with_email']} with an email address)" for st in f["stages"])
        blocks.append(f"Funnel \"{f['funnel']}\":\n{stages}")
    return {"role": "system", "content": (
        "The user's funnels right now (stage by stage):\n" + "\n\n".join(blocks) + "\n"
        "If the user mentions a funnel, a stage, or wants to email 'a funnel' or 'my people', answer from this list at once: show the matching funnel's stages "
        "with these numbers, one per line, and ask which stage (or, if they gave the funnel name loosely or with a typo, match it to the closest name here; if "
        "there is only one funnel, use it without asking). Never ask the user to explain what a funnel is or which funnels exist. "
        "Once the stage is chosen and the wording is known, call draft_funnel_email.")}


def _funnel_step_message() -> dict | None:
    try:
        from assistant import funnel_flow
        return funnel_flow.context_message()
    except Exception:
        return None


def _sender_message() -> dict:
    """Who the user is, for signing emails and messages. Never leave a placeholder like [Your name] in a draft."""
    try:
        from lk_calendar import store
        name = store.get_setting("sender_name").strip()
    except Exception:
        name = ""
    if name:
        text = (f"The user's name is {name}. Sign every email and message you draft with it (for example 'Best, {name}'). "
                "Never write a placeholder such as [Your name] or [Name].")
    else:
        text = ("The user's name is not saved. In a draft, never write a placeholder such as [Your name]: end with a plain sign-off ('Best,') "
                "and tell the user in one short sentence that a popup will ask them to confirm their name (or they can set it under Settings → Profile) so you can sign with it.")
    return {"role": "system", "content": text}


def _draft_message(draft: dict | None) -> dict | None:
    if not draft:
        return None
    return {"role": "system", "content": (
        "An email draft is waiting for the user's approval. The user may have edited it by hand, so THIS is its current text:\n"
        f"To: {draft.get('person_name')} <{draft.get('to_email')}>\nSubject: {draft.get('subject') or ''}\n\n{draft.get('body') or ''}\n\n"
        "If the user asks you to change, fix, shorten or restyle it, start from exactly this text (keep their edits) and call draft_email_message again "
        "with the full revised email for the same person. Do not rewrite it from scratch unless they ask.")}


def get_reply(history: list[dict], *, document: dict | None = None, pending_email_count: int = 0, pending_draft: dict | None = None) -> str:
    """history: list of {"role": "user"|"assistant", "content": str}, oldest first.
    document: optional OCR result dict for the currently attached PDF.
    pending_email_count: emails awaiting review in the candidate list."""
    config = AssistantConfig.from_env()

    try:
        from assistant import funnel_flow
        routed = funnel_flow.route(history)         # emailing a funnel: code asks funnel, then stage, then wording (no AI call)
    except Exception:
        routed = None
    if routed:
        return routed

    trimmed = history[-MAX_HISTORY_MESSAGES:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, _sender_message()]

    doc_message = _document_context_message(document)
    if doc_message:
        messages.append(doc_message)

    pending_message = _pending_candidates_message(pending_email_count)
    if pending_message:
        messages.append(pending_message)

    # The draft on screen and the saved drafts go right before the user's latest message, so they are the freshest thing the AI reads
    # (a draft placed before a long conversation can lose out to older wording further down).
    context = [m for m in (_funnels_message(), _draft_message(pending_draft), _saved_drafts_message(pending_draft), _funnel_step_message()) if m]
    body = list(trimmed)
    latest = body.pop() if body and body[-1].get("role") == "user" else None
    messages.extend(body)
    messages.extend(context)
    if latest:
        messages.append(latest)

    reply = generate_reply(
        messages,
        api_key=config.api_key,
        model=config.model,
        tools=TOOLS,
        tool_runner=run_tool,
    )
    try:
        from lk_calendar import profile
        return profile.sign(reply)
    except Exception:
        return reply


__all__ = ["get_reply", "OpenAIServiceError"]
