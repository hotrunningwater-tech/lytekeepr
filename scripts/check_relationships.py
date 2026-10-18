#!/usr/bin/env python3
"""Checks the relationship graph on a fresh, empty installation in a temporary folder with made-up emails (never real data):
who-emails-with-whom is counted per thread, you/your team, generic addresses and big group emails are left out, introductions
are noticed, suggestions appear only for repeated patterns, Accept adds the person (with who introduced them), Dismiss is
remembered, and a team member sees only suggestions for opportunities they can access.
Prints RELATIONSHIP CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_relationships.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-rel-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({"LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy.db"), "LYTEKEEPR_PASSWORD": "rel-test",
                   "LYTEKEEPR_SECRET_KEY": "rel-test", "LYTEKEEPR_ADMIN_EMAIL": "me@mycompany.test", "LK_GMAIL_DAILY": "0",
                   "LK_GOOGLE_AUTOSYNC": "0", "LK_FATHOM_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": ""})
sys.path.insert(0, str(APP_DIR))
import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print("RELATIONSHIP CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
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
ME = "me@mycompany.test"


def check(ok, what):
    if not ok:
        problems.append(what)


def mail(n, thread, sender, to, cc=(), body="", day="2026-10-0"):
    """One saved email, the way the Gmail tab stores it."""
    from gmail import storage
    who = lambda e: {"email": e, "name": e.split("@")[0].replace(".", " ").title()}
    msg = {"id": f"m{n}", "threadId": thread, "from": who(sender), "to": [who(e) for e in to], "cc": [who(e) for e in cc],
           "subject": "Re: pilot", "receivedAt": f"2026-10-{10 + n:02d}T10:00:00", "textBody": body, "snippet": body[:80]}
    with _profiles.use_profile("home"):
        storage.save_message(ME, msg)
        storage.upsert_index_entry(ME, msg)


def main():
    a = app.test_client()
    a.post("/login", data={"password": "rel-test"})
    def contact(name, email):
        return a.post("/api/calendar/contacts", json={"name": name, "email": email}).get_json()["id"]
    sarah = contact("Sarah Intro", "sarah@alpha.test")
    anna = contact("Anna Weber", "anna@gamma.test")
    fid = a.post("/api/crm/funnels", json={"name": "Partnerships"}).get_json()["funnel_id"]
    oid = a.post("/api/crm/opportunities", json={"name": "Alpha pilot", "funnel_id": fid}).get_json()["opportunity_id"]
    a.post(f"/api/crm/opportunities/{oid}/participants", json={"contact_id": sarah})

    # thread T1: Sarah and me, then Sarah brings Michael in (CC). Thread T2: Sarah and Michael again. Thread T3: Sarah + Dave once.
    mail(1, "T1", "sarah@alpha.test", [ME])
    mail(2, "T1", "sarah@alpha.test", [ME], cc=["michael@beta.test"], body="Looping in Michael who runs ops.\n\nOn Mon, Oct 5, 2026 at 9:00 Anna Weber wrote:\n> old")
    mail(3, "T2", "michael@beta.test", [ME, "sarah@alpha.test"], body="Following up. I spoke with Anna Weber yesterday.")
    mail(4, "T3", "sarah@alpha.test", [ME], cc=["dave@delta.test"])
    mail(5, "T4", "noreply@notifications.test", [ME, "sarah@alpha.test"], cc=["michael@beta.test"])
    mail(6, "T5", "sarah@alpha.test", [ME] + [f"p{i}@crowd.test" for i in range(11)], cc=["michael@beta.test"])
    out = a.post("/api/crm/relationships/refresh").get_json()
    check(out["emails"] == 6, "every saved email is read once")
    check(a.post("/api/crm/relationships/refresh").get_json()["emails"] == 0, "a second run reads nothing new")
    from lk_calendar import relationships as R
    with _profiles.use_profile("home"):
        with R._crm() as c:
            people = {r["email"] for r in c.execute("SELECT email FROM rel_people")}
            check(ME not in people, "you are never in the graph")
            check("noreply@notifications.test" not in people, "generic addresses are left out")
            check(not any(e.endswith("@crowd.test") for e in people), "big group emails are left out")
            pair = c.execute("SELECT COUNT(DISTINCT thread_id) FROM rel_pair_threads WHERE a = 'michael@beta.test' AND b = 'sarah@alpha.test'").fetchone()[0]
            check(pair == 2, f"Sarah and Michael share 2 threads (got {pair})")
            intro = c.execute("SELECT introducer FROM rel_intros WHERE newcomer = 'michael@beta.test'").fetchone()
            check(intro and intro["introducer"] == "sarah@alpha.test", "Sarah is seen as bringing Michael in")
            m = c.execute("SELECT COUNT(*) FROM rel_mentions WHERE person_id = ?", (anna,)).fetchone()[0]
            check(m == 1, f"a full-name mention counts only in the new text, not the quoted part (got {m})")
    sug = a.get("/api/crm/suggestions").get_json()["suggestions"]
    kinds = {(s["kind"], s["email"]) for s in sug}
    check(("add_to_opportunity", "michael@beta.test") in kinds, "Michael is suggested for the Alpha pilot")
    check(not any(s["email"] == "dave@delta.test" for s in sug), "one shared thread is not enough for a suggestion")
    check(("new_contact", "michael@beta.test") not in kinds, "no separate 'new contact' card when adding him to the opportunity already creates the contact")
    conns = a.get(f"/api/crm/people/{sarah}/connections").get_json()["connections"]
    check(conns and conns[0]["email"] == "michael@beta.test" and conns[0]["threads"] == 2, "Sarah's connections list Michael first")

    # a team member without access to the funnel sees no suggestions for it, and no new-contact ones
    uid = a.post("/api/team", json={"email": "member@mycompany.test", "name": "Member"}).get_json()["user"]["id"]
    a.post(f"/api/team/{uid}/access", json={"access": "funnels"})
    m = app.test_client()
    server.login_identity = lambda code: {"email": "member@mycompany.test", "email_verified": True, "name": "Member"}
    m.get(f"/auth/google/callback?state={server._oauth_state_signer().dumps('google-login')}&code=x")
    check(m.get("/api/crm/suggestions").get_json()["suggestions"] == [], "a member sees no suggestions for funnels not shared with them")

    # Claude, through the real connector sign-in, sees the same graph
    import base64, hashlib, html, re, secrets, urllib.parse
    cid = a.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}).get_json()["client_id"]
    v = secrets.token_urlsafe(40); ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": cid, "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "code_challenge": ch, "code_challenge_method": "S256"})
    g = html.unescape(re.search(r'name="grant" value="([^"]+)"', a.get("/oauth/authorize?" + q).get_data(as_text=True)).group(1))
    code = urllib.parse.parse_qs(urllib.parse.urlparse(a.post("/oauth/authorize", data={"grant": g, "choice": "allow"}).headers["Location"]).query)["code"][0]
    tok = a.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid, "code_verifier": v}).get_json()["access_token"]
    def tool(name, args):
        r = a.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, headers={"Authorization": "Bearer " + tok}).get_json()["result"]
        return r["isError"], json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"]
    err, rel = tool("find_related_people", {"opportunity_id": oid})
    check(not err and rel["maybe"] and rel["maybe"][0]["email"] == "michael@beta.test" and "why" in rel["maybe"][0], "Claude finds Michael as related to the opportunity, with the reason")
    check(not err and any(m["email"] == "dave@delta.test" for m in rel["maybe"]), "Claude's search also shows weaker links (one thread) for the user to judge")
    err, conn = tool("who_is_connected", {"contact_id": sarah})
    check(not err and conn[0]["email"] == "michael@beta.test", "Claude sees Sarah's connections")
    err, lst = tool("list_suggestions", {"opportunity_id": oid})
    check(not err and len(lst) == 1, "Claude lists the suggestion for this opportunity")

    add = next(s for s in sug if s["kind"] == "add_to_opportunity")
    r = a.post(f"/api/crm/suggestions/{add['suggestion_id']}/accept").get_json()
    o = a.get(f"/api/crm/opportunities/{oid}").get_json()
    mich = next((p for p in o["participants"] if p["name"].lower().startswith("michael")), None)
    check(mich is not None and mich["introduced_by_name"] == "Sarah Intro", "Accept creates the contact and adds Michael, introduced by Sarah")
    check(not any(p["primary"] for p in o["participants"]), "Accept never sets a primary")
    new = next(s for s in a.get("/api/crm/suggestions").get_json()["suggestions"] if s["kind"] == "new_contact") if any(
        s["kind"] == "new_contact" for s in a.get("/api/crm/suggestions").get_json()["suggestions"]) else None
    if new:                                           # Michael is a contact now, so this one can be dismissed
        a.post(f"/api/crm/suggestions/{new['suggestion_id']}/dismiss")
    a.post("/api/crm/relationships/refresh")
    left = a.get("/api/crm/suggestions").get_json()["suggestions"]
    check(not any(s["email"] == "michael@beta.test" for s in left), "accepted and dismissed suggestions never come back")
    check(a.post(f"/api/crm/suggestions/{add['suggestion_id']}/accept").status_code == 404, "a suggestion can't be accepted twice")


try:
    main()
except Exception as exc:
    import traceback
    traceback.print_exc()
    problems.append(f"crashed: {exc}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
for p in problems:
    print("FAILED:", p)
print("RELATIONSHIP CHECK PASSED" if not problems else f"RELATIONSHIP CHECK FAILED: {len(problems)} problem(s)")
sys.exit(0 if not problems else 1)
