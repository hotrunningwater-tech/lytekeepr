#!/usr/bin/env python3
"""Workspace isolation check: no account may see or change another account's data. Run before every deploy (deploy_calendar.sh
does). Exit code 0 = no leak found, 1 = a leak or a change across workspaces, 2 = the check itself could not run.

It builds two brand-new workspaces in a temporary folder (never the real data), fills each with data carrying a unique marker
("zzleaka" in A, "zzleakb" in B), then signs in as the OTHER workspace's admin and team member and calls every address the app
has, with the victim's ids filled in. It fails if
  - any answer contains the victim's marker,
  - any change request (POST/PUT/DELETE) alters the victim's data,
  - the victim's marker shows up in the attacker's files.
Both directions are checked. Google, Gmail and email are never contacted.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-isolation-"))

# ---- a fresh, empty installation in the temporary folder (set before anything from the app is imported)
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({
    "LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy-calendar.db"),
    "LYTEKEEPR_PASSWORD": "isolation-test", "LYTEKEEPR_SECRET_KEY": "isolation-test",
    "LYTEKEEPR_ADMIN_EMAIL": "owner-a@example.com", "LYTEKEEPR_SUPER_ADMINS": "console-owner@example.com",      # neither attacker may open the owner console
    "LK_GMAIL_DAILY": "0", "LK_GOOGLE_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": "",
})
sys.path.insert(0, str(APP_DIR))

import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    # The app was loaded before this script could point it at the temporary folder (for example by another script importing it
    # first). Running now would touch the REAL data, so stop before a single request.
    print(f"ISOLATION CHECK REFUSED TO RUN: the app points at {_profiles.PROFILE_ROOT}, not the temporary folder. Run this script on its own.")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(2)
if os.getenv("LK_ISOLATION_SELFTEST") == "1":
    # Prove the check can fail: break the separation on purpose (every signed-in request lands in the first workspace).
    _real_set = _profiles.set_request_profile
    _profiles.set_request_profile = lambda profile_id: _real_set(_profiles.home_profile_id())
import google_oauth.store as gstore  # noqa: E402
gstore.LEGACY_DIR = TMP / "legacy-google"          # never copy the real Google tokens
import auth_gate  # noqa: E402
auth_gate._notify_signup = lambda email, url: None   # never send the sign-up email
import server  # noqa: E402
from lk_calendar import superadmin as _console  # noqa: E402
_console._alert = lambda *args: None                 # never email an error alert from a test
from profiles.store import use_profile  # noqa: E402
from lk_calendar import directory  # noqa: E402

app = server.app
import logging  # noqa: E402
app.logger.setLevel(logging.CRITICAL)            # the attacks make the app refuse nonsense; those refusals are not news here
logging.getLogger("werkzeug").setLevel(logging.CRITICAL)
SKIP_PREFIXES = auth_gate.HOME_ONLY_API + ("/api/gmail", "/api/email", "/api/google", "/auth/", "/api/chat", "/api/calendar/chat",
                                           "/api/calendar/contact-ai", "/api/calendar/google", "/api/funnel-email", "/lk-calendar/", "/email/")
BODY_ID_ROUTES = ["/api/crm/funnels/move", "/api/crm/funnels/stages/move", "/api/crm/people/merge", "/api/calendar/followups/move",
                  "/api/calendar/events/done-bulk"]
problems = []


def client():
    return app.test_client()


def google_login(c, email):
    server.login_identity = lambda code: {"email": email, "email_verified": True, "name": email.split("@")[0]}
    state = server._oauth_state_signer().dumps("google-login")
    return c.get(f"/auth/google/callback?state={state}&code=test")


def seed(c, mark, owner_email, workspace, padding=0):
    """Fill a workspace with data carrying its marker; returns the ids an attacker would try. padding first adds filler records,
    so this workspace's ids differ from the other's: an attacker using the victim's ids then hits only filler of its own,
    never its own marked data (which would weaken the check in the other direction)."""
    ids = {}
    if padding:
        pad_funnel = c.post("/api/crm/funnels", json={"name": "filler funnel"}).get_json()["funnel_id"]
        pad_people = [c.post("/api/calendar/contacts", json={"name": f"Filler {i}", "email": f"filler{i}@{mark}-filler.test"}).get_json()["id"] for i in range(padding)]
        c.post("/api/crm/funnels/move", json={"person_ids": pad_people, "funnel_id": pad_funnel})
        for i in range(padding):
            c.post("/api/calendar/events", json={"title": f"Filler {i}", "start_date": "2026-10-21", "start_time": "09:00", "project": "lytekeepr", "kind": "task"})
            c.post("/api/team", json={"email": f"filler{i}@{mark}-filler.test"})
            c.post("/api/calendar/companies", json={"name": f"Filler company {i}"})
    r = c.post("/api/calendar/contacts", json={"name": f"{mark} Person", "company": f"{mark} Company", "email": f"{mark}.person@example.com",
                                              "phone": "+1 555 0101", "notes": f"{mark} private note"})
    assert r.status_code == 201, r.get_json()
    ids["person"] = r.get_json()["id"]
    ids["funnel"] = c.post("/api/crm/funnels", json={"name": f"{mark} Funnel"}).get_json()["funnel_id"]
    c.post("/api/crm/funnels/move", json={"person_ids": [ids["person"]], "funnel_id": ids["funnel"]})
    board = c.get(f"/api/crm/funnels/{ids['funnel']}/board").get_json()
    ids["stage"] = board["stages"][1]["stage_id"]
    ids["event"] = c.post("/api/calendar/events", json={"title": f"{mark} Event", "start_date": "2026-10-20", "start_time": "10:00",
                                                       "project": "lytekeepr", "kind": "task"}).get_json().get("id")
    ids["company"] = [x for x in c.get("/api/calendar/companies").get_json()["companies"] if mark in x["name"].lower()][0]["id"]
    ids["user"] = c.post("/api/team", json={"email": f"member-{mark}@example.com", "name": f"{mark} Member"}).get_json()["user"]["id"]
    c.put("/api/calendar/profile", json={"name": f"{mark} Sender"})
    leads = c.get("/api/calendar/leads").get_json()["leads"]
    ids["lead"] = leads[0]["id"] if leads else 1
    with use_profile(workspace):                        # their own Gmail token and a saved draft, written straight to their folder
        gstore.save_tokens({"refresh_token": f"{mark}-refresh-token"}, account_id=owner_email)
        gstore.set_active_account_id(owner_email)
        from assistant import email_send_flow
        email_send_flow._remember_draft({"id": f"{mark}draft", "person_name": f"{mark} Person", "to_email": f"{mark}.person@example.com",
                                          "subject": f"{mark} subject", "body": f"{mark} body", "createdAt": "2026-10-16T00:00:00"})
    return ids


def snapshot(c):
    """What the victim's own admin sees: if an attacker changed anything, this differs afterwards."""
    ov = c.get("/api/crm/overview").get_json()
    events = c.get("/api/calendar/events?start=2026-10-01&end=2026-11-10").get_json()["events"]
    return json.dumps({
        "people": sorted((p["person_id"], p["name"], p.get("notes") or "", p.get("funnel_stage_name") or "") for p in ov["people"]),
        "companies": sorted(x["company_name"] for x in ov["companies"]),
        "funnels": sorted((f["funnel_id"], f["name"]) for f in c.get("/api/crm/funnels").get_json()["funnels"]),
        "team": sorted((u["email"], u["active"], u["access"]) for u in c.get("/api/team").get_json()["users"]),
        "events": sorted((e["id"], e["title"], e["start_date"], e["done"]) for e in events),
        "profile": c.get("/api/calendar/profile").get_json().get("name"),
    }, sort_keys=True)


def fill(rule, victim, attacker_ids):
    values = {}
    for arg in rule.arguments:
        name = arg.lower()
        if "person" in name or "contact" in name:
            values[arg] = victim["person"]
        elif "funnel" in name:
            values[arg] = victim["funnel"]
        elif "stage" in name:
            values[arg] = victim["stage"]
        elif "event" in name:
            values[arg] = victim["event"]
        elif "lead" in name:
            values[arg] = victim["lead"]
        elif "company" in name:
            values[arg] = victim["company"]
        elif "user" in name:
            values[arg] = victim["user"]
        elif "account" in name:
            values[arg] = 1
        else:
            values[arg] = 1
    path = rule.rule
    for arg, value in values.items():
        path = re.sub(r"<(?:[a-z]+:)?" + arg + r">", str(value), path)
    return path


def attack(direction, attackers, victim_ids, victim_mark, victim_admin, attacker_ids, attacker_folder):
    before = snapshot(victim_admin)
    if victim_mark not in before.lower():
        problems.append(f"BROKEN {direction}: the victim's marked data is missing before the attack, so the check would prove nothing")
    calls = 0
    query = "?start=2026-10-01&end=2026-11-10&q=zzleak&limit=200&channel=&email=" + victim_mark + ".person@example.com"
    for who, c in attackers:
        for rule in app.url_map.iter_rules():
            path = rule.rule
            if path.startswith(SKIP_PREFIXES) or rule.endpoint == "static" or path in ("/logout", "/login"):      # signing out would end the attack
                continue
            url = fill(rule, victim_ids, attacker_ids)
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                if method != "GET" and not rule.arguments and path not in BODY_ID_ROUTES:
                    continue                                  # creating things in your own workspace is not a cross-workspace risk
                body = {"name": "zzhack", "title": "zzhack", "notes": "zzhack", "status": "done", "active": False, "access": "all",
                        "person_id": victim_ids["person"], "person_ids": [victim_ids["person"]], "funnel_id": victim_ids["funnel"],
                        "stage_id": victim_ids["stage"], "keep_person_id": attacker_ids["person"], "merge_person_id": victim_ids["person"],
                        "funnel_ids": [victim_ids["funnel"]], "lead_ids": [victim_ids["lead"]], "event_ids": [victim_ids["event"]],
                        "share": True, "to_date": "2026-12-01", "email": "zzhack@example.com"}
                try:
                    if method == "GET":
                        r = c.get(url + (query if "?" not in url else ""))
                    else:
                        r = c.open(url, method=method, json=body)
                except Exception as exc:                      # a crash is not a leak, but say so
                    print(f"  note: {who} {method} {url} raised {type(exc).__name__}")
                    continue
                calls += 1
                text = r.get_data().lower()
                if victim_mark.encode() in text:
                    problems.append(f"LEAK {direction}: {who} {method} {url} -> {r.status_code}: {text[:200]!r}")
    after = snapshot(victim_admin)
    if before != after:
        problems.append(f"CHANGED {direction}: the victim's data changed after the attack\n  before {before}\n  after  {after}")
    for f in attacker_folder.rglob("*"):
        if f.is_file() and victim_mark.encode() in f.read_bytes().lower():
            problems.append(f"FILE {direction}: {victim_mark} found in {f}")
    print(f"{direction}: {calls} requests checked")


def main():
    a_admin = client()
    assert a_admin.post("/login", data={"password": "isolation-test"}).status_code == 302
    b_admin = client()
    google_login(b_admin, "owner-b@example.com")
    if a_admin.get("/api/superadmin/accounts").status_code != 403 or b_admin.get("/owner").status_code != 403:
        problems.append("OWNER CONSOLE: an account admin who is not the owner could open it")
    accounts = {x["owner_email"]: x for x in directory.list_accounts()}
    directory.set_status(accounts["owner-b@example.com"]["id"], "active")           # what the owner's Approve button does
    ws_a, ws_b = accounts["owner-a@example.com"]["profile_id"], accounts["owner-b@example.com"]["profile_id"]
    ids_a = seed(a_admin, "zzleaka", "owner-a@example.com", ws_a)
    ids_b = seed(b_admin, "zzleakb", "owner-b@example.com", ws_b, padding=6)
    a_member, b_member = client(), client()
    google_login(a_member, "member-zzleaka@example.com")
    google_login(b_member, "member-zzleakb@example.com")
    a_admin.post(f"/api/team/{ids_a['user']}/access", json={"access": "all"})       # members with the widest access inside their own workspace
    b_admin.post(f"/api/team/{ids_b['user']}/access", json={"access": "all"})
    root = TMP / "profiles" / "data"
    folder = {ws: root / p["storage_id"] for ws, p in json.loads((TMP / "profiles" / "registry.json").read_text())["profiles"].items()}
    def own_data(when):
        # each workspace sees its own data (otherwise "no leak" would pass trivially because nothing works at all)
        missing = [f"{who} cannot see {m}" for who, c, m in (("A admin", a_admin, "zzleaka"), ("A member", a_member, "zzleaka"),
                                                              ("B admin", b_admin, "zzleakb"), ("B member", b_member, "zzleakb"))
                   if m.encode() not in c.get("/api/crm/overview").get_data().lower()]
        if missing:
            problems.append(f"BROKEN {when}: " + "; ".join(missing))
    own_data("before the attacks")
    attack("B -> A", [("B admin", b_admin), ("B member", b_member)], ids_a, "zzleaka", a_admin, ids_b, folder[ws_b])
    attack("A -> B", [("A admin", a_admin), ("A member", a_member)], ids_b, "zzleakb", b_admin, ids_a, folder[ws_a])
    own_data("after the attacks")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"ISOLATION CHECK COULD NOT RUN: {exc}")
        shutil.rmtree(TMP, ignore_errors=True)
        sys.exit(2)
    shutil.rmtree(TMP, ignore_errors=True)
    if problems:
        print("\n".join(problems))
        print(f"ISOLATION CHECK FAILED: {len(problems)} problem(s)")
        sys.exit(1)
    print("ISOLATION CHECK PASSED: no workspace saw or changed another's data")
    sys.exit(0)
