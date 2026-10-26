#!/usr/bin/env python3
"""Checks the opportunity rules on a fresh, empty installation in a temporary folder (never the real data):
contact entries stay as they are, opportunities hold several people with optional roles and an optional single primary,
"no longer involved" keeps history while "remove" only drops the link, stages move only by hand, deletes are protected,
and a team member sees an opportunity (and the people on it) only through a funnel shared with them.
Prints OPPORTUNITY CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_opportunities.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-opps-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({"LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy.db"), "LYTEKEEPR_PASSWORD": "opps-test",
                   "LYTEKEEPR_SECRET_KEY": "opps-test", "LYTEKEEPR_ADMIN_EMAIL": "owner@example.com", "LK_GMAIL_DAILY": "0",
                   "LK_GOOGLE_AUTOSYNC": "0", "LK_FATHOM_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": ""})
sys.path.insert(0, str(APP_DIR))
import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print("OPPORTUNITY CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
    sys.exit(2)
import google_oauth.store as gstore  # noqa: E402
gstore.LEGACY_DIR = TMP / "legacy-google"
import auth_gate  # noqa: E402
auth_gate._notify_signup = lambda email, url: None
import server  # noqa: E402
from lk_calendar import superadmin  # noqa: E402
superadmin._alert = lambda *a: None
import logging  # noqa: E402
server.app.logger.setLevel(logging.CRITICAL)

app = server.app
problems = []


def check(ok, what):
    if not ok:
        problems.append(what)


def main():
    a = app.test_client()
    a.post("/login", data={"password": "opps-test"})
    def contact(name, company, email):
        r = a.post("/api/calendar/contacts", json={"name": name, "company": company, "email": email, "brand": "lytekeepr"})
        return r.get_json()["id"]
    sarah, michael, anna, tester = (contact("Sarah Intro", "Alpha Co", "sarah@alpha.test"), contact("Michael Decide", "Beta Inc", "michael@beta.test"),
                                    contact("Anna Advise", "Gamma LLC", "anna@gamma.test"), contact("Tom Tester", "Alpha Co", "tom@alpha.test"))
    fid = a.post("/api/crm/funnels", json={"name": "Partnerships"}).get_json()["funnel_id"]
    other_fid = a.post("/api/crm/funnels", json={"name": "Private"}).get_json()["funnel_id"]
    a.post("/api/crm/funnels/move", json={"person_ids": [tester], "funnel_id": fid})          # an ordinary contact entry
    board = a.get(f"/api/crm/funnels/{fid}/board").get_json()
    stages = board["stages"]
    tester_stage = next(s["stage_id"] for s in stages for p in s["people"] if p["person_id"] == tester)

    o = a.post("/api/crm/opportunities", json={"name": "Beta pilot", "funnel_id": fid}).get_json()
    oid = o["opportunity_id"]
    check(o["participants"] == [] and o["stage_id"] == stages[0]["stage_id"], "a new opportunity starts empty in the first stage")
    a.post(f"/api/crm/opportunities/{oid}/participants", json={"contact_id": sarah, "role": "introducer"})
    a.post(f"/api/crm/opportunities/{oid}/participants", json={"contact_id": michael, "role": "decision-maker", "introduced_by": sarah})
    o = a.post(f"/api/crm/opportunities/{oid}/participants", json={"contact_id": anna, "role": "advisor", "introduced_by": sarah}).get_json()
    check(not any(p["primary"] for p in o["participants"]), "no primary is set unless asked")
    check({p["company"] for p in o["participants"]} == {"Alpha Co", "Beta Inc", "Gamma LLC"}, "participants keep their own companies")
    check(a.post(f"/api/crm/opportunities/{oid}/participants", json={"contact_id": michael}).status_code == 400, "the same person can't be added twice")
    a.post(f"/api/crm/opportunities/{oid}/participants", json={"contact_id": tester})          # a contact entry can also be a participant
    o = a.put(f"/api/crm/opportunities/{oid}/participants/{michael}", json={"primary": True}).get_json()
    o = a.put(f"/api/crm/opportunities/{oid}/participants/{anna}", json={"primary": True}).get_json()
    check([p["contact_id"] for p in o["participants"] if p["primary"]] == [anna], "at most one primary: the newest one")
    o = a.post(f"/api/crm/opportunities/{oid}/clear-primary").get_json()
    check(not any(p["primary"] for p in o["participants"]), "the primary can be cleared")
    o = a.put(f"/api/crm/opportunities/{oid}/participants/{sarah}", json={"active": False}).get_json()
    s_row = next(p for p in o["participants"] if p["contact_id"] == sarah)
    check(not s_row["active"] and s_row["left_at"], "no longer involved: kept, with the date they left")
    check(next(p for p in o["participants"] if p["contact_id"] == michael)["introduced_by_name"] == "Sarah Intro", "who introduced whom is kept after they leave")
    check(o["stage_id"] == stages[0]["stage_id"], "participant changes never move the stage")
    o = a.post(f"/api/crm/opportunities/{oid}/participants/{anna}/remove").get_json()
    check(all(p["contact_id"] != anna for p in o["participants"]) and a.get(f"/api/calendar/contacts/{anna}").status_code == 200,
          "remove drops only the link; the contact stays")
    o = a.post(f"/api/crm/opportunities/{oid}/move", json={"stage_id": stages[2]["stage_id"]}).get_json()
    check(o["stage_id"] == stages[2]["stage_id"], "moving by hand works")
    board = a.get(f"/api/crm/funnels/{fid}/board").get_json()
    check(next(s["stage_id"] for s in board["stages"] for p in s["people"] if p["person_id"] == tester) == tester_stage, "the contact entry did not move")
    check(sum(len(s["people"]) for s in board["stages"]) == 1, "no contact entries were added or removed")
    check([c["opportunity_id"] for s in board["stages"] for c in s["opportunities"]] == [oid], "the opportunity is one card on the board")
    check(any(x["opportunity_id"] == oid for x in a.get(f"/api/crm/people/{tester}/opportunities").get_json()["opportunities"]), "a contact lists their opportunities")
    check(a.post(f"/api/crm/funnels/stages/{stages[2]['stage_id']}/delete").status_code == 400, "a stage holding an opportunity can't be deleted")
    check(a.post(f"/api/crm/funnels/{fid}/delete").status_code == 400, "a funnel holding opportunities can't be deleted")
    hist = [h["text"] for h in a.get(f"/api/crm/opportunities/{oid}").get_json()["history"]]
    check(any("no longer involved" in h for h in hist) and any("added by mistake" in h for h in hist) and any("Moved from" in h for h in hist), "the history records the changes")
    t = a.post(f"/api/crm/opportunities/{oid}/tasks", json={"title": "Send the deck", "date": (date.today() + timedelta(days=3)).isoformat()})
    check(t.status_code == 201 and t.get_json()["tasks"][0]["title"] == "Send the deck", "a task can be linked to an opportunity")
    secret = a.post("/api/crm/opportunities", json={"name": "Secret deal", "funnel_id": other_fid}).get_json()["opportunity_id"]
    a.post(f"/api/crm/opportunities/{secret}/participants", json={"contact_id": anna})

    # a team member with only the Partnerships funnel shared
    uid = a.post("/api/team", json={"email": "member@example.com", "name": "Member"}).get_json()["user"]["id"]
    a.post(f"/api/team/{uid}/access", json={"access": "funnels"})
    a.post(f"/api/team/{uid}/funnels", json={"funnel_ids": [fid]})
    m = app.test_client()
    server.login_identity = lambda code: {"email": "member@example.com", "email_verified": True, "name": "Member"}
    m.get(f"/auth/google/callback?state={server._oauth_state_signer().dumps('google-login')}&code=x")
    check(m.get(f"/api/crm/opportunities/{oid}").status_code == 200, "a member sees opportunities in a shared funnel")
    check(m.get(f"/api/calendar/contacts/{michael}").status_code == 200, "a member can open the people on a shared opportunity")
    check(m.get(f"/api/crm/opportunities/{secret}").status_code == 400, "a member can't open an opportunity in a funnel not shared with them")
    check(m.get(f"/api/calendar/contacts/{anna}").status_code in (403, 404), "a member can't open a contact who is only on an unshared opportunity")
    check([x["name"] for x in m.get("/api/crm/opportunities?q=deal").get_json()["opportunities"]] == [], "search hides unshared opportunities")


try:
    main()
except Exception as exc:                               # a crash is a failure, said plainly
    import traceback
    traceback.print_exc()
    problems.append(f"crashed: {exc}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
for p in problems:
    print("FAILED:", p)
print("OPPORTUNITY CHECK PASSED" if not problems else f"OPPORTUNITY CHECK FAILED: {len(problems)} problem(s)")
sys.exit(0 if not problems else 1)
