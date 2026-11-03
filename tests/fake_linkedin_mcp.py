"""A stand-in for the LinkedIn MCP tool, for checks: no browser and no LinkedIn. Answers from the JSON file in FAKE_LINKEDIN and
writes each tool it was asked for to FAKE_LINKEDIN.calls."""

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

PATH = os.environ["FAKE_LINKEDIN"]
server = MCPServer("fake-linkedin")


def data():
    with open(PATH) as f:
        return json.load(f)


def note(name):
    with open(PATH + ".calls", "a") as f:
        f.write(name + "\n")


@server.tool(name="linkedin.session.status", structured_output=True)
def status(context_id: str, request_id: str) -> dict[str, Any]:
    note("status")
    auth = data().get("auth", "unverified")           # like the real tool: "unverified" until it first opens LinkedIn
    if auth == "expired":                             # the saved login only turns out to be expired once LinkedIn is opened
        auth = "login_required" if "search" in open(PATH + ".calls").read().split() else "unverified"
    return {"authentication_state": auth}


@server.tool(name="linkedin.messaging.search", structured_output=True)
def search(context_id: str, request_id: str, category: str, page_size: int, cursor: str | None = None) -> dict[str, Any]:
    note("search")
    if data().get("auth") == "expired":
        raise ValueError("login required")
    return {"conversations": data()["conversations"], "pagination": {"has_more": False}}


@server.tool(name="linkedin.messaging.conversation.get", structured_output=True)
def conversation(context_id: str, request_id: str, conversation_ref: str, max_messages: int) -> dict[str, Any]:
    note("get:" + conversation_ref)
    d = data()
    if conversation_ref in d.get("broken", []):
        raise ValueError("cannot read this one")
    return {"conversation": {"messages": d["messages"][conversation_ref]}}


@server.tool(name="linkedin.messaging.message.prepare", structured_output=True)
def prepare(context_id: str, request_id: str, message: str, profile_slug: str | None = None, conversation_ref: str | None = None) -> dict[str, Any]:
    note("prepare:" + (profile_slug or conversation_ref or "?"))
    action_id = "action_" + str(abs(hash((message, profile_slug, conversation_ref))))
    preview = {"action_id": action_id, "action_type": "message_send", "summary": message, "payload_hash": "h" + action_id}
    return {"status": "ready_for_confirmation", "draft": {"action_id": action_id, "payload_hash": "h" + action_id, "payload": {"message": message}},
            "approval_preview": preview}


@server.tool(name="linkedin.messaging.message.execute", structured_output=True)
def execute(context_id: str, request_id: str, action_id: str, payload_hash: str, approval_preview: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    if approval_preview.get("action_id") != action_id or approval_preview.get("payload_hash") != payload_hash:
        raise ValueError("Approval preview does not match")
    keys = PATH + ".keys"
    done = open(keys).read().split() if os.path.exists(keys) else []
    if idempotency_key in done:                       # like the real tool: the same key never sends twice
        return {"status": "completed", "replayed": True, "result": {"outcome": "verified", "detail": "already sent"}}
    note("send:" + idempotency_key)
    with open(keys, "a") as f:
        f.write(idempotency_key + "\n")
    with open(PATH + ".sent", "a") as f:
        f.write(approval_preview["summary"] + "\n")
    return {"status": "completed", "result": {"outcome": data().get("send_outcome", "verified"), "detail": "done"}}


if __name__ == "__main__":
    server.run("stdio")
