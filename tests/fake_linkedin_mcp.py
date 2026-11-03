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


@server.tool(name="linkedin.messaging.send", structured_output=True)
def send(context_id: str, request_id: str, message: str, profile_slug: str | None = None, conversation_ref: str | None = None) -> dict[str, Any]:
    note("send:" + (profile_slug or conversation_ref or "?"))
    with open(PATH + ".sent", "a") as f:
        f.write(message + "\n")
    return {"status": "completed", "result": {"outcome": data().get("send_outcome", "verified"), "detail": "done"}}


if __name__ == "__main__":
    server.run("stdio")
