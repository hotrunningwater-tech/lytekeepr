#!/usr/bin/env python3
"""Checks funnels and their automatic moves on a fresh installation in a temporary folder (never the real data):
the standard funnel moves exactly as before (by stage name); a funnel from a template moves by its pattern and its stages' marks
(P1: sent -> contacted, replied -> conversation; P2-P4: no move on messages; every pattern: not interested -> lost, a bounce moves
nothing, a calendar booking -> the first booked stage, never further, never back, never to won); a card moved by hand sets the
matching status; renaming a template funnel's stage changes nothing shared; Claude creates a funnel from a template, shown first;
the template list is whole; a new workspace starts without the owner's brands. Deploys stop if this fails.
Prints FUNNEL CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_funnels.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-funnels-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({"LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy.db"), "LYTEKEEPR_PASSWORD": "funnel-test",
                   "LYTEKEEPR_SECRET_KEY": "funnel-test", "LYTEKEEPR_ADMIN_EMAIL": "owner@example.com", "LK_GMAIL_DAILY": "0",
                   "LK_GOOGLE_AUTOSYNC": "0", "LK_FATHOM_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": ""})
sys.path.insert(0, str(APP_DIR))
import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print("FUNNEL CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
    sys.exit(2)
import google_oauth.store as gstore  # noqa: E402
gstore.LEGACY_DIR = TMP / "legacy-google"
import auth_gate  # noqa: E402
auth_gate._notify_signup = lambda email, url: None
import server  # noqa: E402
from lk_calendar import claude_connector, contacts, directory, funnel_rules, store, superadmin  # noqa: E402
superadmin._alert = lambda *a: None
import logging  # noqa: E402
server.app.logger.setLevel(logging.CRITICAL)

app = server.app
problems = []


def check(ok, what):
    if not ok:
        problems.append(what)


a = app.test_client()


def stage_of(pid):
    with contacts._crm() as c:
        row = c.execute("SELECT fs.name FROM people p LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id WHERE p.person_id = ?", (pid,)).fetchone()
    return (row["name"] or "") if row else ""


def funnel_stages(funnel_id):
    with contacts._crm() as c:
        return [dict(r) for r in c.execute("SELECT stage_id, name, is_won, mark FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (funnel_id,))]


def new_person(name, funnel_id):
    pid = a.post("/api/calendar/contacts", json={"name": name, "email": f"{name.lower().replace(' ', '.')}@client.test", "brand": "lytekeepr"}).get_json()["id"]
    a.post("/api/crm/funnels/move", json={"person_ids": [pid], "funnel_id": funnel_id})
    return pid


def status(pid, s):
    contacts.set_outreach(pid, "email", s)


def hand_move(pid, funnel_id, stage_name, confirm=True):
    sid = next(s["stage_id"] for s in funnel_stages(funnel_id) if s["name"] == stage_name)
    return a.post("/api/crm/funnels/stages/move", json={"person_id": pid, "stage_id": sid, "confirm": confirm})


def meeting(email, declined=False, when=None):
    with store._conn() as conn:
        conn.execute("""INSERT INTO events (title, kind, project, start_date, start_time, duration_min, notes, source, created_at, attendees)
                        VALUES ('Call', 'meeting', 'lytekeepr', ?, '10:00', 30, '', 'google', ?, ?)""",
                     ((when or date.today()).isoformat(), date.today().isoformat(), json.dumps([{"email": email, "name": "x", **({"declined": True} if declined else {})}])))


def main():
    a.post("/login", data={"password": "funnel-test"})

    # the list itself
    ts = funnel_rules.templates()
    check([t["number"] for t in ts] == list(range(1, 48)), "the template list is not numbered 1-47")
    for t in ts:
        marks = [s["mark"] for s in t["stages"]]
        if t["pattern"] not in funnel_rules.PATTERNS or marks[0] != "new" or marks[-1] != "lost" or marks[-2] != "won" or marks.count("won") != 1:
            problems.append(f"template {t['number']} is not new ... won / lost with a pattern")
        if ("contacted" in marks or "conversation" in marks) != (t["pattern"] == "P1"):
            problems.append(f"template {t['number']}: only outbound (P1) moves on messages and replies")
        if any(s["mark"] == "booked" and not s["name"].endswith("Booked") for s in t["stages"]):
            problems.append(f"template {t['number']}: a booked mark on a stage that isn't a Booked stage")
    check(a.get("/api/crm/funnel-templates").get_json()["templates"][0]["name"] == ts[0]["name"], "the app does not serve the template list")

    # the standard funnel: exactly as before
    std = a.post("/api/crm/funnels", json={"name": "Standard", "brand": "lytekeepr"}).get_json()["funnel_id"]
    p = new_person("Std One", std)
    status(p, "sent")
    check(stage_of(p) == "Contacted", f"standard funnel: an email sent no longer moves to Contacted ({stage_of(p)})")
    status(p, "replied")
    check(stage_of(p) == "In conversation", f"standard funnel: a reply no longer moves to In conversation ({stage_of(p)})")
    q = new_person("Std Two", std)
    status(q, "bounced")
    check(stage_of(q) == "Lost", f"standard funnel: a bounce no longer moves to Lost as before ({stage_of(q)})")
    r = new_person("Std Three", std)
    hand_move(r, std, "Lost")
    check(contacts.outreach(r)["channels"][0]["status"] == "not interested", "standard funnel: moving to Lost by hand no longer sets not interested")

    # P1, outbound (#1)
    p1 = a.post("/api/crm/funnels", json={"name": "Outbound", "brand": "lytekeepr", "template": 1}).get_json()["funnel_id"]
    stages = funnel_stages(p1)
    check([s["name"] for s in stages] == [s["name"] for s in ts[0]["stages"]] and [s["is_won"] for s in stages].count(1) == 1,
          "a funnel from template 1 does not have its stages and won stage")
    o = new_person("Out One", p1)
    status(o, "sent")
    check(stage_of(o) == "First Contact", f"P1: an email sent does not move to First Contact ({stage_of(o)})")
    status(o, "replied")
    check(stage_of(o) == "Conversation Started", f"P1: a reply does not move to Conversation Started ({stage_of(o)})")
    o2 = new_person("Out Two", p1)
    status(o2, "bounced")
    check(stage_of(o2) == "Prospect Identified", f"P1: a bounce moved the deal ({stage_of(o2)}); it must only mark the address")
    status(o2, "done")
    check(stage_of(o2) == "Closed Lost", f"P1: not interested does not move to the lost stage ({stage_of(o2)})")
    o3 = new_person("Out Three", p1)
    warned = hand_move(o3, p1, "Lead Qualified", confirm=False)
    check(warned.status_code == 409, "P1: moving past the conversation stage with no reply gives no warning")
    hand_move(o3, p1, "Conversation Started")
    check(contacts.outreach(o3)["channels"][0]["status"] == "replied", "P1: moving to Conversation Started by hand does not set replied")

    # P4, milestones (#2): messages move nothing, not interested is lost
    p4 = a.post("/api/crm/funnels", json={"name": "Projects", "brand": "lytekeepr", "template": 2}).get_json()["funnel_id"]
    m = new_person("Mile One", p4)
    status(m, "sent")
    status(m, "replied")
    check(stage_of(m) == "New Lead", f"P4: a message or reply moved the deal ({stage_of(m)})")
    hand_move(m, p4, "Proposal Sent")
    check(contacts.outreach(m)["channels"][0]["status"] == "replied", "P4: a hand move changed the status")
    status(m, "done")
    check(stage_of(m) == "Closed Lost", f"P4: not interested does not move to the lost stage ({stage_of(m)})")

    # bookings (#9 has two booked stages; #2 has none)
    p9 = a.post("/api/crm/funnels", json={"name": "Enterprise", "brand": "lytekeepr", "template": 9}).get_json()["funnel_id"]
    b1, b2, b3 = new_person("Book One", p9), new_person("Book Two", p9), new_person("Book Three", p9)
    hand_move(b3, p9, "Technical Review")
    meeting("book.one@client.test")
    meeting("book.two@client.test", declined=True)
    meeting("book.three@client.test")
    meeting("mile.one@client.test")
    funnel_rules.apply_bookings()
    funnel_rules.apply_bookings()                       # again: the same meeting never counts twice
    check(stage_of(b1) == "Discovery Booked", f"a booking does not move to the first booked stage, or moved past it ({stage_of(b1)})")
    check(stage_of(b2) == "New Lead", f"a declined invite moved someone ({stage_of(b2)})")
    check(stage_of(b3) == "Technical Review", f"a booking moved someone back ({stage_of(b3)})")
    check(stage_of(m) == "Closed Lost", "a booking moved a lost deal")
    b4 = new_person("Book Four", p9)
    meeting("book.four@client.test", when=date(2020, 1, 1))
    funnel_rules.apply_bookings()
    check(stage_of(b4) == "New Lead", "a meeting from years ago counted as booked")
    w = new_person("Book Won", p9)
    hand_move(w, p9, "Contract Signed")
    meeting("book.won@client.test")
    funnel_rules.apply_bookings()
    check(stage_of(w) == "Contract Signed", "a booking moved a won deal")
    s1 = new_person("Std Book", std)
    meeting("std.book@client.test")
    funnel_rules.apply_bookings()
    check(stage_of(s1) == "New lead", "a booking moved someone in the standard funnel (which keeps its old rules)")

    # renaming a template funnel's stage changes nothing shared
    with store._conn() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM status_stages ORDER BY channel, status_key")]
    sid = next(s["stage_id"] for s in funnel_stages(p1) if s["name"] == "First Contact")
    a.post(f"/api/crm/funnels/stages/{sid}/rename", json={"name": "Contacted"})
    with store._conn() as conn:
        after = [tuple(r) for r in conn.execute("SELECT * FROM status_stages ORDER BY channel, status_key")]
    check(before == after, "renaming a template funnel's stage changed the rules of other funnels")
    o4 = new_person("Out Four", p1)
    status(o4, "sent")
    check(stage_of(o4) == "Contacted", f"a renamed stage lost its mark ({stage_of(o4)})")

    # Claude: picks a template, shows it first, creates it
    def call(method, path, body=None):
        resp = a.open(path, method=method, json=body)
        return resp.status_code, resp.get_json()

    def need(st, data):
        if st >= 400:
            raise RuntimeError(f"{st} {data}")
        return data
    listed = claude_connector._run_tool(call, "funnel_templates", {})
    check(len(listed) == 47 and listed[0]["stages"] and listed[0]["recognise"], "Claude does not get the template list")
    args = {"name": "Coaching clients", "brand": "lytekeepr", "template": 26}
    shown = claude_connector._run_tool(call, "create_funnel", dict(args))
    check("Brand: LyteKeepr" in (shown.get("show_the_user") or "") and "Brand: lytekeepr" not in (shown.get("show_the_user") or ""),
          f"the confirmation shows the brand's internal key instead of its name: {shown.get('show_the_user')}")
    check("Intro Booked" in (shown.get("show_the_user") or "") and not any(f["name"] == "Coaching clients" for f in a.get("/api/crm/funnels").get_json()["funnels"]),
          f"creating a funnel is not shown first with its stages: {shown}")
    made = claude_connector._run_tool(call, "create_funnel", dict(args, confirm_code=shown.get("confirm_code")))
    got = funnel_stages(made.get("funnel_id"))
    check([s["name"] for s in got] == [s["name"] for s in ts[25]["stages"]], f"Claude's funnel is not template 26: {got}")
    try:
        claude_connector._run_tool(call, "create_funnel", {"name": "Bad", "brand": "lytekeepr", "template": 99})
        problems.append("a funnel from a template that doesn't exist was accepted")
    except Exception:
        pass

    # Drop a list on Home, Claude plans it, USER checks the plan on the Import page and imports: nothing exists before that click
    import io
    def drop(text, name):
        return a.post("/api/calendar/import/scan", data={"file": (io.BytesIO(text.encode()), name)}, content_type="multipart/form-data").get_json()
    def import_step():
        return next(x for x in a.get("/api/calendar/getting-started").get_json()["steps"] if x["key"] == "import")
    def people_count():
        return len(a.get("/api/calendar/contacts").get_json().get("contacts") or [])
    # the test account's case: a list imported earlier and the same file dropped again: Claude must take the dropped one, nothing else
    same = "Name,Email\nDee Park,dee@park.test\n"
    first = drop(same, "same.csv")
    a.post("/api/calendar/import/run", json={"token": first["token"], "mapping": {"Name": "name", "Email": "email"},
                                             "options": {"brand": "lytekeepr", "duplicates": "skip", "batch": "same"}})
    drop(same, "same.csv")
    for tool, args in (("imported_list", {}), ("organize_imported", {"funnel_id": 1, "placements": [{"stage": "New lead", "person_ids": [1]}]})):
        got = claude_connector._run_tool(call, tool, args)
        check(got.get("use_instead") == "dropped_list" and "show_the_user" not in got, f"{tool} ran while a dropped file was waiting: {got}")
    a.post("/api/calendar/getting-started", json={"start_over": "import"})
    # an imported list organized into a NEW cold outreach funnel: asked first, made only after the yes
    listed = claude_connector._run_tool(call, "imported_list", {})
    dee = next(p["person_id"] for p in listed["people"] if p["name"] == "Dee Park")
    args = {"new_funnel": {"new": "cold", "name": "Hand list: Cold outreach"}, "placements": [{"stage": "Prospect found", "person_ids": [dee]}]}
    count = len(a.get("/api/crm/funnels").get_json()["funnels"])
    shown = claude_connector._run_tool(call, "organize_imported", dict(args))
    check("cold outreach funnel Hand list: Cold outreach" in (shown.get("show_the_user") or "") and "Prospect found: 1" in shown.get("show_the_user", "")
          and len(a.get("/api/crm/funnels").get_json()["funnels"]) == count, f"a new cold funnel for an imported list isn't shown first: {shown}")
    claude_connector._run_tool(call, "organize_imported", dict(args, confirm_code=shown.get("confirm_code")))
    check(stage_of(dee) == "Prospect found" and len(a.get("/api/crm/funnels").get_json()["funnels"]) == count + 1, "the imported list wasn't put in a new cold funnel")

    # a list imported by hand that nobody organized (the test account today): Start over stops it asking, the contacts stay
    ran = a.post("/api/calendar/import/run", json={"token": drop("Name,Email\nCy Noor,cy@noor.test\n", "byhand.csv")["token"], "mapping": {"Name": "name", "Email": "email"},
                                                    "options": {"brand": "lytekeepr", "duplicates": "skip", "batch": "by hand"}}).get_json()
    step = import_step()
    check(ran.get("added") == 1 and "organize the list I just imported" in step.get("say", "") and step.get("start_over"),
          f"a list imported by hand doesn't ask to be organized, with Start over: {step}")
    a.post("/api/calendar/getting-started", json={"start_over": "import"})
    step = import_step()
    check(step.get("action") == "import" and not step.get("say") and not step["done"], f"Start over doesn't bring back a plain 'Import a list': {step}")
    check(any(c.get("name") == "Cy Noor" for c in a.get("/api/calendar/contacts?q=Cy").get_json()["contacts"]), "Start over removed imported contacts")
    drop("Instagram,Bio\nhttps://instagram.com/wrongfile,Oops\n", "wrong.csv")
    check(import_step().get("start_over"), "a dropped file can't be started over")
    a.post("/api/calendar/getting-started", json={"start_over": "import"})
    step = import_step()
    check(step.get("action") == "import" and not step.get("start_over") and not step.get("say"), f"Start over doesn't bring back a plain 'Import a list': {step}")
    drop("Instagram,Bio\nhttps://instagram.com/coldone,Study abroad\nhttps://instagram.com/coldtwo,Tutors\n", "cold.csv")
    step = import_step()
    check(not step["done"] and step.get("chat") and step.get("say") == "In LyteKeepr, organize the list I just dropped.", f"after a drop, the step doesn't hand over to Claude: {step}")
    dropped = claude_connector._run_tool(call, "dropped_list", {})
    main_f = next((f for f in dropped.get("funnels") or [] if f["main"]), None)
    cold_stages = dropped.get("new_cold_outreach_stages") or []
    check(dropped.get("file") == "cold.csv" and dropped.get("people") == 2 and main_f, f"Claude doesn't see the dropped list and the main funnel: {dropped}")
    check(cold_stages[:3] == ["Prospect found", "First message sent", "Follow-up sent"] and cold_stages[3:] == main_f["stages"],
          f"a cold outreach funnel isn't three stages in front of the main funnel: {cold_stages} / {main_f}")
    check("plan_import" not in claude_connector.SHOWN_FIRST, "planning an import asks twice")
    before, funnels_before = people_count(), len(a.get("/api/crm/funnels").get_json()["funnels"])
    saved = claude_connector._run_tool(call, "plan_import", {"brand": "lytekeepr", "funnel": {"new": "cold", "name": main_f["name"] + ": Cold outreach"}})
    token = dropped["token"]
    check(saved.get("review_link", "").endswith("/?import=" + token) and "Prospect found 2" in saved.get("summary", "") and "coldone" not in json.dumps(saved),
          f"the plan doesn't come back as totals and a review link: {saved}")
    check(people_count() == before and len(a.get("/api/crm/funnels").get_json()["funnels"]) == funnels_before, "planning created contacts or a funnel")
    step = import_step()
    check(step.get("action") == "import_review" and step.get("link", "").endswith("?import=" + token) and not step["done"], f"the step doesn't offer the review: {step}")
    review = a.get("/api/calendar/import/plan?token=" + token).get_json()
    check(review.get("counts") == {"by_value": {}, "default": 2} and review["plan"]["funnel"]["stages"] == cold_stages and review["scan"]["rows"] == 2,
          f"the review page doesn't get the plan: {review}")
    plan_f = dict(review["plan"]["funnel"], name="Signs: Cold outreach", stage="Prospect found")
    ran = a.post("/api/calendar/import/run", json={"token": token, "mapping": review["plan"]["mapping"],
                                                    "options": {"brand": "lytekeepr", "duplicates": "skip", "batch": "b", "plan": {"funnel": plan_f}}}).get_json()
    check(ran.get("added") == 2 and (ran.get("funnel") or {}).get("placed") == {"Prospect found": 2}, f"importing the plan didn't place everyone: {ran}")
    cold_id = (ran.get("funnel") or {}).get("funnel_id")
    check([s["name"] for s in funnel_stages(cold_id)] == cold_stages, "the new cold outreach funnel doesn't have the planned stages")
    check(import_step()["done"], f"'Import a list' is not done after importing Claude's plan: {import_step()}")
    check(a.get("/api/calendar/import/plan?token=" + token).status_code == 409, "an imported plan can be imported again")
    # the next list joins the existing cold outreach funnel, a stage by the file's Status column
    drop("Instagram,Status\nhttps://instagram.com/coldthree,replied\nhttps://instagram.com/coldfour,\n", "cold2.csv")
    again = claude_connector._run_tool(call, "dropped_list", {})
    check(any(f["funnel_id"] == cold_id and f["cold_outreach"] for f in again["funnels"]), f"Claude doesn't see the cold outreach funnel as one: {again['funnels']}")
    first_main = main_f["stages"][0]
    saved = claude_connector._run_tool(call, "plan_import", {"brand": "lytekeepr", "funnel": {"funnel_id": cold_id, "column": "Status", "values": {"Replied": first_main}}})
    check(f"{first_main} 1" in saved["summary"] and "Prospect found 1" in saved["summary"], f"placing by a column's value doesn't plan right: {saved}")
    review = a.get("/api/calendar/import/plan?token=" + again["token"]).get_json()
    ran = a.post("/api/calendar/import/run", json={"token": again["token"], "mapping": review["plan"]["mapping"],
                                                    "options": {"brand": "lytekeepr", "duplicates": "skip", "plan": {"funnel": review["plan"]["funnel"]}}}).get_json()
    check((ran.get("funnel") or {}).get("placed") == {first_main: 1, "Prospect found": 1} and len(a.get("/api/crm/funnels").get_json()["funnels"]) == funnels_before + 1,
          f"joining the existing funnel didn't place by Status, or made another funnel: {ran}")
    try:
        claude_connector._run_tool(call, "plan_import", {"brand": "lytekeepr"})
        problems.append("a plan was saved with no file waiting")
    except Exception:
        pass

    # A list imported by hand on CRM → Import: Claude organizes it afterwards, everyone by stage in one go, shown as totals
    csv_text = "Name,Email,Company,Status\nAva Stone,ava@stone.test,Stone Co,quote sent\nBen Ruiz,ben@ruiz.test,Ruiz LLC,new lead\n"
    scan = a.post("/api/calendar/import/scan", data={"file": (io.BytesIO(csv_text.encode()), "leads.csv")}, content_type="multipart/form-data").get_json()
    mapping = {c["name"]: c["field"] for c in scan["columns"]}
    ran = a.post("/api/calendar/import/run", json={"token": scan["token"], "mapping": mapping,
                                                    "options": {"brand": "lytekeepr", "duplicates": "skip", "batch": scan["default_batch"]}}).get_json()
    check(ran.get("added") == 2, f"the list was not imported: {ran}")
    listed = claude_connector._run_tool(call, "imported_list", {})
    ids = {p["name"]: p["person_id"] for p in listed["people"]}
    check(set(ids) == {"Ava Stone", "Ben Ruiz"} and any("quote sent" in json.dumps(p) for p in listed["people"]),
          f"Claude doesn't see the imported list with what the file said: {listed}")
    target = made.get("funnel_id")
    names = [s["name"] for s in funnel_stages(target)]
    args = {"funnel_id": target, "placements": [{"stage": names[0], "person_ids": [ids["Ben Ruiz"]]}, {"stage": names[2], "person_ids": [ids["Ava Stone"]]}]}
    shown = claude_connector._run_tool(call, "organize_imported", dict(args))
    text = shown.get("show_the_user") or ""
    check(f"{names[2]}: 1" in text and "Ava Stone" not in text and "Coaching clients" in text, f"organizing is not shown first as totals by stage: {text}")
    check(not stage_of(ids["Ava Stone"]), "organizing changed something before the yes")
    claude_connector._run_tool(call, "organize_imported", dict(args, confirm_code=shown.get("confirm_code")))
    check(stage_of(ids["Ava Stone"]) == names[2] and stage_of(ids["Ben Ruiz"]) == names[0], "the imported people were not placed in their stages")

    # a new USER's workspace starts without the owner's brands
    b = app.test_client()
    server.login_identity = lambda code: {"email": "new-user@example.com", "email_verified": True, "name": "New"}
    b.get(f"/auth/google/callback?state={server._oauth_state_signer().dumps('google-login')}&code=x")
    directory.set_status(next(x for x in directory.list_accounts() if x["owner_email"] == "new-user@example.com")["id"], "active")
    check(b.get("/api/crm/brands").get_json()["brands"] == [], f"a new workspace starts with the owner's brands: {b.get('/api/crm/brands').get_json()['brands']}")
    check(any(x["key"] == "nexy" for x in a.get("/api/crm/brands").get_json()["brands"]), "the owner's workspace lost its brands")


try:
    main()
except Exception as exc:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    problems.append(f"the check crashed: {exc}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

if problems:
    for p in problems:
        print("FAILED:", p)
    print("FUNNEL CHECK FAILED")
    sys.exit(1)
print("FUNNEL CHECK PASSED")
