"""
Extracts chronology events from email/document sources using the OpenAI
API. chronology/ has zero knowledge this exists — it only ever calls the
two functions below (injected as extract_email_batch_fn / extract_document_fn
into chronology.pipeline.run_extraction), and gets back plain dicts.

This is a structured-extraction call, not the chat — no tools, no
conversation history.
"""

import json

from .config import AssistantConfig
from services.openai.client import OpenAIServiceError, generate_reply

CATEGORY_LIST = "court_procedural, confirmed_action, deadline_request, communication, case_fact"

EXTRACTION_INSTRUCTIONS = (
    "You are a chronology-extraction assistant for DLYTE, a family-law case "
    "organizer. Read the source(s) below and identify meaningful case EVENTS "
    "based on the meaning and intent of the text, not keyword matching. A "
    "single source can produce zero, one, or multiple events — for example, "
    "an email where a judge complains that materials are late should "
    "produce BOTH an important-communication event AND a separate possible "
    "non-compliance event.\n\n"
    "For each event, set natureOfClaim carefully:\n"
    "- confirmed_fact: the source establishes this as settled / actually "
    "happened (e.g. a signed agreement, a hearing that occurred, a filed "
    "document, a payment made).\n"
    "- proposed: suggested, requested, or scheduled but not yet confirmed "
    "(e.g. a proposed hearing date, a requested deadline).\n"
    "- alleged: one party's claim or accusation about the other, not an "
    "established fact.\n"
    "- disputed: something one party asserts and the other contests.\n\n"
    "category must be exactly one of: " + CATEGORY_LIST + ".\n"
    "Do not invent dates, amounts, or people not supported by the text. Do "
    "not give legal conclusions, advice, or predictions — describe events "
    "factually and neutrally.\n\n"
    "Respond with ONLY a JSON object, no prose, no markdown fences, in this "
    "exact shape (one key per input source id, value is an array of zero or "
    "more events, empty array if none):\n"
    '{"<source id>": [{"title": "...", "description": "...", "category": "...", '
    '"eventDate": "YYYY-MM-DD or null", "dateRangeFrom": "YYYY-MM-DD or null", '
    '"dateRangeTo": "YYYY-MM-DD or null", "recordedAt": "YYYY-MM-DD or null", '
    '"involvedParties": ["..."], "natureOfClaim": "confirmed_fact|proposed|alleged|disputed", '
    '"importance": "high|medium|low", "confidence": "high|medium|low", '
    '"excerpt": "short verbatim supporting quote", "pageNumber": null}]}\n'
    "Every input source id must appear as a key."
)


def _parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_email(email: dict) -> str:
    from_obj = email.get("from") or {}
    body = (email.get("textBody") or email.get("snippet") or "")[:3000]
    return (
        f"=== SOURCE id: {email['id']} (email) ===\n"
        f"From: {from_obj.get('name', '')} <{from_obj.get('email', '')}>\n"
        f"Date: {email.get('receivedAt', '')}\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Body:\n{body}\n"
    )


def extract_events_from_email_batch(emails: list[dict]) -> dict[str, list[dict]]:
    if not emails:
        return {}
    config = AssistantConfig.from_env()
    prompt = "\n\n".join(_format_email(e) for e in emails)
    messages = [
        {"role": "system", "content": EXTRACTION_INSTRUCTIONS},
        {"role": "user", "content": prompt},
    ]
    try:
        text = generate_reply(messages, api_key=config.api_key, model=config.model)
    except OpenAIServiceError:
        return {}
    parsed = _parse_json_object(text)
    return {e["id"]: parsed.get(e["id"], []) for e in emails}


def _format_document(doc: dict) -> str:
    pages = doc.get("pages") or []
    if pages:
        body = "\n\n".join(f"--- Page {p['page_number']} ---\n{p['text'][:1500]}" for p in pages[:20])
    else:
        body = (doc.get("full_text") or "")[:6000]
    return f"=== SOURCE id: {doc['job_id']} (document: {doc.get('original_filename', '')}) ===\n{body}\n"


def extract_events_from_document(doc: dict) -> list[dict]:
    config = AssistantConfig.from_env()
    prompt = _format_document(doc)
    messages = [
        {"role": "system", "content": EXTRACTION_INSTRUCTIONS},
        {"role": "user", "content": prompt},
    ]
    try:
        text = generate_reply(messages, api_key=config.api_key, model=config.model)
    except OpenAIServiceError:
        return []
    parsed = _parse_json_object(text)
    return parsed.get(doc["job_id"], [])
