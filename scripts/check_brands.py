#!/usr/bin/env python3
"""Checks the brand rules on a fresh, empty installation in a temporary folder (never the real data): a team member limited to
some brands never sees a contact, funnel, task or opportunity participant of another brand; "whole brand" and "ticked funnels
only" behave as described in lk_calendar/access.py; a member can only create contacts in their brands and can't change brands;
a member with no brands chosen keeps their old funnel access. Deploys stop if this fails.
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
        a.post(f"/api/calendar/people/{pid}/outreach", json={"channel": "email", "status": "email sent"})
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
