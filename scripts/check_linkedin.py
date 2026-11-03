#!/usr/bin/env python3
"""LinkedIn check: run before every deploy (deploy_calendar.sh does). Exit code 0 = fine, 1 = a problem.

It builds two brand-new workspaces in a temporary folder (never the real data), starts the app and runs the real helper program
against it, with a fake LinkedIn tool: no browser, nothing reaches LinkedIn. It proves that
  - connecting needs the risk accepted and a code that works once; team members cannot do it;
  - the first read covers 3 months, a returning read at most 7 days, unchanged conversations are not read again;
  - messages land only in the workspace the key belongs to; a thread with no profile link does not double a known contact;
  - a logged-out or failed read does not move "last synced", and a disconnected helper stops for good.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-linkedin-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({
    "LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy-calendar.db"),
    "LYTEKEEPR_PASSWORD": "linkedin-test", "LYTEKEEPR_SECRET_KEY": "linkedin-test",
    "LYTEKEEPR_ADMIN_EMAIL": "owner-a@example.com", "LYTEKEEPR_SUPER_ADMINS": "console-owner@example.com",
    "LK_GMAIL_DAILY": "0", "LK_GOOGLE_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": "",
})
sys.path.insert(0, str(APP_DIR))

import profiles.store as _profiles  # noqa: E402

if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print(f"LINKEDIN CHECK REFUSED TO RUN: the app points at {_profiles.PROFILE_ROOT}, not the temporary folder.")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)

import auth_gate  # noqa: E402,F401
import server  # noqa: E402
from integrations.linkedin import storage  # noqa: E402
from lk_calendar import directory, linkedin_link  # noqa: E402
from lk_calendar import superadmin as _console  # noqa: E402
from profiles.store import use_profile  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

_console._alert = lambda *args: None
app = server.app
problems = []
AGENT = APP_DIR / "lk_calendar" / "linkedin_helper" / "agent.py"
FAKE = APP_DIR / "tests" / "fake_linkedin_mcp.py"
LINKEDIN = TMP / "fake-linkedin.json"
HELPER_HOME = TMP / "helper"


def check(ok, text):
    if not ok:
        problems.append(text)


def fake(**data):
    LINKEDIN.write_text(json.dumps(data))
    Path(str(LINKEDIN) + ".calls").write_text("")


def calls():
    return Path(str(LINKEDIN) + ".calls").read_text().split()


def tick():
    env = dict(os.environ, LK_LINKEDIN_HOME=str(HELPER_HOME), LK_LINKEDIN_PAUSE="0", LK_LINKEDIN_SEND_PAUSE="0", FAKE_LINKEDIN=str(LINKEDIN), LK_LINKEDIN_NO_STOP="1",
               LK_LINKEDIN_MCP_COMMAND=json.dumps([sys.executable, str(FAKE)]))
    return subprocess.run([sys.executable, str(AGENT), "tick"], env=env, capture_output=True, text=True, timeout=120)


def helper_run(*args):
    env = dict(os.environ, LK_LINKEDIN_HOME=str(HELPER_HOME))
    return subprocess.run([sys.executable, str(AGENT), *args], env=env, capture_output=True, text=True, timeout=60)


def make_read_due():
    state = json.loads((HELPER_HOME / "state.json").read_text())
    state["last_read"] = 0
    (HELPER_HOME / "state.json").write_text(json.dumps(state))
    linkedin_link.request_sync(ws_a)           # also makes the check independent of the time of day


def names(c):
    return [x["participant_name"] for s in ("trusted", "pending_review") for x in c.get(f"/api/linkedin/crm/conversations?status={s}").get_json()["conversations"]]


def message(ref, text, direction="incoming", when="9:00 AM"):
    return {"message_ref": ref, "direction": direction, "sender_name": "x", "text": text, "sent_at_text": when}


a_admin, b_admin, a_member = app.test_client(), app.test_client(), app.test_client()


def google_login(c, email):
    server.login_identity = lambda code: {"email": email, "email_verified": True, "name": email.split("@")[0]}
    state = server._oauth_state_signer().dumps("google-login")
    return c.get(f"/auth/google/callback?state={state}&code=test")


def main():
    global ws_a
    assert a_admin.post("/login", data={"password": "linkedin-test"}).status_code == 302
    google_login(b_admin, "owner-b@example.com")
    accounts = {x["owner_email"]: x for x in directory.list_accounts()}
    directory.set_status(accounts["owner-b@example.com"]["id"], "active")
    ws_a, ws_b = accounts["owner-a@example.com"]["profile_id"], accounts["owner-b@example.com"]["profile_id"]
    a_admin.post("/api/team", json={"email": "member-a@example.com"})
    google_login(a_member, "member-a@example.com")

    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{httpd.server_port}"

    # ---- connecting
    check(a_admin.get("/api/me").get_json().get("features", {}).get("linkedin") is True, "LinkedIn is not on for the admin")
    check(b_admin.get("/api/me").get_json().get("features", {}).get("linkedin") is True, "LinkedIn is not on for a customer workspace")
    check(a_admin.post("/api/linkedin/link/code", json={}).status_code == 400, "a code was given without accepting the risk")
    for path in ("/api/linkedin/status", "/api/linkedin/crm/conversations"):
        check(a_member.get(path).status_code == 403, f"a team member could open {path}")
    check(a_member.post("/api/linkedin/link/code", json={"accepted_risk": True}).status_code == 403, "a team member could connect LinkedIn")
    check(app.test_client().get("/api/linkedin/status").status_code == 401, "the status answers without a sign-in")
    lines = a_admin.post("/api/linkedin/link/code", json={"accepted_risk": True}).get_json()
    check(lines["code"] in lines["mac"] and lines["code"] in lines["windows"], "the lines to paste do not carry the code")
    for name in ("install.sh", "install.ps1"):
        text = app.test_client().get(f"/linkedin-helper/{name}").get_data(as_text=True)
        check("__" not in text.replace("$", "") and "linkedin-mcp-local==" in text, f"{name} still has blanks in it")
    check(helper_run("pair", "WRONGCODE1", address).returncode == 1, "a wrong code was accepted")
    check(helper_run("pair", lines["code"], address).returncode == 0, "the right code was refused")
    check(helper_run("pair", lines["code"], address).returncode == 1, "a code worked twice")
    config = json.loads((HELPER_HOME / "config.json").read_text())
    check(config["key"] not in (TMP / "profiles" / "directory.db").read_bytes().decode("latin1"), "the helper's key is stored readable")
    status = a_admin.get("/api/linkedin/status").get_json()
    check(status["connected"] and not status["first_done"], f"status after connecting is wrong: {status}")

    # ---- the first read: 3 months
    with use_profile(ws_a):
        from crm import db as crm_db
        crm_db.init_db()
        conn = crm_db.get_connection()
        known_person, _ = crm_db.get_or_create_person_for_channel(conn, "linkedin", "known-slug", "Known Lead")
        conn.commit()
        conn.close()
    old = (datetime.now() - timedelta(days=60)).strftime("%b %-d %Y").replace(" 20", ", 20")
    too_old = (datetime.now() - timedelta(days=150)).strftime("%b %-d %Y").replace(" 20", ", 20")
    conversations = [
        {"conversation_ref": "c1", "participant_name": "New Person", "participant_profile_slug": "new-person", "unread": True, "last_activity_text": "9:00 AM"},
        {"conversation_ref": "c2", "participant_name": "Two Months", "participant_profile_slug": "two-months", "last_activity_text": old},
        {"conversation_ref": "c3", "participant_name": "Five Months", "participant_profile_slug": "five-months", "last_activity_text": too_old},
        {"conversation_ref": "c4", "participant_name": "Known Lead", "participant_profile_slug": None, "last_activity_text": "Yesterday"},
        {"conversation_ref": "c5", "participant_name": "Cold Recruiter", "participant_profile_slug": "cold-recruiter", "last_activity_text": "Yesterday"},
        {"conversation_ref": "c6", "participant_name": "Real Prospect", "participant_profile_slug": "real-prospect", "last_activity_text": "Yesterday"},
    ]
    messages = {"c1": [message("m1", "zzleak first", "outgoing")], "c2": [message("m2", "two months ago", "outgoing", old)],
                "c3": [message("m3", "five months ago", "outgoing", too_old)], "c4": [message("m4", "from a known lead")],
                "c5": [message("m6", "great opportunity for you")], "c6": [message("m7", "interested in a pilot")]}
    fake(conversations=conversations, messages=messages)
    run = tick()
    check(run.returncode == 0, f"the helper crashed: {run.stderr[-400:]}")
    got = names(a_admin)
    check({"New Person", "Two Months", "Known Lead"} <= set(got), f"the first read missed someone: {got}")
    check("Five Months" not in got, "the first read went past 3 months")
    check(got.count("Known Lead") == 1, "a thread with no profile link doubled a known contact")
    with use_profile(ws_a):
        conn = crm_db.get_connection()
        check(len(crm_db.list_messages_for_person(conn, "linkedin", known_person)) == 1, "the known contact did not get the thread")
        conn.close()
    check(b"zzleak first" not in b_admin.get("/api/linkedin/crm/conversation/1").get_data() and not names(b_admin), "messages reached another workspace")
    status = a_admin.get("/api/linkedin/status").get_json()
    check(status["first_done"] and status["last_synced_at"] and status["state"] == "ok", f"status after the first read is wrong: {status}")

    # ---- returning reads
    fake(conversations=conversations, messages=messages)
    tick()
    check(calls() == [], f"it read LinkedIn again within the hour: {calls()}")
    make_read_due()
    fake(conversations=conversations, messages=messages)
    tick()
    check(not [c for c in calls() if c.startswith("get:")], f"unchanged conversations were read again: {calls()}")
    check(a_admin.get("/api/linkedin/status").get_json()["sync_requested"] is False, "Sync now stayed on after a read")
    with use_profile(ws_a):
        state = storage.load()
        state["last_synced_at"] = (datetime.now() - timedelta(days=30)).isoformat()
        storage.save(state)
        check(linkedin_link.plan()["window_days"] == 7, "a return after 30 days does not stop at 7 days")
        state["last_synced_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
        storage.save(state)
        check(1 < linkedin_link.plan()["window_days"] < 2, "a return after 2 hours looks back too far")
        before = storage.load()["last_synced_at"]

    # ---- logged out, then a failed read: "last synced" stays, the state says why
    make_read_due()
    fake(auth="login_required", conversations=conversations, messages=messages)
    tick()
    check(calls() == ["status"], f"it kept going while logged out: {calls()}")
    check(a_admin.get("/api/linkedin/status").get_json()["state"] == "needs_login", "being logged out is not shown")
    make_read_due()
    fake(auth="expired", conversations=conversations, messages=messages)
    tick()
    check(calls() == ["status", "search", "status"], f"an expired saved login was not caught: {calls()}")
    check(a_admin.get("/api/linkedin/status").get_json()["state"] == "needs_login", "an expired login is not shown")
    make_read_due()
    changed = [dict(conversations[0], last_activity_text="10:00 AM")]
    fake(conversations=changed, messages=messages, broken=["c1"])
    tick()
    status = a_admin.get("/api/linkedin/status").get_json()
    check(status["state"] == "error", f"a failed read is not shown: {status}")
    with use_profile(ws_a):
        check(storage.load()["last_synced_at"] == before, "a failed read moved last synced")
    make_read_due()
    messages["c1"].append(message("m5", "the retry", "incoming", "10:00 AM"))
    fake(conversations=changed, messages=messages)
    tick()
    check(any(c == "get:c1" for c in calls()), "a failed conversation was not retried")

    # ---- an outdated helper updates itself to the current version, and only to that
    copy = HELPER_HOME / "agent.py"
    copy.write_text(AGENT.read_text().replace("SEEN_KEPT = 2000", "SEEN_KEPT = 1999"))
    env = dict(os.environ, LK_LINKEDIN_HOME=str(HELPER_HOME), LK_LINKEDIN_PAUSE="0", FAKE_LINKEDIN=str(LINKEDIN), LK_LINKEDIN_NO_STOP="1", LK_LINKEDIN_MCP_COMMAND=json.dumps([sys.executable, str(FAKE)]))
    fake(conversations=conversations, messages=messages)
    run = subprocess.run([sys.executable, str(copy), "tick"], env=env, capture_output=True, text=True, timeout=120)
    check("Helper updated." in run.stdout and copy.read_bytes() == AGENT.read_bytes(), f"an outdated helper did not update itself: {run.stdout[-200:]} {run.stderr[-300:]}")
    check(calls() == [], "the update run also read LinkedIn")

    # ---- nonsense and strangers
    stranger = app.test_client()
    check(stranger.get("/linkedin-helper/plan").status_code == 401, "the plan answers without a key")
    check(stranger.post("/linkedin-helper/conversations", json={"conversations": []}).status_code == 401, "messages were taken without a key")
    good = {"Authorization": "Bearer " + config["key"]}
    check(stranger.post("/linkedin-helper/conversations", headers=good, json={"conversations": [{"summary": 5}, "x", {"summary": {"conversation_ref": 1}, "messages": []}]}).get_json() == {"new_messages": 0},
          "bad data was not ignored")
    check(linkedin_link.clean_conversations([{"summary": {"conversation_ref": "r"}, "messages": [{"message_ref": "m", "text": "x" * 99999}]}])[0][1][0]["text"].__len__() == linkedin_link.MAX_TEXT,
          "an oversized message was let through")

    # ---- Claude sorts the people waiting for review
    from lk_calendar import claude_connector

    def connector_call(method, path, body=None):
        r = a_admin.open(path, method=method, json=body)
        return r.status_code, r.get_json()
    waiting = claude_connector._run_tool(connector_call, "linkedin_inbox", {})
    check(any(w["name"] == "Known Lead" for w in waiting) is False and len(waiting) >= 1, f"the review list is wrong: {waiting}")
    keep, drop = waiting[0]["person_id"], (waiting[1]["person_id"] if len(waiting) > 1 else None)
    args = {"keep": [keep], "remove": [drop] if drop else []}
    shown = claude_connector._run_tool(connector_call, "sort_linkedin_inbox", dict(args))
    check("show_the_user" in shown and waiting[0]["name"] in shown["show_the_user"], f"sorting was not shown first: {shown}")
    check(len(claude_connector._run_tool(connector_call, "linkedin_inbox", {})) == len(waiting), "sorting changed something before the yes")
    done = claude_connector._run_tool(connector_call, "sort_linkedin_inbox", dict(args, confirm_code=shown["confirm_code"]))
    check(done.get("kept") == [waiting[0]["name"]], f"keeping did not work: {done}")
    left = {w["person_id"] for w in claude_connector._run_tool(connector_call, "linkedin_inbox", {})}
    check(keep not in left and (drop is None or drop not in left), "sorted people are still waiting")

    # ---- sending: shown first, then it waits for the add-on, which sends it and reports back
    fake(conversations=conversations, messages=messages)
    shown = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "New Person", "text": "zz hello from LyteKeepr"})
    check("show_the_user" in shown and "zz hello from LyteKeepr" in shown["show_the_user"], f"a LinkedIn message was not shown first: {shown}")
    check(a_admin.get("/api/linkedin/status").get_json()["outbox_waiting"] == 0, "a message was queued before the yes")
    wrong = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "New Person", "text": "something else", "confirm_code": shown["confirm_code"]})
    check("show_the_user" in wrong and a_admin.get("/api/linkedin/status").get_json()["outbox_waiting"] == 0, "a changed message was sent with the old yes")
    done = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "New Person", "text": "zz hello from LyteKeepr", "confirm_code": shown["confirm_code"]})
    check(done.get("status") == "queued", f"the approved message was not queued: {done}")
    check(a_admin.get("/api/linkedin/status").get_json()["outbox_waiting"] == 1, "the approved message is not waiting")
    tick()
    check("send:new-person" in calls(), f"the add-on did not send it: {calls()}")
    check(a_admin.get("/api/linkedin/status").get_json()["outbox_waiting"] == 0, "a sent message is still waiting")
    sent_file = Path(str(LINKEDIN) + ".sent")
    check(sent_file.read_text().count("zz hello from LyteKeepr") == 1, "the message was not sent exactly once")
    tick()
    check(sent_file.read_text().count("zz hello from LyteKeepr") == 1, "a sent message went out again")
    # a first message to someone with a LinkedIn profile but no conversation yet
    first = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "Known Lead", "text": "zz first message"})
    check("show_the_user" in first, f"a contact with a profile but no conversation could not be messaged: {first}")
    # logged out: nothing is tried, the message keeps waiting; LinkedIn refusing: reported, not retried
    for outcome_case in ("logged out", "refused"):
        shown = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "Two Months", "text": f"zz {outcome_case}"})
        claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "Two Months", "text": f"zz {outcome_case}", "confirm_code": shown["confirm_code"]})
        if outcome_case == "logged out":
            fake(auth="login_required", conversations=conversations, messages=messages)
            tick()
            check(not [c for c in calls() if c.startswith("send:")] and a_admin.get("/api/linkedin/status").get_json()["outbox_waiting"] == 1,
                  f"a message was tried, or dropped, while logged out: {calls()}")
        fake(conversations=conversations, messages=messages, send_outcome="failed")
        tick()
    status = a_admin.get("/api/linkedin/status").get_json()
    check(status["outbox_waiting"] == 0 and len(status["outbox_problems"]) == 2, f"failed sends are not reported: {status}")

    # ---- a suspended workspace pauses, Disconnect stops for good
    directory.set_status(accounts["owner-a@example.com"]["id"], "suspended")
    check(stranger.get("/linkedin-helper/plan", headers=good).status_code == 403, "a suspended workspace can still add messages")
    directory.set_status(accounts["owner-a@example.com"]["id"], "active")
    check(a_admin.post("/api/linkedin/link/disconnect", json={}).status_code == 200, "Disconnect failed")
    check(stranger.get("/linkedin-helper/plan", headers=good).status_code == 410, "a disconnected key still works")
    fake(conversations=conversations, messages=messages)
    tick()
    check(calls() == [] and not (HELPER_HOME / "config.json").exists(), "a disconnected helper did not stop")
    status = a_admin.get("/api/linkedin/status").get_json()
    check(not status["connected"] and status["message_count"] > 0, f"disconnecting should keep the saved messages: {status}")
    check(a_admin.post("/api/linkedin/sync/run", json={}).status_code == 400, "Sync now answered with nothing connected")
    shown = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "New Person", "text": "zz not connected"})
    refused = claude_connector._run_tool(connector_call, "send_linkedin_message", {"contact": "New Person", "text": "zz not connected", "confirm_code": shown["confirm_code"]})
    check(refused.get("status") == "not_connected", f"a message was accepted with LinkedIn disconnected: {refused}")
    addon_check(address)
    httpd.shutdown()


def addon_check(address):
    """The Claude add-on: download, Approve page, linking, and a real run of its server with the fake LinkedIn."""
    import io
    import zipfile
    a_member.get("/")
    check(a_member.get("/api/linkedin/addon").status_code == 403, "a team member could download the add-on")
    data = a_admin.get("/api/linkedin/addon").get_data()
    z = zipfile.ZipFile(io.BytesIO(data))
    check(sorted(z.namelist()) == ["address.txt", "agent.py", "manifest.json", "pyproject.toml", "server.py"], f"the add-on file is wrong: {z.namelist()}")
    check(z.read("address.txt").decode().strip() == "http://localhost", "the add-on does not carry the address it was downloaded from")
    manifest = json.loads(z.read("manifest.json"))
    check(manifest["server"]["type"] == "uv" and manifest["manifest_version"] == "0.4", "the add-on manifest is wrong")
    # what the Claude app itself demands of a 0.4 add-on (read from Claude Desktop's own checks): otherwise "Couldn't preview extension"
    for field in ("name", "version", "description", "author", "server"):
        check(field in manifest, f"the add-on manifest lacks {field}")
    server_part = manifest.get("server", {})
    check(all(k in server_part for k in ("type", "entry_point", "mcp_config")) and isinstance(server_part.get("mcp_config", {}).get("command"), str),
          "the add-on's server needs type, entry_point and mcp_config with a command (Claude refuses it otherwise)")
    check(set(manifest) <= {"$schema", "manifest_version", "name", "display_name", "version", "description", "long_description", "author", "repository",
                            "homepage", "documentation", "support", "icon", "icons", "screenshots", "localization", "server", "tools", "tools_generated",
                            "prompts", "prompts_generated", "keywords", "license", "privacy_policies", "compatibility", "user_config", "_meta"},
          "the add-on manifest has a field Claude does not know")
    check(all(set(t) <= {"name", "description"} for t in manifest.get("tools", [])), "the add-on's tool list has a field Claude does not know")
    folder = TMP / "addon"
    z.extractall(folder)
    (folder / "address.txt").write_text(address + "\n")          # the test app's real address, for the run below
    # install it the way the Claude app does ("uv sync" in the add-on's folder), so a clash between its parts fails here, not on a USER's computer
    synced = subprocess.run(["uv", "sync", "--quiet"], cwd=folder, capture_output=True, text=True, timeout=600)
    check(synced.returncode == 0, f"the add-on's parts do not install together: {synced.stderr[-400:]}")
    addon_python = folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if synced.returncode != 0:
        return

    stranger = app.test_client()
    started = stranger.post("/linkedin-helper/link/start", json={"computer": "Test Mac"}).get_json()
    check(stranger.post("/linkedin-helper/link/poll", json={"link_id": started["link_id"], "secret": started["secret"]}).status_code == 202, "a link was ready before Approve")
    check(stranger.post("/linkedin-helper/link/poll", json={"link_id": started["link_id"], "secret": "wrong"}).status_code == 410, "a wrong secret was answered")
    page = started["url"].split("://", 1)[1].split("/", 1)[1]
    page = "/" + page
    check(stranger.get(page).status_code == 302, "the Approve page opened without signing in")
    check(b"Ask your admin" in a_member.get(page).get_data(), "a team member was offered Approve")
    check(b"Approve" in a_admin.get(page).get_data() and b"Test Mac" in a_admin.get(page).get_data(), "the admin did not get the Approve page")
    check(a_admin.post("/linkedin/approve", data={"l": started["link_id"]}).status_code == 400, "Approve worked without accepting the risk")
    check(a_member.post("/linkedin/approve", data={"l": started["link_id"], "risk": "yes"}).status_code == 403, "a team member could approve")
    check(a_admin.post("/linkedin/approve", data={"l": started["link_id"], "risk": "yes"}).status_code == 200, "Approve failed")
    got = stranger.post("/linkedin-helper/link/poll", json={"link_id": started["link_id"], "secret": started["secret"]})
    check(got.status_code == 200 and got.get_json().get("key"), "the add-on did not get its key after Approve")
    check(stranger.post("/linkedin-helper/link/poll", json={"link_id": started["link_id"], "secret": started["secret"]}).status_code == 410, "a key was handed out twice")
    a_admin.post("/api/linkedin/link/disconnect", json={})

    # the add-on's own server, as the Claude app runs it: it asks for a link by itself, waits for Approve, then reads
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    home = TMP / "addon-home"
    env = dict(os.environ, LK_LINKEDIN_HOME=str(home), LK_NO_BROWSER="1", LK_LINKEDIN_NO_LOGIN="1", LK_LINKEDIN_NO_STOP="1", LK_LINKEDIN_PAUSE="0",
               FAKE_LINKEDIN=str(LINKEDIN), LK_LINKEDIN_MCP_COMMAND=json.dumps([sys.executable, str(FAKE)]))
    changed = [dict(c, last_activity_text="11:11 AM") for c in json.loads(LINKEDIN.read_text())["conversations"]]
    fake(conversations=changed, messages=json.loads(LINKEDIN.read_text())["messages"])

    async def run():
        async with stdio_client(StdioServerParameters(command=str(addon_python), args=[str(folder / "server.py")], env=env)) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
                check(names == ["linkedin_status", "sync_linkedin"], f"Claude sees other tools: {names}")
                for _ in range(100):                     # the add-on asked for a link by itself
                    with linkedin_link._db() as conn:
                        row = conn.execute("SELECT link_id FROM linkedin_pending WHERE computer != 'Test Mac' AND key = ''").fetchone()
                    if row:
                        break
                    await asyncio.sleep(0.1)
                check(bool(row), "the add-on did not ask for a link")
                if not row:
                    return
                a_admin.post("/linkedin/approve", data={"l": row["link_id"], "risk": "yes"})
                for _ in range(100):
                    if (home / "config.json").exists():
                        break
                    await asyncio.sleep(0.1)
                await asyncio.sleep(1.5)
                text = (await session.call_tool("sync_linkedin", {})).content[0].text
                check("Reading LinkedIn now" in text, f"sync_linkedin answered: {text}")
                for _ in range(150):
                    if "get:c1" in calls():
                        break
                    await asyncio.sleep(0.1)
                check("get:c1" in calls(), f"the add-on did not read LinkedIn: {calls()}")
                await asyncio.sleep(1)
                status_text = (await session.call_tool("linkedin_status", {})).content[0].text
                check(status_text.startswith("Linked."), f"linkedin_status answered: {status_text}")
    asyncio.run(run())
    status = a_admin.get("/api/linkedin/status").get_json()
    check(status["connected"] and status["computer"] not in ("", "Test Mac"), f"the add-on's computer is not the connected one: {status}")


try:
    main()
except Exception as exc:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    problems.append(f"COULD NOT FINISH: {exc}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
for p in problems:
    print("FAILED:", p)
print("LINKEDIN CHECK FAILED" if problems else "LINKEDIN CHECK PASSED")
sys.exit(1 if problems else 0)
