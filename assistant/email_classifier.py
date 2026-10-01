"""
Classifies scanned Gmail messages for likely relevance to the user's legal
case, using the OpenAI API. gmail/ has no knowledge this exists — it only
ever gets back a plain {message_id: {is_relevant, reason}} dict.

This is a classification call, not the chat — it does not use tools and
does not go through assistant.service.get_reply().
"""

import json

from .config import AssistantConfig
from services.openai.client import OpenAIServiceError, generate_reply

BATCH_SIZE = 12

CLASSIFIER_SYSTEM_PROMPT = (
    "You are a triage assistant for DLYTE, a tool that helps someone "
    "organize email for an active divorce/custody legal case. Given a batch "
    "of emails (sender, subject, date, a short body excerpt, attachment "
    "filenames), decide which are LIKELY RELEVANT to the case — e.g. from "
    "a lawyer, the court, the other party, about custody/parenting, child "
    "support, financial/legal documents, hearings, or mediation — versus "
    "NOT relevant (newsletters, promotions, unrelated personal mail, "
    "unrelated work/shopping/travel, etc.).\n\n"
    "Respond with ONLY a JSON array, no prose, no markdown fences, one "
    "object per email in the input, in this exact shape:\n"
    '[{"id": "<message id>", "relevant": true or false, "reason": "<one short sentence>"}]\n'
    "Every input id must appear exactly once in the output."
)


def _format_batch(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        from_obj = m.get("from") or {}
        attachments = ", ".join(a.get("filename", "") for a in m.get("attachments", []) if a.get("filename"))
        lines.append(
            f"id: {m.get('id')}\n"
            f"from: {from_obj.get('name', '')} <{from_obj.get('email', '')}>\n"
            f"subject: {m.get('subject', '')}\n"
            f"date: {m.get('receivedAt', '')}\n"
            f"attachments: {attachments or 'none'}\n"
            f"excerpt: {(m.get('textBody') or m.get('snippet') or '')[:400]}\n"
        )
    return "\n---\n".join(lines)


def _parse_json_array(text: str) -> list[dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def classify_messages(messages: list[dict]) -> dict[str, dict]:
    """messages: normalized Gmail message dicts (from gmail.parser).
    Returns {message_id: {"is_relevant": bool, "reason": str}}. On any
    classification failure for a batch, those messages are treated as not
    relevant (safe default — they can still be found later via search;
    they just won't be pre-surfaced as candidates)."""
    if not messages:
        return {}

    config = AssistantConfig.from_env()
    results: dict[str, dict] = {}

    for start in range(0, len(messages), BATCH_SIZE):
        batch = messages[start : start + BATCH_SIZE]
        batch_ids = {m["id"] for m in batch}
        prompt_messages = [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": _format_batch(batch)},
        ]

        try:
            text = generate_reply(prompt_messages, api_key=config.api_key, model=config.model)
        except OpenAIServiceError:
            text = ""

        parsed = _parse_json_array(text)
        seen_ids = set()
        for item in parsed:
            mid = item.get("id")
            if mid not in batch_ids:
                continue
            seen_ids.add(mid)
            results[mid] = {
                "is_relevant": bool(item.get("relevant")),
                "reason": str(item.get("reason", ""))[:200],
            }

        for mid in batch_ids - seen_ids:
            results[mid] = {"is_relevant": False, "reason": "Classification unavailable."}

    return results
