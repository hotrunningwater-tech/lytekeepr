"""LyteKeepr LinkedIn, the Claude add-on (built into a .mcpb file by lk_calendar/linkedin_link.py, with this workspace's address in
address.txt). The USER installs it in the Claude app; the app runs it on their computer while it is open.

What it does by itself: the first time, it opens a LyteKeepr page in the browser to link this computer to the USER's workspace (they
click Approve), then a LinkedIn window to log in (closes at once when a saved login works). After that it asks LyteKeepr every minute:
messages the USER approved are sent at once, and LinkedIn is read when due (the same helper and rules as linkedin_helper/agent.py: 3 months first, then hourly 8am-9pm, at most 7 days back).
Claude sees only two tools: sync_linkedin and linkedin_status. Nothing here can send or post on LinkedIn."""

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer          # mcp 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer   # mcp 1, which the LinkedIn tool needs inside the add-on

HERE = Path(__file__).resolve().parent
ADDRESS = (HERE / "address.txt").read_text().strip().rstrip("/")
HOME_DIR = Path(os.environ.get("LK_LINKEDIN_HOME") or (Path.home() / ".lytekeepr-linkedin"))
LINK_FILE = HOME_DIR / "linking.json"
EVERY_SECONDS = 60              # asks LyteKeepr each minute; LinkedIn is only opened when there is something to send or read
LINK_MINUTES = 15
server = MCPServer("LyteKeepr LinkedIn", instructions=(
    "When the user asks to sort, sync, check, update or bring in their LinkedIn, in any wording, call sync_linkedin, then LyteKeepr's "
    "linkedin_inbox, then suggest who to keep and who to remove and do it with LyteKeepr's sort_linkedin_inbox after their yes."))
_busy = threading.Lock()
_note = {"login_tried": 0.0}


def _post(path, body):
    req = urllib.request.Request(ADDRESS + "/linkedin-helper/" + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def helper():
    """The helper code lives in the LyteKeepr folder of this computer, so it can update itself; this add-on brings the first copy."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    mine = HOME_DIR / "agent.py"
    if not mine.exists():
        shutil.copy(HERE / "agent.py", mine)
    spec = importlib.util.spec_from_file_location("lytekeepr_agent", mine)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def linked() -> bool:
    try:
        return bool(json.loads((HOME_DIR / "config.json").read_text()).get("key"))
    except (OSError, ValueError):
        return False


def link() -> str:
    """Link this computer to the USER's workspace: a LyteKeepr page opens, they click Approve, this waits for that."""
    if linked():
        return "linked"
    try:
        pending = json.loads(LINK_FILE.read_text())
    except (OSError, ValueError):
        pending = {}
    if not pending or time.time() - pending.get("at", 0) > LINK_MINUTES * 60:
        status, pending = _post("link/start", {"computer": (platform.node() or "this computer")[:80]})
        if status != 200:
            return "LyteKeepr could not be reached. Check the internet connection; this tries again by itself."
        pending["at"] = time.time()
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        LINK_FILE.write_text(json.dumps(pending))
        if platform.system() != "Windows":
            LINK_FILE.chmod(0o600)
        if os.environ.get("LK_NO_BROWSER"):
            print("approve at " + pending["url"], file=sys.stderr, flush=True)
        else:
            webbrowser.open(pending["url"])
    deadline = time.time() + LINK_MINUTES * 60
    while time.time() < deadline:
        status, out = _post("link/poll", {"link_id": pending["link_id"], "secret": pending["secret"]})
        if status == 200 and out.get("key"):
            agent = helper()
            agent.save(agent.CONFIG, {"address": ADDRESS, "key": out["key"]})
            agent.save(agent.STATE, {})
            LINK_FILE.unlink(missing_ok=True)
            log_in()
            return "just linked"
        if status == 410:
            LINK_FILE.unlink(missing_ok=True)
            return "The LyteKeepr page was not approved in time. Say 'sync my LinkedIn' to get a new one."
        time.sleep(3)
    return "Waiting for you to click Approve on the LyteKeepr page in your browser."


def log_in() -> None:
    """A LinkedIn window opens; with a saved login it closes by itself."""
    if os.environ.get("LK_LINKEDIN_NO_LOGIN"):     # checks: there is no LinkedIn to log in to
        return
    agent = helper()
    for args in (("profile", "create"), ("setup",), ("login",)):
        try:
            subprocess.run(agent._mcp_args(*args), capture_output=True, timeout=1800)
        except (OSError, subprocess.SubprocessError):
            pass


def read(force: bool) -> None:
    if not _busy.acquire(blocking=False):
        return
    try:
        state = link()
        if state in ("linked", "just linked"):
            helper().tick(force=force or state == "just linked")      # right after linking it reads at once, whatever the hour
            if _status().get("state") == "needs_login" and time.time() - _note.get("login_tried", 0) > 86400:
                _note["login_tried"] = time.time()          # a LinkedIn window at most once a day, never every hour
                log_in()
    except Exception as exc:  # noqa: BLE001 - the background loop must keep going
        print(f"LinkedIn read failed: {type(exc).__name__}", file=sys.stderr, flush=True)
    finally:
        _busy.release()


def _loop() -> None:
    while True:
        read(False)
        time.sleep(EVERY_SECONDS)


def _status() -> dict:
    agent = helper()
    try:
        cfg = agent.load(agent.CONFIG)
        req = urllib.request.Request(ADDRESS + "/linkedin-helper/status", headers={"Authorization": "Bearer " + cfg.get("key", "")})
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())
    except (OSError, ValueError, KeyError):
        return {}


@server.tool(name="sync_linkedin", description="Bring the newest LinkedIn messages into LyteKeepr now. The first time it links this computer to "
             "LyteKeepr (the user clicks Approve in their browser) and opens LinkedIn to log in. Afterwards, sort them with LyteKeepr's linkedin_inbox.")
def sync_linkedin() -> str:
    if not linked():
        threading.Thread(target=read, args=(True,), daemon=True).start()
        return "A LyteKeepr page opens in the browser: click Approve there, then log in to LinkedIn in the window that opens. Messages follow in a few minutes."
    threading.Thread(target=read, args=(True,), daemon=True).start()
    return "Reading LinkedIn now. New messages are in LyteKeepr within a few minutes; then use linkedin_inbox to sort the people waiting for review."


@server.tool(name="linkedin_status", description="Whether LinkedIn is linked to LyteKeepr on this computer, when it last synced, and how many messages LyteKeepr has.")
def linkedin_status() -> str:
    if not linked():
        return "Not linked yet. Call sync_linkedin to link this computer to LyteKeepr."
    s = _status()
    if not s:
        return "Linked, but LyteKeepr could not be reached just now."
    state = {"needs_login": "LinkedIn needs a new login: call sync_linkedin and log in in the window that opens.",
             "error": "The last read did not finish; it tries again within the hour."}.get(s.get("state"), "")
    return (f"Linked. {s.get('message_count', 0)} LinkedIn messages from {s.get('conversation_count', 0)} people in LyteKeepr. "
            f"Last synced: {s.get('last_synced_at') or 'not yet'}. " + ("" if s.get("first_done") else "The first sync (3 months) is still running. ") + state).strip()


if __name__ == "__main__":
    threading.Thread(target=_loop, daemon=True).start()
    server.run("stdio")
