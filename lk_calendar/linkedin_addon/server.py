"""LyteKeepr LinkedIn, the Claude add-on: a small starter that should never need changing (built into a .mcpb file by
lk_calendar/linkedin_link.py, with this workspace's address in address.txt). The USER installs it once in the Claude app.

All the work (linking to the workspace, logging in, reading, sending) lives in agent.py in the user's LyteKeepr folder, which updates
itself from LyteKeepr. This starter only runs it every minute and answers Claude's two tools with it. LAUNCHER goes up only when this file
must change; LyteKeepr then shows "update the add-on" on the LinkedIn tab."""

import importlib.util
import os
import shutil
import sys
import threading
import time
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer          # mcp 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer   # mcp 1, which the LinkedIn tool needs inside the add-on

LAUNCHER = 2
HERE = Path(__file__).resolve().parent
ADDRESS = (HERE / "address.txt").read_text().strip().rstrip("/")
HOME_DIR = Path(os.environ.get("LK_LINKEDIN_HOME") or (Path.home() / ".lytekeepr-linkedin"))
EVERY_SECONDS = 60
server = MCPServer("LyteKeepr LinkedIn", instructions=(
    "When the user asks to sort, sync, check, update or bring in their LinkedIn, in any wording, call sync_linkedin, then LyteKeepr's "
    "linkedin_inbox, then suggest who to keep and who to remove and do it with LyteKeepr's sort_linkedin_inbox after their yes. "
    "To send a LinkedIn message, use LyteKeepr's send_linkedin_message."))
_busy = threading.Lock()


def helper():
    """The working part, fresh from the user's LyteKeepr folder each time (so its own updates take effect at once). Until this computer
    is linked, the copy that came with this add-on is used, so a new install always starts with the newest one."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    mine = HOME_DIR / "agent.py"
    if not mine.exists() or not (HOME_DIR / "config.json").exists():
        shutil.copy(HERE / "agent.py", mine)
    spec = importlib.util.spec_from_file_location("lytekeepr_agent", mine)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(force: bool) -> None:
    if not _busy.acquire(blocking=False):
        return
    try:
        helper().addon_run(ADDRESS, LAUNCHER, force)
    except Exception as exc:  # noqa: BLE001 - the minute check must keep going
        print(f"LinkedIn check failed: {type(exc).__name__}", file=sys.stderr, flush=True)
    finally:
        _busy.release()


def _loop() -> None:
    while True:
        run(False)
        time.sleep(EVERY_SECONDS)


@server.tool(name="sync_linkedin", description="Bring the newest LinkedIn messages into LyteKeepr now. The first time it links this computer to "
             "LyteKeepr (the user clicks Approve in their browser) and opens LinkedIn to log in. Afterwards, sort them with LyteKeepr's linkedin_inbox.")
def sync_linkedin() -> str:
    first = not (HOME_DIR / "config.json").exists()
    threading.Thread(target=run, args=(True,), daemon=True).start()
    if first:
        return "A LyteKeepr page opens in the browser: click Approve there, then log in to LinkedIn in the window that opens. Messages follow in a few minutes."
    return "Reading LinkedIn now. New messages are in LyteKeepr within a few minutes; then use linkedin_inbox to sort the people waiting for review."


@server.tool(name="linkedin_status", description="Whether LinkedIn is linked to LyteKeepr on this computer, when it last synced, how many messages "
             "LyteKeepr has, and LinkedIn messages still waiting to be sent.")
def linkedin_status() -> str:
    return helper().addon_status_text(ADDRESS)


if __name__ == "__main__":
    threading.Thread(target=_loop, daemon=True).start()
    server.run("stdio")
