"""LinkedIn follow-up sequence engine.

Test-phase scope, per explicit agreement: LinkedIn only (email comes once
this is proven), a short fixed interval instead of real-world cadences, and
no cross-channel cancel yet. See crm/db.py's follow_ups table docstring for
the state machine (active -> awaiting_approval -> active/cancelled/stopped/
completed).

record_outreach() is called after every confirmed LinkedIn send (manual or
scheduled) to start/advance a sequence. run_followup_tick() is the
scheduler entry point: for each sequence whose time has come, it re-pulls
the conversation, and — because LinkedIn gives no reliable per-message
timestamp, only display strings like "TODAY" — determines whether there's
a genuinely new incoming reply the same way normal sync does: by whether
insert_message() actually inserts it (dedup by external_message_id) rather
than by comparing unreliable timestamps.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from assistant.config import AssistantConfig
from crm import db as crm_db
from integrations.linkedin import client as linkedin_client
from services.openai.client import OpenAIServiceError, generate_text

logger = logging.getLogger(__name__)

FOLLOW_UP_INTERVAL_MINUTES = 5  # test-phase interval; real cadences come later
MAX_STAGES = 3
_TICK_SECONDS = 60

CLASSIFY_INSTRUCTIONS = (
    "Classify a single incoming reply to a sales/networking outreach message. "
    "Respond with exactly one word, nothing else: "
    "'positive' if it's a real, engaged reply (interested, asking questions, agreeing "
    "to talk, or any substantive response); "
    "'not_interested' if they clearly decline, say no, or ask to stop being contacted; "
    "'auto_ooo' if it looks automatic (out-of-office/vacation auto-responder) or is too "
    "short/ambiguous to count as a real response (e.g. just an emoji, 'ok', 'thanks')."
)

DRAFT_INSTRUCTIONS = (
    "Write ONE short, natural follow-up message continuing an outreach conversation "
    "that has gone unanswered. Plain text only, no subject line, 2-3 sentences max, "
    "friendly and low-pressure — must not sound like an automated drip campaign. "
    "Output only the message text, nothing else."
)


def _classify_reply(reply_text: str, api_key: str | None, model: str) -> str:
    if not api_key:
        return "auto_ooo"
    try:
        result = generate_text(
            reply_text, api_key=api_key, model=model, instructions=CLASSIFY_INSTRUCTIONS
        ).strip().lower()
    except OpenAIServiceError as exc:
        # Fail safe: an API hiccup should never silently cancel a real
        # sequence — treat it like "not a real answer yet" and try again.
        # Logged (not silent) so a run of these is diagnosable, not guessed at.
        logger.warning("classify_reply: OpenAI call failed, defaulting to auto_ooo: %s", exc)
        return "auto_ooo"
    if "positive" in result:
        return "positive"
    if "not_interested" in result or "not interested" in result:
        return "not_interested"
    return "auto_ooo"


def _generate_followup_text(person_name: str, history_text: str, stage: int, api_key: str | None, model: str) -> str:
    fallback = f"Hi {person_name} — just following up on my last message, wanted to check in."
    if not api_key:
        return fallback
    prompt = (
        f"Contact: {person_name}\n"
        f"This will be follow-up message #{stage}.\n\n"
        f"Conversation so far:\n{history_text}\n\n"
        "Write the next follow-up message."
    )
    try:
        text = generate_text(prompt, api_key=api_key, model=model, instructions=DRAFT_INSTRUCTIONS).strip()
        if not text:
            logger.warning("generate_followup_text: OpenAI returned an empty response, using fallback text")
        return text or fallback
    except OpenAIServiceError as exc:
        logger.warning("generate_followup_text: OpenAI call failed, using fallback text: %s", exc)
        return fallback


def record_outreach(person_id: int, channel: str, conversation_ref: str | None, follow_up_type: str = "new_lead") -> None:
    """Call after any confirmed send. Starts a sequence if none is active
    for this person+channel, or advances the existing one to the next
    stage/interval."""
    if channel != "linkedin":
        return  # test-phase scope
    conn = crm_db.get_connection()
    try:
        existing = crm_db.get_active_follow_up(conn, person_id, channel)
        next_at = (datetime.now() + timedelta(minutes=FOLLOW_UP_INTERVAL_MINUTES)).isoformat()
        if existing:
            if existing["stage"] >= MAX_STAGES:
                crm_db.end_follow_up(conn, existing["follow_up_id"], "completed", "Reached max follow-up stages.")
                logger.warning("record_outreach: person_id=%s follow_up_id=%s completed (max stages)", person_id, existing["follow_up_id"])
            else:
                crm_db.advance_follow_up(conn, existing["follow_up_id"], next_at)
                logger.warning("record_outreach: person_id=%s follow_up_id=%s advanced, next_at=%s", person_id, existing["follow_up_id"], next_at)
        else:
            new_id = crm_db.start_follow_up(conn, person_id, channel, conversation_ref, next_at, follow_up_type)
            logger.warning("record_outreach: person_id=%s started new follow_up_id=%s, next_at=%s", person_id, new_id, next_at)
        conn.commit()
    finally:
        conn.close()


def _ingest_and_find_new_incoming(conn, person_id: int, conversation_ref: str) -> list[dict]:
    """Re-pulls the conversation, writes every message into the CRM (same
    dedup as normal sync), and returns just the ones that were genuinely
    new AND incoming — the only ones that count as "a reply since last
    outreach"."""
    conv_res = linkedin_client.get_conversation(conversation_ref, max_messages=10)
    if not conv_res.get("ok"):
        return []
    messages = ((conv_res.get("result") or {}).get("conversation") or {}).get("messages") or []

    rows = conn.execute(
        "SELECT account_id FROM channel_accounts WHERE person_id = ? AND channel = 'linkedin' LIMIT 1",
        (person_id,),
    ).fetchone()
    if not rows:
        return []
    account_id = rows["account_id"]

    new_incoming = []
    for msg in messages:
        was_new = crm_db.insert_message(
            conn,
            person_id=person_id,
            account_id=account_id,
            thread_id=conversation_ref,
            channel="linkedin",
            sender=msg.get("sender_name"),
            direction=msg.get("direction", "incoming"),
            message_text=msg.get("text"),
            timestamp=msg.get("sent_at_text"),
            external_message_id=msg.get("message_ref"),
        )
        if was_new and msg.get("direction") == "incoming":
            new_incoming.append(msg)
    return new_incoming


def _process_due_follow_up(row, api_key: str | None, model: str) -> None:
    person_id = row["person_id"]
    conversation_ref = row["conversation_ref"]
    person_name = row["person_name"]
    logger.warning(
        "processing due follow_up_id=%s person_id=%s person_name=%s stage=%s conversation_ref=%s",
        row["follow_up_id"], person_id, person_name, row["stage"], conversation_ref,
    )
    if not conversation_ref:
        logger.warning("follow_up_id=%s has no conversation_ref — skipping, will retry next tick", row["follow_up_id"])
        return

    run_id = crm_db.new_run_id()
    conn = crm_db.get_connection()
    try:
        new_incoming = _ingest_and_find_new_incoming(conn, person_id, conversation_ref)
        crm_db.log_activity(run_id, 1, "followup", "check_for_reply", detail=f"{person_name}: {len(new_incoming)} new incoming message(s)", conn=conn)
        conn.commit()
        logger.warning("follow_up_id=%s: %d new incoming message(s) found", row["follow_up_id"], len(new_incoming))

        if new_incoming:
            reply_text = "\n".join(m.get("text", "") for m in new_incoming if m.get("text"))
            outcome = _classify_reply(reply_text, api_key, model)
            crm_db.log_activity(run_id, 2, "followup", "classify_reply", detail=f"{person_name}: classified as {outcome!r} — text={reply_text[:300]!r}", conn=conn)
            conn.commit()
            logger.warning("follow_up_id=%s: reply classified as %r — reply_text=%r", row["follow_up_id"], outcome, reply_text[:200])
            if outcome == "positive":
                crm_db.end_follow_up(conn, row["follow_up_id"], "cancelled", "Got a real reply.")
                conn.commit()
                return
            if outcome == "not_interested":
                crm_db.end_follow_up(conn, row["follow_up_id"], "stopped", "Asked not to be contacted.")
                conn.commit()
                return
            # auto_ooo / ambiguous: NOT the same as silence. Someone did
            # reply, just not clearly positive/negative — auto-drafting a
            # generic "no reply" nudge here would (and did) talk past
            # whatever they actually said. Pause instead and let a human
            # write the real reply.
            crm_db.end_follow_up(conn, row["follow_up_id"], "needs_review", f"Got an unclear reply, needs a human look: {reply_text[:300]}")
            conn.commit()
            return

        history_rows = crm_db.list_messages_for_person(conn, "linkedin", person_id)
        history_text = "\n".join(
            f"{'You' if m['direction'] == 'outgoing' else person_name}: {m['message_text']}"
            for m in history_rows[-10:]
        )
        draft_text = _generate_followup_text(person_name, history_text, row["stage"] + 1, api_key, model)
        crm_db.log_activity(run_id, 3, "followup", "generate_draft", detail=f"{person_name}: stage {row['stage'] + 1} draft: {draft_text[:300]!r}", conn=conn)
        conn.commit()
        logger.warning("follow_up_id=%s: generated draft (stage %d): %r", row["follow_up_id"], row["stage"] + 1, draft_text[:200])
        from assistant.linkedin_send_flow import create_pending_send  # local import: avoids a circular import with linkedin_send_flow -> followup_engine
        stage_result = create_pending_send(person_name, draft_text)
        logger.warning("follow_up_id=%s: create_pending_send result: %r", row["follow_up_id"], stage_result)
        crm_db.set_follow_up_awaiting_approval(conn, row["follow_up_id"])
        conn.commit()
    finally:
        conn.close()


def _heal_stuck_awaiting_approval(conn) -> int:
    """A draft staged for approval has a TTL meant for an interactive chat
    session (see linkedin_send_flow.PENDING_SEND_TTL_MINUTES) — a
    scheduler-generated one can easily sit unseen past that and expire
    before anyone approves it. When that happens the follow_ups row is
    left stuck at 'awaiting_approval' forever, since nothing else ever
    resets it. Self-heal here: if a row is awaiting_approval but there's
    no live pending draft for that person, put it back to 'active' so the
    next due-check regenerates a fresh one. Returns how many were healed."""
    from assistant.linkedin_send_flow import load_pending_send  # local import: avoids circular import

    pending = load_pending_send()
    pending_person = pending.get("person_name") if pending else None

    stuck = conn.execute(
        "SELECT * FROM follow_ups WHERE status = 'awaiting_approval'"
    ).fetchall()
    healed = 0
    for row in stuck:
        conn2 = crm_db.get_connection()
        try:
            person_name_row = conn2.execute(
                "SELECT name FROM people WHERE person_id = ?", (row["person_id"],)
            ).fetchone()
        finally:
            conn2.close()
        person_name = person_name_row["name"] if person_name_row else None
        if person_name == pending_person:
            continue  # still genuinely waiting on this one, leave it
        logger.warning(
            "follow_up_id=%s for person_id=%s was stuck at awaiting_approval with no live draft — resetting to active",
            row["follow_up_id"], row["person_id"],
        )
        conn.execute(
            "UPDATE follow_ups SET status = 'active', next_follow_up_at = ?, updated_at = ? WHERE follow_up_id = ?",
            (datetime.now().isoformat(), datetime.now().isoformat(), row["follow_up_id"]),
        )
        healed += 1
    return healed


def run_followup_tick() -> None:
    config = AssistantConfig.from_env()
    conn = crm_db.get_connection()
    try:
        healed = _heal_stuck_awaiting_approval(conn)
        conn.commit()
        due = crm_db.list_due_follow_ups(conn, datetime.now().isoformat())
    finally:
        conn.close()
    logger.warning("follow-up tick: healed=%d due=%d", healed, len(due))
    for row in due:
        try:
            _process_due_follow_up(row, config.api_key, config.model)
        except Exception:  # noqa: BLE001 - one bad sequence should never kill the loop
            logger.exception("follow-up tick failed for follow_up_id=%s", row["follow_up_id"])


def _scheduler_loop() -> None:
    while True:
        time.sleep(_TICK_SECONDS)
        try:
            run_followup_tick()
        except Exception:  # noqa: BLE001
            logger.exception("follow-up scheduler tick crashed")


def start_scheduler() -> None:
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
