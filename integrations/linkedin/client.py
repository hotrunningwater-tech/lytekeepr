"""
MCP client wrapper for the LinkedIn MCP server
(prakharagarwal-dev/linkedin-mcp-server, vendored under
mcp-integrations/linkedin-mcp-server with its own venv).

Important: this server opens a real, visible login browser window the
moment its process starts — before any tool is called. That's a property
of the server itself, not something triggered here. Every function in this
module is a deliberate UI-initiated action; none of them are called at
import time or on a timer.
"""

import asyncio
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_SERVER_DIR = Path(__file__).resolve().parents[2] / "mcp-integrations" / "linkedin-mcp-server"
_SERVER_BIN = _SERVER_DIR / ".venv" / "bin" / "linkedin-mcp"


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=str(_SERVER_BIN),
        args=["serve", "--transport", "stdio"],
        cwd=str(_SERVER_DIR),
    )


def _not_installed() -> dict:
    return {
        "installed": False,
        "server_reachable": False,
        "connected": False,
        "error": f"linkedin-mcp is not installed (expected at {_SERVER_BIN})",
    }


async def _call_tool(tool_name: str, arguments: dict) -> dict:
    # The actual tool call regularly succeeds while the stdio/session
    # teardown that follows (closing the subprocess connection to this
    # server's persistent runtime) raises inside the async context
    # managers' __aexit__. That would otherwise mask a perfectly good
    # result with a useless "unhandled errors in a TaskGroup" error, so
    # the result is captured in `outcome` before cleanup runs, and a
    # cleanup-only failure is ignored once we already have data.
    outcome: dict = {}
    try:
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=20)
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments), timeout=30
                )
                if result.structured_content:
                    outcome["data"] = result.structured_content
                else:
                    # Fall back to the first text block if the tool has no
                    # structured output for some reason.
                    for block in result.content:
                        if getattr(block, "type", None) == "text":
                            outcome["data"] = {"raw_text": block.text}
                            break
                    else:
                        outcome["data"] = {}
    except Exception:
        if "data" in outcome:
            return outcome["data"]
        raise
    return outcome["data"]


async def _get_status_async() -> dict:
    if not _SERVER_BIN.exists():
        return _not_installed()

    try:
        data = await _call_tool(
            "linkedin.session.status",
            {"context_id": "lytekeepr", "request_id": f"status-{uuid.uuid4().hex[:12]}"},
        )
        auth_state = data.get("authentication_state")
        return {
            "installed": True,
            "server_reachable": True,
            "connected": auth_state == "authenticated",
            "authentication_state": auth_state,
            "login_browser_open": data.get("login_browser_open"),
            "status_message": data.get("status_message"),
        }
    except Exception as exc:  # noqa: BLE001 - surface any failure as status, not a crash
        return {
            "installed": True,
            "server_reachable": False,
            "connected": False,
            "error": str(exc),
        }


def get_status() -> dict:
    """Synchronous wrapper — server.py routes are plain sync Flask views.

    Calling this starts the LinkedIn MCP server process. If no LinkedIn
    session is saved yet, the server itself opens a real login browser
    window for the user to sign in — that's expected here, since this is
    only ever invoked from an explicit UI action (clicking the LinkedIn
    tab), never automatically.
    """
    return asyncio.run(_get_status_async())


async def _search_messages_async(page_size: int, cursor: str | None) -> dict:
    if not _SERVER_BIN.exists():
        return _not_installed()

    try:
        args = {
            "context_id": "lytekeepr",
            "request_id": f"search-{uuid.uuid4().hex[:12]}",
            "category": "focused",
            "page_size": page_size,
        }
        if cursor:
            args["cursor"] = cursor
        data = await _call_tool("linkedin.messaging.search", args)
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def search_messages(page_size: int = 10, cursor: str | None = None) -> dict:
    """Fetch one page of focused-inbox conversations, most recent first.
    Only meaningful once get_status() reports connected: true. Pass the
    cursor from a previous result's result.next_cursor to page further back."""
    return asyncio.run(_search_messages_async(page_size, cursor))


async def _get_conversation_async(conversation_ref: str, max_messages: int) -> dict:
    if not _SERVER_BIN.exists():
        return _not_installed()

    try:
        data = await _call_tool(
            "linkedin.messaging.conversation.get",
            {
                "context_id": "lytekeepr",
                "request_id": f"conv-{uuid.uuid4().hex[:12]}",
                "conversation_ref": conversation_ref,
                "max_messages": max_messages,
            },
        )
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def get_conversation(conversation_ref: str, max_messages: int = 30) -> dict:
    """Fetch the message history for one conversation by conversation_ref
    (as returned by search_messages())."""
    return asyncio.run(_get_conversation_async(conversation_ref, max_messages))


async def _search_people_async(query: str) -> dict:
    if not _SERVER_BIN.exists():
        return _not_installed()

    try:
        data = await _call_tool(
            "linkedin.people.search",
            {
                "context_id": "lytekeepr",
                "request_id": f"peoplesearch-{uuid.uuid4().hex[:12]}",
                "query": query,
                "page_size": 5,
            },
        )
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def search_people(query: str) -> dict:
    """Search LinkedIn People by name/keywords. Used to resolve a profile_slug
    for a contact we only know by display name (messaging never gives us
    one directly)."""
    return asyncio.run(_search_people_async(query))


async def _get_person_async(profile_slug: str) -> dict:
    if not _SERVER_BIN.exists():
        return _not_installed()

    try:
        data = await _call_tool(
            "linkedin.people.get",
            {
                "context_id": "lytekeepr",
                "request_id": f"person-{uuid.uuid4().hex[:12]}",
                "profile_slug": profile_slug,
                "sections": ["all"],
            },
        )
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def get_person(profile_slug: str) -> dict:
    """Fetch one member's visible profile by slug (as returned by
    search_people())."""
    return asyncio.run(_get_person_async(profile_slug))


async def _send_message_async(message: str, conversation_ref: str | None, profile_slug: str | None) -> dict:
    if not _SERVER_BIN.exists():
        return _not_installed()

    args = {
        "context_id": "lytekeepr",
        "request_id": f"send-{uuid.uuid4().hex[:12]}",
        "message": message,
    }
    if profile_slug:
        args["profile_slug"] = profile_slug
    elif conversation_ref:
        args["conversation_ref"] = conversation_ref

    try:
        data = await _call_tool("linkedin.messaging.send", args)
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def send_message(message: str, conversation_ref: str | None = None, profile_slug: str | None = None) -> dict:
    """Send a real, live message to a LinkedIn contact. Prefer profile_slug
    when known — it targets the person's own Message button directly and
    lands in a normal 1:1, whereas an existing conversation_ref can be
    rejected if LinkedIn has that thread flagged as a group conversation
    (even one that looks like a plain 1:1 in the UI). Exactly one of
    conversation_ref/profile_slug should be passed. This is a write action
    with real, visible-to-the-recipient consequences — every caller of this
    function must already have explicit user confirmation before reaching
    here (see assistant/linkedin_send_flow.py)."""
    return asyncio.run(_send_message_async(message, conversation_ref, profile_slug))
