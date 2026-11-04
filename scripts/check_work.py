#!/usr/bin/env python3
"""Checks Work pipelines (a test feature) on a fresh installation in a temporary folder (never the real data):
off by default (nothing to create, no Work tools or options for Claude, no Work routes); switched on, a work pipeline holds items
with sub-items and never contacts; work items never show up in sales (opportunity routes, search, company pages, suggestions,
conversation logging) and nothing moves between the two; sales opportunities keep working as before; Claude's Work tools work and
are shown first. Deploys stop if this fails. Prints WORK CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_work.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-work-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({"LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy.db"), "LYTEKEEPR_PASSWORD": "work-test",
                   "LYTEKEEPR_SECRET_KEY": "work-test", "LYTEKEEPR_ADMIN_EMAIL": "owner@example.com", "LK_GMAIL_DAILY": "0",
                   "LK_GOOGLE_AUTOSYNC": "0", "LK_FATHOM_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": ""})
sys.path.insert(0, str(APP_DIR))
import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print("WORK CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
    sys.exit(2)
import google_oauth.store as gstore  # noqa: E402
gstore.LEGACY_DIR = TMP / "legacy-google"
import auth_gate  # noqa: E402
auth_gate._notify_signup = lambda email, url: None
import server  # noqa: E402
from lk_calendar import claude_connector, company_view, directory, opportunities, relationships, superadmin  # noqa: E402
superadmin._alert = lambda *a: None
import logging  # noqa: E402
server.app.logger.setLevel(logging.CRITICAL)

app = server.app
problems = []
a = app.test_client()


def check(ok, what):
    if not ok:
        problems.append(what)


def call(method, path, body=None):
    r = a.open(path, method=method, json=body)
    return r.status_code, r.get_json()


def need(st, data):
    if st >= 400:
        raise RuntimeError(f"{st} {data}")
    return data


def tool(name, args):
    return claude_connector._run_tool(call, name, dict(args))


def shown_then_done(name, args):
    shown = tool(name, args)
    return shown, tool(name, dict(args, confirm_code=shown.get("confirm_code")))


def main():
    a.post("/login", data={"password": "work-test"})
    home = next(x for x in directory.list_accounts() if x["profile_id"] == _profiles.home_profile_id())

    # off by default
    check(a.post("/api/crm/funnels", json={"name": "Ideas", "brand": "lytekeepr", "kind": "work"}).status_code == 403, "a work pipeline was created while switched off")
    check(a.get("/api/crm/work/funnels/1/items").status_code == 403, "the Work routes answer while switched off")
    check(not ({t["name"] for t in claude_connector.tool_list(work=directory.work_enabled(home["profile_id"]))} & claude_connector.WORK_TOOLS),
          "Claude sees Work tools while switched off")

    # sales first, to prove it keeps working
    sales = a.post("/api/crm/funnels", json={"name": "Sales", "brand": "lytekeepr"}).get_json()["funnel_id"]
    pid = a.post("/api/calendar/contacts", json={"name": "Sam Sales", "email": "sam@client.test", "brand": "lytekeepr", "company": "Acme"}).get_json()["id"]
    a.post("/api/crm/funnels/move", json={"person_ids": [pid], "funnel_id": sales})
    opp = a.post("/api/crm/opportunities", json={"name": "Acme deal", "funnel_id": sales, "company": "Acme", "participants": [{"contact_id": pid}]})
    check(opp.status_code == 201, f"a sales opportunity can no longer be created: {opp.get_json()}")
    sales_opp = opp.get_json()["opportunity_id"]

    # switched on
    directory.set_work_pipelines(home["id"], True)
    shown, made = shown_then_done("create_funnel", {"name": "Content", "brand": "lytekeepr", "kind": "work", "item_name": "Idea"})
    check("work pipeline" in (shown.get("show_the_user") or "").lower() and "To do → In progress → Done" in (shown.get("show_the_user") or ""),
          f"creating a work pipeline is not shown first with its stages: {shown}")
    work = made.get("funnel_id")
    funnels = {f["funnel_id"]: f for f in a.get("/api/crm/funnels").get_json()["funnels"]}
    check(funnels.get(work, {}).get("kind") == "work" and funnels[work].get("item_name") == "Idea", f"the work pipeline is not marked as work: {funnels.get(work)}")
    custom = a.post("/api/crm/funnels", json={"name": "Orders", "brand": "lytekeepr", "kind": "work", "stages": ["Received", "Making", "Shipped"]}).get_json()["funnel_id"]
    check([s["name"] for s in a.get(f"/api/crm/funnels/{custom}/board").get_json()["stages"]] == ["Received", "Making", "Shipped"], "a work pipeline's own stages were not used")

    # items and sub-items through Claude
    _, item = shown_then_done("create_work_item", {"funnel_id": work, "name": "Post about pricing"})
    iid = item.get("item_id")
    check(item.get("stage") == "To do", f"a new item is not in the first stage: {item}")
    _, sub = shown_then_done("add_sub_item", {"item_id": iid, "name": "Draft"})
    _, sub = shown_then_done("add_sub_item", {"item_id": iid, "name": "Images", "note": "two"})
    subs = sub.get("subitems", [])
    check([s["name"] for s in subs] == ["Draft", "Images"] and subs[1]["note"] == "two", f"sub-items were not added: {subs}")
    _, upd = shown_then_done("update_sub_item", {"subitem_id": subs[0]["subitem_id"], "done": True})
    check(upd["subitems"][0]["done"], "a sub-item could not be marked done")
    _, moved = shown_then_done("update_work_item", {"item_id": iid, "stage": "in progress", "notes": "for Friday"})
    check(moved.get("stage") == "In progress" and moved.get("notes") == "for Friday", f"an item could not be moved or noted: {moved}")
    listed = tool("work_items", {"funnel_id": work})
    check(listed["item_word"] == "Idea" and listed["items"][0]["subitems"][0]["done"], f"Claude's view of the pipeline is wrong: {listed}")
    board = a.get(f"/api/crm/funnels/{work}/board").get_json()
    cards = [o for st in board["stages"] for o in st.get("opportunities") or []]
    check(cards and cards[0].get("kind") == "work" and cards[0]["subitems"] == {"done": 1, "total": 2}, f"the board doesn't show the item with its sub-items: {cards}")
    _, rm = shown_then_done("update_sub_item", {"subitem_id": subs[1]["subitem_id"], "remove": True})
    check(len(rm["subitems"]) == 1, "a sub-item could not be removed")

    # never mixed with sales
    check(a.post("/api/crm/funnels/move", json={"person_ids": [pid], "funnel_id": work}).status_code == 400, "a contact was put into a work pipeline")
    first_work_stage = a.get(f"/api/crm/funnels/{work}/board").get_json()["stages"][0]["stage_id"]
    check(a.post("/api/crm/funnels/stages/move", json={"person_id": pid, "stage_id": first_work_stage, "confirm": True}).status_code == 400,
          "a contact was moved onto a work pipeline's stage")
    check(a.get(f"/api/crm/opportunities/{iid}").status_code >= 400, "the sales routes return a work item")
    check(a.post(f"/api/crm/opportunities/{iid}/participants", json={"contact_id": pid}).status_code >= 400, "a contact was added to a work item")
    check(a.post("/api/crm/opportunities", json={"name": "Sneaky", "funnel_id": work}).status_code >= 400, "a sales opportunity was created in a work pipeline")
    check(a.post("/api/crm/work/items", json={"name": "Sneaky", "funnel_id": sales}).status_code >= 400, "a work item was created in a sales funnel")
    sales_stage = a.get(f"/api/crm/funnels/{sales}/board").get_json()["stages"][1]["stage_id"]
    check(a.post(f"/api/crm/work/items/{iid}/move", json={"stage_id": sales_stage}).status_code >= 400, "a work item moved into a sales funnel")
    check(a.post(f"/api/crm/opportunities/{sales_opp}/move", json={"stage_id": first_work_stage}).status_code >= 400, "a sales opportunity moved into a work pipeline")
    check(not [o for o in opportunities.search("pricing") if o["opportunity_id"] == iid], "a work item shows up in opportunity search")
    check(all(not x["work"] for x in [opportunities.get(sales_opp)]), "a sales opportunity is marked as work")
    found = tool("find", {"query": "pricing"})
    check("Post about pricing" not in json.dumps(found), "Claude's find returns a work item as a sales opportunity")
    try:
        tool("create_opportunity", {"name": "Sneaky", "funnel_id": work, "confirm_code": "x"})
    except Exception:
        pass
    check(len([o for st in a.get(f"/api/crm/funnels/{work}/board").get_json()["stages"] for o in st.get("opportunities") or []]) == 1,
          "Claude's sales tool put something into a work pipeline")

    # sales still works exactly as before
    o = a.get(f"/api/crm/opportunities/{sales_opp}").get_json()
    check(o.get("name") == "Acme deal" and len(o.get("participants", [])) == 1 and not o.get("work"), f"a sales opportunity changed: {o}")
    check(a.post(f"/api/crm/opportunities/{sales_opp}/move", json={"stage_id": sales_stage}).status_code == 200, "a sales opportunity can no longer move")

    # delete takes the sub-items with it
    _, gone = shown_then_done("update_work_item", {"item_id": iid, "delete": True})
    check(gone.get("deleted") and a.get(f"/api/crm/work/items/{iid}").status_code >= 400, "a work item could not be deleted")
    from lk_calendar import work as work_mod
    check(work_mod.subitems(iid) == [], "a deleted item's sub-items were left behind")

    # switched off again: gone from sight, data kept
    directory.set_work_pipelines(home["id"], False)
    check(work not in {f["funnel_id"] for f in a.get("/api/crm/funnels").get_json()["funnels"]}, "work pipelines still show after switching off")
    check(not ({t["name"] for t in claude_connector.tool_list(work=directory.work_enabled(home["profile_id"]))} & claude_connector.WORK_TOOLS),
          "Claude still sees Work tools after switching off")
    directory.set_work_pipelines(home["id"], True)
    check(work in {f["funnel_id"] for f in a.get("/api/crm/funnels").get_json()["funnels"]}, "switching back on lost the work pipeline")


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
    print("WORK CHECK FAILED")
    sys.exit(1)
print("WORK CHECK PASSED")
