#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["mcp==2.2.0"]
# ///
"""The LinkedIn helper: the working part of the LyteKeepr LinkedIn add-on in the Claude app, on the USER's own computer. It reads their
LinkedIn with the LinkedIn MCP tool (their own login, their own address), hands the messages to their LyteKeepr workspace and sends the
messages they approved. Their LinkedIn login never leaves the computer; LyteKeepr only gives a key that lets it add messages to that
one workspace.

The add-on (lk_calendar/linkedin_addon/server.py) is a small starter that never changes; it runs this file from the user's LyteKeepr
folder and calls addon_run every minute. This file updates itself (update_self) to the version LyteKeepr says is current, checked
against its fingerprint, so a fix reaches every computer without a reinstall.

When to read: straight away the first time (LyteKeepr asks for 3 months), then hourly from 8am to 9pm while the computer is on, and soon
after "Sync now". A returning read looks back as far as LyteKeepr says, at most 7 days. Conversations whose "last activity" has not
changed since the last read are skipped, so LinkedIn sees as few requests as possible."""

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

MCP_PACKAGE = "linkedin-mcp-local==0.16.0"
HOME_DIR = Path(os.environ.get("LK_LINKEDIN_HOME") or (Path.home() / ".lytekeepr-linkedin"))
CONFIG = HOME_DIR / "config.json"
STATE = HOME_DIR / "state.json"
LOCK = HOME_DIR / "running.lock"
LOCK_HOURS = 3
START_HOUR, END_HOUR = 8, 21
EVERY_SECONDS = 3600
MAX_PAGES = 20
PAGE_SIZE = 50
MAX_CONVERSATIONS = 300
MAX_MESSAGES = 100
SEND_BATCH = 10
PAUSE_SECONDS = float(os.environ.get("LK_LINKEDIN_PAUSE", "2"))        # between LinkedIn requests
SEEN_KEPT = 2000
LOGGED_OUT = ("login_required", "attention_required")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


class Gone(Exception):
    """LyteKeepr says this computer is disconnected."""


class Refused(Exception):
    """LyteKeepr refused for now (workspace suspended, for example)."""


def parse_when(text, now):
    """LinkedIn only shows display text ("10:39 PM", "Yesterday", "Monday", "Feb 12"). None when the format is unknown."""
    if not text:
        return None
    t = text.strip().lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        return now.replace(hour=hour, minute=int(m.group(2)), second=0, microsecond=0)
    if t == "today":
        return now
    if t == "yesterday":
        return midnight - timedelta(days=1)
    m = re.match(r"^(\d+)\s*(d|w|mo|months?|y|yr|years?)$", t)
    if m:
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)[0]]
        return midnight - timedelta(days=int(m.group(1)) * days)
    if t in WEEKDAYS:
        return midnight - timedelta(days=(now.weekday() - WEEKDAYS.index(t)) % 7 or 7)
    m = re.match(r"^([a-z]{3})[a-z]*\s+(\d{1,2})(?:,?\s+(\d{4}))?$", t)
    if m and m.group(1) in MONTHS:
        try:
            when = midnight.replace(month=MONTHS[m.group(1)], day=int(m.group(2)))
            if m.group(3):
                return when.replace(year=int(m.group(3)))
            return when.replace(year=when.year - 1) if when > now else when
        except ValueError:
            return None
    return None


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save(path, data):
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    if platform.system() != "Windows":
        tmp.chmod(0o600)
    tmp.replace(path)


def call(cfg, method, path, body=None):
    req = urllib.request.Request(cfg["address"] + "/linkedin-helper/" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg.get("key", "")})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            raise Gone() from exc
        if exc.code in (401, 403):
            raise Refused(str(exc.code)) from exc
        raise


def _mcp_args(*args):
    custom = os.environ.get("LK_LINKEDIN_MCP_COMMAND")
    if custom:
        return json.loads(custom) + list(args)
    own = Path(sys.executable).parent / ("linkedin-mcp.exe" if platform.system() == "Windows" else "linkedin-mcp")
    if own.exists():                           # inside the Claude add-on, the tool is installed next to this Python
        return [str(own), *args]
    uv = os.environ.get("UV") or shutil.which("uv") or "uv"
    return [uv, "tool", "run", "--from", MCP_PACKAGE, "linkedin-mcp", *args]


class LinkedIn:
    """One session with the LinkedIn MCP tool."""

    def __init__(self, session):
        self.session = session

    async def tool(self, name, **args):
        args.update(context_id="lytekeepr", request_id=f"lk-{uuid.uuid4().hex[:12]}")
        result = await asyncio.wait_for(self.session.call_tool(name, args), timeout=120)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):       # mcp 2 / mcp 1 spelling
            said = " ".join(getattr(b, "text", "") for b in getattr(result, "content", None) or []).strip()
            raise RuntimeError(said[:300] or f"{name} failed")
        data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if data:
            return data
        for block in getattr(result, "content", None) or []:
            try:
                return json.loads(getattr(block, "text", "") or "")
            except ValueError:
                continue
        return {}

    async def logged_out(self):
        """Only a clear "log in" counts: the tool says "unverified" until it first opens LinkedIn with the saved login."""
        return (await self.tool("linkedin.session.status")).get("authentication_state") in LOGGED_OUT

    async def recent(self, days):
        """Conversations active in the last `days`; an age it cannot read counts as recent, so nothing is dropped on a guess."""
        now, found, cursor = datetime.now(), [], None
        for _ in range(MAX_PAGES):
            args = {"category": "focused", "page_size": PAGE_SIZE}
            if cursor:
                args["cursor"] = cursor
            result = await self.tool("linkedin.messaging.search", **args)
            page = result.get("conversations") or []
            ages = [parse_when(c.get("last_activity_text"), now) for c in page]
            found += [c for c, w in zip(page, ages) if w is None or (now - w).total_seconds() / 86400 <= days]
            known = [(now - w).total_seconds() / 86400 for w in ages if w is not None]
            more = result.get("pagination") or {}
            cursor = more.get("next_cursor")
            if (known and all(a > days for a in known)) or not more.get("has_more") or not cursor:
                break
            await asyncio.sleep(PAUSE_SECONDS)
        return found

    async def messages(self, ref):
        result = await self.tool("linkedin.messaging.conversation.get", conversation_ref=ref, max_messages=MAX_MESSAGES)
        now, out = datetime.now(), []
        for m in (result.get("conversation") or {}).get("messages") or []:
            when = parse_when(m.get("sent_at_text"), now)
            out.append({"message_ref": m.get("message_ref"), "direction": m.get("direction"), "sender_name": m.get("sender_name"), "text": m.get("text"),
                        "sent_at": when.isoformat() if when else (m.get("sent_at_text") or "")})
        return out


async def read_and_send(cfg, state, days):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    args = _mcp_args("serve", "--transport", "stdio")
    env = dict(os.environ, LINKEDIN_MCP_AUTO_LOGIN_ON_START="false")   # a login window only ever opens from the installer, not on a timer
    report = {"ok": False, "conversations": 0, "failed": 0, "capped": False, "state": "error"}
    seen = state.setdefault("seen", {})
    async with stdio_client(StdioServerParameters(command=args[0], args=args[1:], env=env)) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=60)
            li = LinkedIn(session)
            if await li.logged_out():
                report["state"] = "needs_login"
                return report
            try:
                found = await li.recent(days)
            except Exception:
                if await li.logged_out():         # the saved login turned out to be expired
                    report["state"] = "needs_login"
                    return report
                raise
            fresh = [c for c in found if c.get("conversation_ref") and seen.get(c["conversation_ref"]) != c.get("last_activity_text")]
            report["capped"] = len(fresh) > MAX_CONVERSATIONS
            batch = []

            def flush():
                if batch:
                    call(cfg, "POST", "conversations", {"conversations": batch})
                    for item in batch:
                        seen[item["summary"]["conversation_ref"]] = item["summary"].get("last_activity_text")
                    batch.clear()
                    save(STATE, state)

            for conv in fresh[:MAX_CONVERSATIONS]:
                await asyncio.sleep(PAUSE_SECONDS)
                try:
                    messages = await li.messages(conv["conversation_ref"])
                except Exception:      # noqa: BLE001 - one unreadable conversation must not lose the others; it is retried next time
                    report["failed"] += 1
                    continue
                batch.append({"summary": {k: conv.get(k) for k in ("conversation_ref", "participant_name", "participant_profile_slug", "unread", "last_activity_text")},
                              "messages": messages})
                report["conversations"] += 1
                if len(batch) >= SEND_BATCH:
                    flush()
            flush()
            report["ok"] = not report["failed"]
            report["state"] = "ok" if report["ok"] else "error"
    return report


SEND_PAUSE_SECONDS = float(os.environ.get("LK_LINKEDIN_SEND_PAUSE", "20"))     # between two messages, so LinkedIn sees a person's pace


async def send_waiting(cfg):
    """Send what the USER approved in LyteKeepr, one by one, and report each back. Logged out: back in the queue, nothing tried."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    items = call(cfg, "GET", "outbox").get("messages") or []
    if not items:
        return 0
    args = _mcp_args("serve", "--transport", "stdio")
    env = dict(os.environ, LINKEDIN_MCP_AUTO_LOGIN_ON_START="false")
    sent = 0
    async with stdio_client(StdioServerParameters(command=args[0], args=args[1:], env=env)) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=60)
            li = LinkedIn(session)
            if await li.logged_out():
                for item in items:
                    call(cfg, "POST", f"outbox/{item['id']}", {"outcome": "retry", "detail": "LinkedIn needs a new login"})
                return 0
            for n, item in enumerate(items):
                if n:
                    await asyncio.sleep(SEND_PAUSE_SECONDS)
                try:                    # LinkedIn's tool sends in two steps: an exact draft, then that draft (the key stops a second send)
                    draft = await _prepare(li, item)
                    if not draft:
                        call(cfg, "POST", f"outbox/{item['id']}", {"outcome": "failed", "detail": f"Couldn't find {item.get('name') or 'them'} on your LinkedIn."})
                        continue
                    result = (await li.tool("linkedin.messaging.message.execute", action_id=draft["draft"]["action_id"], payload_hash=draft["draft"]["payload_hash"],
                                            approval_preview=draft["approval_preview"], idempotency_key=f"lk-outbox-{item['id']}")).get("result") or {}
                    outcome = result.get("outcome") if result.get("outcome") in ("verified", "failed", "uncertain") else "uncertain"
                    detail = result.get("detail") or ""
                except Exception as exc:  # noqa: BLE001 - LinkedIn refused or the tool broke: reported, never retried blindly
                    outcome, detail = "failed", _plain(str(exc))
                call(cfg, "POST", f"outbox/{item['id']}", {"outcome": outcome, "detail": detail})
                if outcome == "verified":
                    sent += 1
                    await _bring_in_thread(cfg, li, item)
    return sent


async def _prepare(li, item):
    """LinkedIn's exact draft of the message (nothing is sent yet), trying each way to reach the person until one works: the person's
    own profile (their Message button; LinkedIn can flag a chat that looks one-to-one as a group and refuse it), the one connection
    with exactly that name, then the chat, looked up now (a conversation ref only works in the session that just found it).
    Preparing sends nothing, so trying the next way is safe. None when the person can't be found at all."""
    name = (item.get("name") or "").strip()
    refused = None

    async def routes():
        if item.get("profile_slug"):
            yield {"profile_slug": item["profile_slug"]}
        if name:
            people = (await li.tool("linkedin.connections.search", query=name)).get("people") or []
            same = {p["profile_slug"] for p in people if (p.get("name") or "").strip().lower() == name.lower() and p.get("profile_slug")} - {item.get("profile_slug")}
            if len(same) == 1:
                yield {"profile_slug": same.pop()}
            found = await li.tool("linkedin.messaging.search", query=name, page_size=10)
            chats = [c for c in found.get("conversations") or [] if (c.get("participant_name") or "").strip().lower() == name.lower() and not c.get("is_group")]
            if chats:
                yield {"conversation_ref": chats[0]["conversation_ref"]}

    async for target in routes():
        try:
            return await li.tool("linkedin.messaging.message.prepare", message=item["text"], **target)
        except RuntimeError as exc:
            refused = exc
    if refused:
        raise refused
    return None


def _plain(said):
    """LinkedIn's refusal in words a USER can act on."""
    if "Group conversations" in said:
        return "LinkedIn's tool can't send into this chat (it reads it as a group chat). Please send it on LinkedIn yourself."
    if "message request" in said.lower() or "inmail" in said.lower():
        return "LinkedIn only allows a message request or InMail here. Please send it on LinkedIn yourself."
    return "LinkedIn refused it: " + said[:200]


async def _bring_in_thread(cfg, li, item):
    """The sent message onto the contact's timeline now (with LinkedIn's own id, so the next read doesn't add it twice)."""
    try:
        ref = item.get("conversation_ref")
        summary = None
        for conv in await li.recent(1):
            if (ref and conv.get("conversation_ref") == ref) or (item.get("profile_slug") and conv.get("participant_profile_slug") == item["profile_slug"]) \
                    or conv.get("participant_name") == item.get("name"):
                summary = conv
                break
        if not summary:
            return
        messages = await li.messages(summary["conversation_ref"])
        call(cfg, "POST", "conversations", {"conversations": [{"summary": {k: summary.get(k) for k in ("conversation_ref", "participant_name", "participant_profile_slug", "unread", "last_activity_text")},
                                                               "messages": messages}]})
    except Exception:  # noqa: BLE001 - the next read brings it anyway
        pass


def _stop_tool():
    """The tool keeps a browser running in the background: nothing stays on between reads."""
    if os.environ.get("LK_LINKEDIN_NO_STOP"):
        return
    try:
        subprocess.run(_mcp_args("stop"), capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def update_self(cfg, wanted):
    """LyteKeepr says which version of this helper is current (its fingerprint); a different one is fetched and checked against
    that fingerprint before it replaces this file. Returns True when it replaced it."""
    me = Path(__file__).resolve()
    if not wanted or hashlib.sha256(me.read_bytes()).hexdigest() == wanted:
        return False
    try:
        with urllib.request.urlopen(cfg["address"] + "/linkedin-helper/agent.py", timeout=60) as res:
            new = res.read()
    except OSError:
        return False
    if hashlib.sha256(new).hexdigest() != wanted:
        return False
    tmp = me.with_suffix(".new")
    tmp.write_bytes(new)
    tmp.replace(me)
    print("Helper updated.")
    return True


def tick(force=False, launcher=0):
    """force: read now whatever the hour (the user asked, through Claude). launcher: the add-on starter's version, so LyteKeepr can
    say when a starter is too old."""
    cfg = load(CONFIG)
    if not cfg.get("key"):
        return 0
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < LOCK_HOURS * 3600:
        return 0
    state = load(STATE)
    try:
        plan = call(cfg, "GET", f"plan?launcher={int(launcher)}")
    except Gone:
        CONFIG.unlink(missing_ok=True)
        print("Disconnected in LyteKeepr: the helper has stopped.")
        return 0
    except (Refused, OSError):
        return 0          # suspended or no internet: try again next time
    if update_self(cfg, plan.get("helper")):
        return 0           # the new version runs from the next check on
    if plan.get("outbox"):                   # messages the USER approved go out first, whatever the hour
        LOCK.write_text(str(time.time()))
        try:
            print(f"{datetime.now().isoformat(timespec='seconds')} sent {asyncio.run(send_waiting(cfg))} LinkedIn message(s)")
        except (Gone, Refused):
            return 0
        except Exception as exc:  # noqa: BLE001 - what was claimed is reported or marked by LyteKeepr; reading goes on
            print(f"Sending failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            _stop_tool()
            LOCK.unlink(missing_ok=True)
    now = time.time()
    hour = datetime.now().hour
    due = force or plan.get("sync_now") or (plan.get("first") and not state.get("last_read")) or (START_HOUR <= hour < END_HOUR and now - state.get("last_read", 0) >= EVERY_SECONDS)
    if not due:
        return 0
    LOCK.write_text(str(now))
    state["last_read"] = now
    save(STATE, state)
    try:
        report = asyncio.run(read_and_send(cfg, state, plan["window_days"]))
    except Gone:
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to LyteKeepr as a plain state, tried again next hour
        print(f"Read failed: {type(exc).__name__}", file=sys.stderr)
        report = {"ok": False, "conversations": 0, "failed": 0, "capped": False, "state": "error"}
    finally:
        _stop_tool()
        LOCK.unlink(missing_ok=True)
    state["seen"] = dict(list(state.get("seen", {}).items())[-SEEN_KEPT:])
    save(STATE, state)
    try:
        call(cfg, "POST", "done", report)
    except (Gone, Refused, OSError):
        pass
    print(f"{datetime.now().isoformat(timespec='seconds')} {report['state']}: {report['conversations']} conversation(s)")
    return 0


# ---- the add-on's own work (the starter calls these; they live here so they update with this file)
LINK_FILE = HOME_DIR / "linking.json"
LINK_MINUTES = 15


def _post(address, path, body):
    req = urllib.request.Request(address + "/linkedin-helper/" + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def linked():
    return bool(load(CONFIG).get("key"))


def link(address):
    """Link this computer to the USER's workspace: a LyteKeepr page opens in their browser, they click Approve, this waits for that.
    Returns "linked", "just linked" or a sentence for the user."""
    if linked():
        return "linked"
    pending = load(LINK_FILE)
    if not pending or time.time() - pending.get("at", 0) > LINK_MINUTES * 60:
        status, pending = _post(address, "link/start", {"computer": (platform.node() or "this computer")[:80]})
        if status != 200:
            return "LyteKeepr could not be reached. Check the internet connection; this tries again by itself."
        pending["at"] = time.time()
        save(LINK_FILE, pending)
        print("approve at " + pending["url"], file=sys.stderr, flush=True)     # the USER clicks Approve on LyteKeepr's Home, in their own window
    deadline = time.time() + LINK_MINUTES * 60
    while time.time() < deadline:
        status, out = _post(address, "link/poll", {"link_id": pending["link_id"], "secret": pending["secret"]})
        if status == 200 and out.get("key"):
            save(CONFIG, {"address": address, "key": out["key"]})
            save(STATE, {})
            LINK_FILE.unlink(missing_ok=True)
            log_in()
            return "just linked"
        if status == 410:
            LINK_FILE.unlink(missing_ok=True)
            return "It was not approved in time. Say 'sync my LinkedIn', then click Approve on LyteKeepr's Home."
        time.sleep(3)
    return "Waiting for you to click Approve on LyteKeepr's Home."


def log_in():
    """A LinkedIn window opens; with a saved login it closes by itself."""
    if os.environ.get("LK_LINKEDIN_NO_LOGIN"):     # checks: there is no LinkedIn to log in to
        return
    for args in (("profile", "create"), ("setup",), ("login",)):
        try:
            subprocess.run(_mcp_args(*args), capture_output=True, timeout=1800)
        except (OSError, subprocess.SubprocessError):
            pass


def status(address):
    try:
        req = urllib.request.Request(address + "/linkedin-helper/status", headers={"Authorization": "Bearer " + load(CONFIG).get("key", "")})
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())
    except (OSError, ValueError):
        return {}


def addon_run(address, launcher, force=False):
    """One minute's work: link if needed, send what was approved, read when due; a LinkedIn window at most once a day when logged out."""
    state = link(address)
    if state not in ("linked", "just linked"):
        return state
    tick(force=force or state == "just linked", launcher=launcher)      # right after linking it reads at once, whatever the hour
    if status(address).get("state") == "needs_login":
        st = load(STATE)
        if time.time() - st.get("login_tried", 0) > 86400:
            st["login_tried"] = time.time()
            save(STATE, st)
            log_in()
    return "linked"


def addon_status_text(address):
    if not linked():
        return "Not linked yet. Call sync_linkedin to link this computer to LyteKeepr."
    s = status(address)
    if not s:
        return "Linked, but LyteKeepr could not be reached just now."
    note = {"needs_login": "LinkedIn needs a new login: call sync_linkedin and log in in the window that opens.",
            "error": "The last read did not finish; it tries again within the hour."}.get(s.get("state"), "")
    waiting = f"{s['outbox_waiting']} LinkedIn message(s) waiting to be sent. " if s.get("outbox_waiting") else ""
    return (f"Linked. {s.get('message_count', 0)} LinkedIn messages from {s.get('conversation_count', 0)} people in LyteKeepr. "
            f"Last synced: {s.get('last_synced_at') or 'not yet'}. " + ("" if s.get("first_done") else "The first sync (3 months) is still running. ")
            + waiting + note).strip()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "tick":
        sys.exit(tick())
    print("usage: agent.py tick")
    sys.exit(2)
