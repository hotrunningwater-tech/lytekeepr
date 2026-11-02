#!/usr/bin/env python3
"""Checks the brand rules on a fresh, empty installation in a temporary folder (never the real data): a team member limited to
some brands never sees a contact, funnel, task or opportunity participant of another brand; "whole brand" and "ticked funnels
only" behave as described in lk_calendar/access.py; a member can only create contacts in their brands and can't change brands;
a member with no brands chosen keeps their old funnel access; personal tasks, goals and documents are only ever their maker's. Deploys stop if this fails.
Prints BRAND CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_brands.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-brands-"))
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
    print("BRAND CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
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
    def contact(**k):
        if "brand" not in k:                       # a contact with no brand (as old data, syncs and suggestions still make)
            from lk_calendar import contacts as lk_contacts
            return lk_contacts.create_contact(k, require_brand=False)["id"]
        return a.post("/api/calendar/contacts", json=k).get_json()["id"]
    def funnel(name, brand=""):
        f = a.post("/api/crm/funnels", json={"name": name}).get_json()["funnel_id"]
        if brand:
            a.post(f"/api/crm/funnels/{f}/brand", json={"brand": brand})
        return f
    NF, AF, PF = funnel("NexyF", "nexy"), funnel("AspanF", "aspan"), funnel("PlainF")
    n1, a1, p1 = contact(name="Nexy One", email="n1@t.test"), contact(name="Aspan One", email="a1@t.test"), contact(name="Plain One", email="p1@t.test")
    x1, u1 = contact(name="Nexy Loose", email="x1@t.test", brand="nexy"), contact(name="Unbranded", email="u1@t.test")
    for pid, f in ((n1, NF), (a1, AF), (p1, PF)):
        a.post("/api/crm/funnels/move", json={"person_ids": [pid], "funnel_id": f})
    for pid in (n1, a1):
        a.post(f"/api/calendar/people/{pid}/outreach", json={"channel": "email", "status": "email sent", "follow_up_days": 3})      # a send makes a follow-up only when asked
    opp = a.post("/api/crm/opportunities", json={"name": "Nexy deal", "funnel_id": NF}).get_json()["opportunity_id"]
    for pid in (n1, a1, u1):
        a.post(f"/api/crm/opportunities/{opp}/participants", json={"contact_id": pid})
    names = {n1: "n1", a1: "a1", p1: "p1", x1: "x1", u1: "u1"}
    uid = a.post("/api/team", json={"email": "member@example.com", "name": "M"}).get_json()["user"]["id"]
    a.post(f"/api/team/{uid}/funnels", json={"funnel_ids": [NF]})
    m = app.test_client()
    server.login_identity = lambda code: {"email": "member@example.com", "email_verified": True, "name": "M"}
    m.get(f"/auth/google/callback?state={server._oauth_state_signer().dumps('google-login')}&code=x")

    def seen():
        o = m.get(f"/api/crm/opportunities/{opp}")
        board = m.get(f"/api/crm/funnels/{NF}/board")
        return {"contacts": {names.get(p["person_id"], p["name"]) for p in m.get("/api/crm/overview").get_json()["people"]},
                "search": {names.get(c["id"], c["name"]) for c in m.get("/api/calendar/contacts?q=t.test").get_json()["contacts"]},
                "funnels": {f["name"] for f in m.get("/api/crm/funnels").get_json()["funnels"]},
                "opp": {names[p["contact_id"]] for p in o.get_json()["participants"]} if o.status_code == 200 else None,
                "opp_text": " ".join(c.get("people_text", "") + c.get("summary", "") for st in (board.get_json() or {}).get("stages", []) for c in st.get("opportunities", [])) if board.status_code == 200 else "",
                "tasks": " ".join(e["title"] for e in m.get("/api/calendar/agenda").get_json()["upcoming"]),
                "a1": m.get(f"/api/calendar/contacts/{a1}").status_code,
                "brands": {b["key"] for b in m.get("/api/crm/brands").get_json()["brands"]}}

    v = seen()
    check(v["contacts"] == {"n1", "a1", "u1"} and v["funnels"] == {"NexyF"}, f"no brands chosen: the old funnel access is kept ({v['contacts']}, {v['funnels']})")

    aspan_doc = a.post("/api/crm/docs", json={"title": "Aspan secret plan", "brand": "aspan", "content": "secret"}).get_json()["doc_id"]
    aspan_funnel_doc = a.post("/api/crm/docs", json={"title": "AspanF notes", "funnel_id": AF, "content": "secret"}).get_json()["doc_id"]
    nexy_doc = a.post("/api/crm/docs", json={"title": "Nexy plan", "brand": "nexy", "content": "ok"}).get_json()["doc_id"]
    a.post(f"/api/crm/people/{a1}/conversations", json={"type": "phone call", "text": "aspanonly call about pricing"})
    a.post(f"/api/crm/people/{a1}/notes", json={"text": "aspanonly note"})
    a.post(f"/api/team/{uid}/brands", json={"brands": {"nexy": "all"}})
    v = seen()
    check(m.get(f"/api/crm/docs/{aspan_doc}").status_code == 400 and m.get(f"/api/crm/docs/{aspan_funnel_doc}").status_code == 400,
          "documents of a blocked brand (and its funnels) can't be read")
    check(m.get(f"/api/crm/docs/{nexy_doc}").status_code == 200, "documents of an allowed brand can be read")
    check("secret" not in str(m.get("/api/crm/docs/search?q=secret").get_json()) and "Aspan" not in str(m.get("/api/crm/docs").get_json()),
          "search and the documents overview never show a blocked brand's documents")
    check(m.post("/api/crm/docs", json={"title": "x", "brand": "aspan"}).status_code == 400, "a member can't create documents in a blocked brand")
    check("aspanonly" not in str(m.get("/api/crm/records/search?q=aspanonly").get_json()) and m.get(f"/api/crm/people/{a1}/history").status_code in (400, 403, 404),
          "notes and logged conversations of a blocked brand's contact stay hidden")
    check(v["contacts"] == {"n1", "u1", "x1"} and v["search"] <= {"n1", "u1", "x1"}, f"whole Nexy: Nexy contacts only ({v['contacts']}, search {v['search']})")
    check(v["opp"] == {"n1", "u1"} and "Aspan One" not in v["opp_text"], "an Aspan person on a Nexy opportunity is hidden, also on the board card")
    check("a1@" not in v["tasks"] and "n1@" in v["tasks"], "tasks of an Aspan contact are hidden")
    check(v["a1"] in (403, 404) and v["brands"] == {"nexy"}, "an Aspan contact can't be opened and only Nexy is listed")
    r = m.post("/api/calendar/contacts", json={"name": "Made By M", "email": "mm@t.test"})
    check(r.status_code == 201 and r.get_json()["brand"] == "nexy", "a member's new contact gets their only brand")
    check(m.post("/api/calendar/contacts", json={"name": "Bad", "email": "bad@t.test", "brand": "aspan"}).status_code == 400, "a member can't create a contact in a blocked brand")
    check(m.put(f"/api/calendar/contacts/{x1}", json={"brand": "aspan"}).status_code == 400, "a member can't move a contact to a blocked brand")
    check(m.post(f"/api/team/{uid}/brands", json={"brands": {"aspan": "all"}}).status_code in (401, 403), "a member can't change brand access")
    check(m.post(f"/api/crm/funnels/{AF}/brand", json={"brand": "nexy"}).status_code == 403, "a member can't change a funnel's brand")

    a.post(f"/api/team/{uid}/funnels", json={"funnel_ids": [NF, AF, PF]})
    a.post(f"/api/team/{uid}/brands", json={"brands": {"nexy": "funnels"}})
    v = seen()
    check(v["funnels"] == {"NexyF", "PlainF"}, f"ticked funnels: a shared funnel of a blocked brand stays hidden ({v['funnels']})")
    check("x1" not in v["contacts"] and "a1" not in v["contacts"] and {"n1", "p1"} <= v["contacts"], f"ticked funnels: no Nexy contacts outside the ticked funnels ({v['contacts']})")
    check(m.get(f"/api/crm/funnels/{AF}/board").status_code == 403, "the board of a blocked brand's funnel can't be opened")

    a.post(f"/api/team/{uid}/funnels", json={"funnel_ids": [PF]})
    v = seen()
    check(v["opp"] is None and "n1" not in v["contacts"], "ticked funnels: an unticked Nexy funnel is hidden with its opportunity")

    # Personal tasks and reminders: only their maker sees or changes them, whatever the access (the admin and "Everything" included)
    day = (date.today() + timedelta(days=1)).isoformat()
    def task(client, title, project=None):
        body = {"title": title, "kind": "task", "start_date": day, "start_time": "08:00", "untimed": True, **({"project": project} if project else {})}
        r = client.post("/api/calendar/events", json=body)
        return r.get_json().get("id") if r.status_code == 201 else None
    def titles(client):
        ag = client.get("/api/calendar/agenda").get_json()
        return " ".join(e["title"] for k in ("overdue", "today", "upcoming") for e in ag[k])
    mine_admin, mine_member, work = task(a, "AdminDentist", "personal"), task(m, "MemberGym", "personal"), task(a, "AdminWork")
    check(mine_admin and mine_member and work, "personal tasks can be created by the admin and by a member")
    check("MemberGym" in titles(m) and "AdminDentist" not in titles(m), "a member sees their own personal task and not the admin's")
    check("AdminDentist" in titles(a) and "MemberGym" not in titles(a), "the admin sees their own personal task and not a member's")
    for client, other in ((m, mine_admin), (a, mine_member)):
        check(client.put(f"/api/calendar/events/{other}", json={"title": "x"}).status_code in (403, 404)
              and client.post(f"/api/calendar/events/{other}/done", json={"date": day}).status_code in (403, 404)
              and client.get(f"/api/calendar/events/{other}").status_code in (403, 404)
              and client.delete(f"/api/calendar/events/{other}").status_code in (403, 404), "nobody opens, changes, ticks off or deletes another's personal task")
    check(m.put(f"/api/calendar/events/{mine_member}", json={"title": "MemberGym2"}).status_code == 200, "a member changes their own personal task")
    check(m.put(f"/api/calendar/events/{work}", json={"title": "x"}).status_code in (403, 404) and m.delete(f"/api/calendar/events/{work}").status_code in (403, 404),
          "a member can't change or delete a task they can't see")
    a.post(f"/api/team/{uid}/access", json={"access": "all"})
    check("AdminDentist" not in titles(m), "a member with Everything still doesn't see the admin's personal task")
    check(a.post("/api/calendar/events", json={"title": "x", "kind": "meeting", "project": "personal", "start_date": day, "start_time": "10:00"}).status_code == 400,
          "a meeting can't be personal")

    # Personal goals (every level) and Personal documents: only their maker's, whatever the access
    def goal(client, text, **k):
        r = client.post("/api/calendar/goals", json={"text": text, **k})
        return r.get_json().get("goal_id") if r.status_code == 200 else None
    def plan(client):
        return str(client.get("/api/calendar/goals/plan?brand=all").get_json())
    admin_life, member_life = goal(a, "AdminLife", period="year"), goal(m, "MemberLife", period="mid", brand="personal")
    nexy_goal = goal(a, "NexyShared", period="month", brand="nexy", shared=True)
    check(admin_life and member_life and nexy_goal, "long, mid and short-term goals can be added, for Personal and a brand")
    check("AdminLife" not in plan(m) and "MemberLife" in plan(m), "a member sees their own personal goals and not the admin's")
    check("MemberLife" not in plan(a) and "AdminLife" in plan(a), "the admin sees their own personal goals and not a member's")
    check(m.put(f"/api/calendar/goals/{admin_life}", json={"done": True}).status_code == 400
          and a.put(f"/api/calendar/goals/{member_life}", json={"done": True}).status_code == 400, "nobody ticks off or changes another's goal")
    step = goal(a, "AdminStep", period="month", parent_id=admin_life)
    check(step and goal(a, "x", parent_id=step) is None, "a goal has steps, and a step has no steps of its own")
    check(a.put(f"/api/calendar/goals/{admin_life}", json={"shared": True}).status_code == 400, "a personal long-term goal can't be shown to the team")
    done = a.put(f"/api/calendar/goals/{admin_life}", json={"done": True}).get_json()
    check(done.get("done_at") and not a.put(f"/api/calendar/goals/{admin_life}", json={"done": False}).get_json().get("done_at"),
          "ticking off keeps the time, unticking clears it")
    def doc(client, title):
        r = client.post("/api/crm/docs", json={"title": title, "brand": "personal", "content": "diaryword"})
        return r.get_json().get("doc_id") if r.status_code == 200 else None
    admin_doc, member_doc = doc(a, "AdminDiary"), doc(m, "MemberDiary")
    check(admin_doc and member_doc, "everyone can write Personal documents")
    check(m.get(f"/api/crm/docs/{admin_doc}").status_code >= 400 and a.get(f"/api/crm/docs/{member_doc}").status_code >= 400,
          "nobody opens another's Personal document")
    check("AdminDiary" not in str(m.get("/api/crm/docs/search?q=diaryword").get_json()) and "MemberDiary" not in str(a.get("/api/crm/docs/search?q=diaryword").get_json())
          and "AdminDiary" not in str(m.get("/api/crm/docs/tree?brand=personal").get_json()), "search and the Personal list show only your own")
    check(a.put(f"/api/crm/docs/{admin_doc}", json={"brand": "nexy"}).status_code >= 400, "a Personal document can't be moved to a brand")

    # An unexpected failure: plain words, no raw error
    from lk_calendar import goals as goals_mod
    real, goals_mod.overview = goals_mod.overview, lambda: 1 / 0
    r = a.get("/api/calendar/goals")
    goals_mod.overview = real
    check(r.status_code == 500 and "went wrong on our side" in r.get_json().get("error", "") and "division" not in r.get_data(as_text=True), "an unexpected failure gives plain words")
    check(a.get("/no-such-page-at-all").status_code in (302, 404), "normal web errors stay as they are")

    # Recently deleted: a deleted contact comes back whole, and only for whoever may see the bin
    gone = contact(name="Delete Me", email="gone@t.test", brand="nexy")
    a.post(f"/api/crm/people/{gone}/notes", json={"text": "binnote"})
    a.post(f"/api/calendar/people/{gone}/outreach", json={"channel": "email", "status": "email sent", "follow_up_days": 3})
    check(a.delete(f"/api/calendar/contacts/{gone}").status_code < 400 and a.get(f"/api/calendar/contacts/{gone}").status_code == 404, "deleting a contact removes it")
    binned = [d for d in a.get("/api/crm/recycle").get_json()["deleted"] if d["person_id"] == gone]
    check(len(binned) == 1 and binned[0]["days_left"] == 30, "a deleted contact is in Recently deleted for 30 days")
    check(gone not in [d["person_id"] for d in m.get("/api/crm/recycle").get_json()["deleted"]], "a member doesn't see what others deleted")
    check(m.post(f"/api/crm/recycle/{binned[0]['bin_id']}/restore").status_code >= 400, "a member can't restore what others deleted")
    same = contact(name="Same Email", email="gone@t.test", brand="nexy")
    check(a.post(f"/api/crm/recycle/{binned[0]['bin_id']}/restore").status_code == 400, "no restore while another contact uses that email")
    a.delete(f"/api/calendar/contacts/{same}")
    check(a.post(f"/api/crm/recycle/{binned[0]['bin_id']}/restore").status_code == 200, "a deleted contact can be restored")
    back = a.get(f"/api/calendar/contacts/{gone}")
    check(back.status_code == 200 and back.get_json().get("email") == "gone@t.test", "the restored contact is back with its email")
    check("binnote" in str(a.get(f"/api/crm/people/{gone}/notes").get_json()), "its notes come back")
    check(a.get(f"/api/calendar/people/{gone}/outreach").get_json() and "email" in str(a.get(f"/api/calendar/people/{gone}/outreach").get_json()), "its statuses come back")
    check(any(d.get("person_id") == gone for d in a.get("/api/crm/overview").get_json()["people"]), "it shows in the CRM again")


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
print("BRAND CHECK PASSED" if not problems else f"BRAND CHECK FAILED: {len(problems)} problem(s)")
sys.exit(0 if not problems else 1)
