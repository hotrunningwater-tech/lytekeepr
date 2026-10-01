"""Connects the OpenAI assistant to local tools. This is the seam the
prompt calls out explicitly: gmail/, chronology/, and doc_registry/ have
zero OpenAI knowledge, and services/openai/client.py has zero knowledge of
any of them — this module is where each tool's schema and dispatch live
together."""

import json

from assistant.chronology_extractor import extract_events_from_document, extract_events_from_email_batch
from assistant.delete_flow import create_pending_delete
from assistant import linkedin_send_flow
from assistant import email_send_flow
from crm import db as crm_db
from chronology import storage as chronology_storage
from chronology.pipeline import run_extraction
from doc_registry.case_documents import find_case_documents, read_case_document, search_case_document_content
from doc_registry import queue_storage
from doc_registry.registry import compute_counts
from doc_registry.scheduler import run_check_now
from services.ocr.adapter import list_recent_scans
from gmail.search import search_synced_emails
from gmail.sync import get_active_account_id, get_pending_candidates, scan_for_candidates
from chronology.query import search_events
from doc_registry.text_search import search_documents

EMAIL_SEARCH_TOOL = {
    "type": "function",
    "name": "search_synced_emails",
    "description": (
        "Search the user's locally synced Gmail records (subject, sender, "
        "recipients, body text, dates, Gmail labels). Use this whenever the "
        "user asks to find, summarize, or recall anything from their email. "
        "This only searches what has already been synced to DLYTE — it does "
        "not contact Gmail directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search across subject, sender, and message body.",
            },
            "sender": {
                "type": "string",
                "description": "Filter by sender name or email address substring.",
            },
            "date_from": {
                "type": "string",
                "description": "Earliest received date to include, as YYYY-MM-DD.",
            },
            "date_to": {
                "type": "string",
                "description": "Latest received date to include, as YYYY-MM-DD.",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Gmail label IDs to filter by, e.g. STARRED. Leave empty by default — "
                    "saved records include both received and sent mail, and most don't "
                    "carry an INBOX label (sent mail never does). Only set this if the "
                    "user explicitly asks to filter by a label or folder."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 10.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

CHRONOLOGY_SEARCH_TOOL = {
    "type": "function",
    "name": "search_chronology_events",
    "description": (
        "Search DLYTE's confirmed chronology — a compact, deduplicated, "
        "human-reviewed list of case events (hearings, deadlines, filings, "
        "communications, case facts). Prefer this FIRST for any question "
        "about what happened, when, deadlines, or case status — it's much "
        "smaller and already cross-referenced/deduplicated compared to "
        "searching raw email. Each event lists its source email/document "
        "ids; only fall back to search_synced_emails or search_documents "
        "if chronology has nothing relevant, or if the user wants the "
        "exact original wording — and when you do, use the event's own "
        "eventDate as date_from/date_to to go straight to it instead of a "
        "broad search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text search across event title and description."},
            "category": {
                "type": "string",
                "enum": ["court_procedural", "confirmed_action", "deadline_request", "communication", "case_fact"],
                "description": "Filter by event category.",
            },
            "date_from": {"type": "string", "description": "Earliest event date, YYYY-MM-DD."},
            "date_to": {"type": "string", "description": "Latest event date, YYYY-MM-DD."},
            "importance": {"type": "string", "enum": ["high", "medium", "low"]},
            "limit": {"type": "integer", "description": "Max results, default 15."},
        },
        "required": [],
        "additionalProperties": False,
    },
}

DOCUMENT_SEARCH_TOOL = {
    "type": "function",
    "name": "search_documents",
    "description": (
        "Full-text search over the OCR'd text of locally scanned documents "
        "(sworn statements, Form III, financial statements, court filings). "
        "Use this when the user asks about content inside a scanned "
        "document/PDF, not just email — email search does not cover "
        "document contents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for within document contents."},
            "limit": {"type": "integer", "description": "Max matching documents to return, default 5."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

CASE_DOCUMENT_FIND_TOOL = {
    "type": "function",
    "name": "find_case_document",
    "description": (
        "Resolve one or more already-saved OCR documents from normal user clues. "
        "Use this when the user refers to a known saved document by sender, date, "
        "subject/theme, filename fragment like P13, or attachment type like French declaration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "General topic or theme clue, such as Montreal transfer request.",
            },
            "sender": {
                "type": "string",
                "description": "Sender name or email clue for the source email.",
            },
            "date_from": {
                "type": "string",
                "description": "Earliest source email date to include, as YYYY-MM-DD.",
            },
            "date_to": {
                "type": "string",
                "description": "Latest source email date to include, as YYYY-MM-DD.",
            },
            "filename": {
                "type": "string",
                "description": "Filename fragment such as P13 or exhibit.",
            },
            "attachment_type": {
                "type": "string",
                "description": "Attachment role/type clue such as French declaration or message exhibit.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matches to return. Defaults to 5.",
            },
        },
        "additionalProperties": False,
    },
}

CASE_DOCUMENT_READ_TOOL = {
    "type": "function",
    "name": "read_case_document",
    "description": (
        "Open one already-saved OCR document from DLYTE's local storage and return its full OCR text "
        "plus per-page text. Use this after find_case_document or search_case_document_content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Saved OCR job id for the document.",
            },
            "registry_id": {
                "type": "string",
                "description": "Registry entry id for the document.",
            },
        },
        "additionalProperties": False,
    },
}

CASE_DOCUMENT_CONTENT_SEARCH_TOOL = {
    "type": "function",
    "name": "search_case_document_content",
    "description": (
        "Search inside already-saved OCR documents by claim, phrase, topic, or page cue. "
        "Use this when the user asks what an exhibit says about a topic or asks about a page reference."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Topic, phrase, or page clue to search for within saved OCR documents.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matching documents to return. Defaults to 5.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

UPDATE_CASE_CHRONOLOGY_TOOL = {
    "type": "function",
    "name": "update_case_chronology",
    "description": (
        "Refresh the case timeline from new information only. Use this when "
        "the user asks whether the case is updated, asks to refresh/update "
        "the case, asks to scan for new chronology items, or asks to process "
        "new documents/emails into chronology. This tool scans Gmail for new "
        "candidate messages, refreshes the document registry, and runs "
        "incremental chronology extraction on only previously unprocessed "
        "approved emails and OCR documents. It never silently rewrites "
        "confirmed chronology events; conflicts are sent to review."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

CASE_STATUS_TOOL = {
    "type": "function",
    "name": "get_case_status",
    "description": (
        "Return a compact snapshot of the current local case state. Use this "
        "for questions like what's pending, is the case up to date, what still "
        "needs review, or what documents/scans are currently in the system."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

CASE_SEARCH_TOOL = {
    "type": "function",
    "name": "search_case_sources",
    "description": (
        "Run one broad local search across confirmed chronology, saved emails, "
        "and OCR'd documents at the same time. Use this for broad find/search "
        "requests when the right source is not obvious yet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The text to search for across chronology, saved emails, and OCR documents.",
            },
            "limit_per_source": {
                "type": "integer",
                "description": "Maximum results to return from each source type. Defaults to 5.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

CRM_SEARCH_TOOL = {
    "type": "function",
    "name": "search_crm_messages",
    "description": (
        "Search the user's curated CRM message history — across every "
        "connected messaging channel at once (LinkedIn and email), not just "
        "one. Use this FIRST whenever the user asks what someone "
        "said/discussed, a specific person's message history, or 'do we "
        "have any messages from X' — regardless of which channel it might "
        "be on. Results come back in chronological "
        "order, tagged with which channel each message is from, and with "
        "company name when known. Note: for email specifically this only "
        "covers emails the user has explicitly saved into the CRM, not the "
        "full raw inbox — if this comes back empty for an email question, "
        "it's still worth falling back to search_synced_emails (the full "
        "synced inbox, not just saved/CRM ones) before concluding nothing "
        "exists. LinkedIn coverage here is complete (everything synced is "
        "already in the CRM), so no such fallback is needed for LinkedIn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person_name": {
                "type": "string",
                "description": "Filter to one person by name substring, e.g. Sergiy.",
            },
            "query": {
                "type": "string",
                "description": "Free-text search within message content.",
            },
            "channel": {
                "type": "string",
                "description": "Optionally narrow to one channel, e.g. linkedin. Leave unset to search all channels.",
            },
            "since_days": {
                "type": "number",
                "description": "How many days back to search. Defaults to 30. Pass a larger number if the user asks about older history.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of messages to return. Defaults to 50.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

FIND_LINKEDIN_CONTACT_TOOL = {
    "type": "function",
    "name": "find_linkedin_contact",
    "description": (
        "Look up a LinkedIn contact by name — tolerates small typos/misspellings. "
        "Use this as soon as the user names a contact to message, BEFORE you know "
        "the message content yet, so you can confirm who you're actually talking "
        "about while also asking what the message should say. Returns the "
        "contact's real name if found, or says clearly if no match / more than "
        "one match exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person_name": {
                "type": "string",
                "description": "The name as the user typed it, even if possibly misspelled.",
            },
        },
        "required": ["person_name"],
        "additionalProperties": False,
    },
}

FIND_EMAIL_CONTACT_TOOL = {
    "type": "function",
    "name": "find_email_contact",
    "description": (
        "Look up an email contact by name — tolerates small typos/misspellings. "
        "Use this as soon as the user names a contact to email, BEFORE you know "
        "the message content yet, so you can confirm who you're actually talking "
        "about while also asking what the email should say. Returns the "
        "contact's real name and email address if found, or says clearly if no "
        "match / more than one match exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person_name": {
                "type": "string",
                "description": "The name as the user typed it, even if possibly misspelled.",
            },
        },
        "required": ["person_name"],
        "additionalProperties": False,
    },
}

DRAFT_EMAIL_MESSAGE_TOOL = {
    "type": "function",
    "name": "draft_email_message",
    "description": (
        "Stage an email to one existing contact for the user to review and "
        "send. This NEVER sends anything by itself — it only prepares a "
        "draft; the actual send only happens if the user clicks the 'Send "
        "now' button that appears in the UI and then confirms in the popup "
        "that follows. Use this when the user asks you to email, reply to, "
        "or follow up with someone by email. After calling this, tell the "
        "user the draft is ready and that a Send now button has appeared "
        "— never say the email was sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person_name": {
                "type": "string",
                "description": "The email contact's name, e.g. Sergiy Zasorin.",
            },
            "subject": {
                "type": "string",
                "description": "The email subject line.",
            },
            "body": {
                "type": "string",
                "description": "The exact email body text to draft.",
            },
        },
        "required": ["person_name", "body"],
        "additionalProperties": False,
    },
}

DRAFT_LINKEDIN_MESSAGE_TOOL = {
    "type": "function",
    "name": "draft_linkedin_message",
    "description": (
        "Stage a LinkedIn message to one existing contact for the user to "
        "review and send. This NEVER sends anything by itself — it only "
        "prepares a draft; the actual send only happens if the user clicks "
        "the 'Send now' button that appears in the UI and then confirms in "
        "the popup that follows. Use this when the user asks you to message, "
        "reply to, or follow up with someone on LinkedIn. After calling this, "
        "tell the user the draft is ready and that a Send now button has "
        "appeared — never say the message was sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person_name": {
                "type": "string",
                "description": "The LinkedIn contact's name, e.g. Sergiy Zasorin.",
            },
            "message_text": {
                "type": "string",
                "description": "The exact message text to draft.",
            },
        },
        "required": ["person_name", "message_text"],
        "additionalProperties": False,
    },
}

DELETE_SCANNED_DOCUMENT_TOOL = {
    "type": "function",
    "name": "request_delete_scanned_document",
    "description": (
        "Stage deletion of one local scanned/original document already stored "
        "inside DLYTE's OCR storage. Use this only when the user clearly asks "
        "to delete/remove a scanned document or local case file copy. This "
        "tool never deletes immediately; it only prepares a pending request "
        "that still requires the user to type exact CONFIRM in chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Exact local OCR job id if known.",
            },
            "filename": {
                "type": "string",
                "description": "Exact or near-exact local scanned document filename if job id is not known.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

TOOLS = [
    CHRONOLOGY_SEARCH_TOOL,
    EMAIL_SEARCH_TOOL,
    DOCUMENT_SEARCH_TOOL,
    CASE_DOCUMENT_FIND_TOOL,
    CASE_DOCUMENT_READ_TOOL,
    CASE_DOCUMENT_CONTENT_SEARCH_TOOL,
    UPDATE_CASE_CHRONOLOGY_TOOL,
    CASE_STATUS_TOOL,
    CASE_SEARCH_TOOL,
    DELETE_SCANNED_DOCUMENT_TOOL,
    CRM_SEARCH_TOOL,
    DRAFT_LINKEDIN_MESSAGE_TOOL,
    FIND_LINKEDIN_CONTACT_TOOL,
    FIND_EMAIL_CONTACT_TOOL,
    DRAFT_EMAIL_MESSAGE_TOOL,
]


def _run_case_chronology_update() -> dict:
    gmail_result = None
    gmail_error = None
    try:
        gmail_result = scan_for_candidates()
    except Exception as exc:
        gmail_error = str(exc)

    document_result = None
    document_error = None
    try:
        entries = run_check_now()
        document_result = {
            "counts": compute_counts(entries),
            "entriesChecked": len(entries),
        }
    except Exception as exc:
        document_error = str(exc)

    chronology_result = run_extraction(extract_events_from_email_batch, extract_events_from_document)
    account_id = get_active_account_id()
    pending_email_count = len(get_pending_candidates(account_id)) if account_id else 0

    return {
        "gmail": gmail_result,
        "gmailError": gmail_error,
        "documents": document_result,
        "documentsError": document_error,
        "chronology": chronology_result,
        "pendingEmailReviewCount": pending_email_count,
    }


def _run_case_status() -> dict:
    account_id = get_active_account_id()
    pending_email_count = len(get_pending_candidates(account_id)) if account_id else 0
    entries = run_check_now()
    doc_counts = compute_counts(entries)
    events = chronology_storage.load_events()
    review_queue = chronology_storage.load_review_queue()
    extraction_state = chronology_storage.load_extraction_state()

    return {
        "confirmedChronologyCount": len(events),
        "chronologyReviewCount": len(review_queue),
        "pendingEmailReviewCount": pending_email_count,
        "documentCounts": doc_counts,
        "scanQueueCount": len(queue_storage.load_queue()),
        "recentScans": list_recent_scans(limit=5),
        "lastChronologyRunAt": extraction_state.get("lastRunAt"),
    }


def _run_case_search(query: str, limit_per_source: int) -> dict:
    chronology_results = search_events(query=query, limit=limit_per_source)
    email_results = search_synced_emails(query=query, limit=limit_per_source)
    document_results = search_documents(query=query, limit=limit_per_source)
    return {
        "query": query,
        "chronology": chronology_results,
        "emails": email_results,
        "documents": document_results,
    }


def run_tool(name: str, arguments: dict) -> str:
    if name == "search_chronology_events":
        results = search_events(
            query=arguments.get("query"),
            category=arguments.get("category"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            importance=arguments.get("importance"),
            limit=arguments.get("limit") or 15,
        )
        return json.dumps({"results": results, "count": len(results)})
    if name == "search_synced_emails":
        results = search_synced_emails(
            query=arguments.get("query"),
            sender=arguments.get("sender"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            labels=arguments.get("labels"),
            limit=arguments.get("limit") or 10,
        )
        return json.dumps({"results": results, "count": len(results)})
    if name == "search_documents":
        results = search_documents(query=arguments.get("query", ""), limit=arguments.get("limit") or 5)
        return json.dumps({"results": results, "count": len(results)})
    if name == "find_case_document":
        results = find_case_documents(
            query=arguments.get("query"),
            sender=arguments.get("sender"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            filename=arguments.get("filename"),
            attachment_type=arguments.get("attachment_type"),
            limit=arguments.get("limit") or 5,
        )
        return json.dumps({"results": results, "count": len(results)})
    if name == "read_case_document":
        try:
            result = read_case_document(
                job_id=arguments.get("job_id"),
                registry_id=arguments.get("registry_id"),
            )
        except FileNotFoundError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result)
    if name == "search_case_document_content":
        results = search_case_document_content(
            query=arguments.get("query", ""),
            limit=arguments.get("limit") or 5,
        )
        return json.dumps({"results": results, "count": len(results)})
    if name == "update_case_chronology":
        return json.dumps(_run_case_chronology_update())
    if name == "get_case_status":
        return json.dumps(_run_case_status())
    if name == "search_case_sources":
        return json.dumps(
            _run_case_search(
                query=arguments.get("query", ""),
                limit_per_source=arguments.get("limit_per_source") or 5,
            )
        )
    if name == "search_crm_messages":
        conn = crm_db.get_connection()
        try:
            results = crm_db.search_messages(
                conn,
                channel=arguments.get("channel"),
                person_name=arguments.get("person_name"),
                query=arguments.get("query"),
                since_days=arguments.get("since_days") or 30,
                limit=arguments.get("limit") or 50,
            )
        finally:
            conn.close()
        return json.dumps({"results": results, "count": len(results)})
    if name == "find_linkedin_contact":
        return json.dumps(linkedin_send_flow.resolve_linkedin_contact(arguments.get("person_name", "")))
    if name == "draft_linkedin_message":
        return json.dumps(
            linkedin_send_flow.create_pending_send(
                person_name=arguments.get("person_name", ""),
                message_text=arguments.get("message_text", ""),
            )
        )
    if name == "find_email_contact":
        return json.dumps(email_send_flow.resolve_email_contact(arguments.get("person_name", "")))
    if name == "draft_email_message":
        return json.dumps(
            email_send_flow.create_pending_send(
                person_name=arguments.get("person_name", ""),
                subject=arguments.get("subject", ""),
                body=arguments.get("body", ""),
            )
        )
    if name == "request_delete_scanned_document":
        return json.dumps(
            create_pending_delete(
                job_id=arguments.get("job_id"),
                filename=arguments.get("filename"),
            )
        )
    return json.dumps({"error": f"Unknown tool: {name}"})
