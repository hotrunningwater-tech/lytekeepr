#!/usr/bin/env python3
"""Checks sorting email one decision per sender, on a fresh installation in a temporary folder (never the real data):
waiting emails are grouped by sender (written to, automatic, selling, never written to), "written to" asks the mailbox itself;
Claude's sort is shown first and changes nothing before the yes; kept senders become contacts with all their emails; removed
senders' later emails skip review; a removed sender can be brought back; a team member can't see or sort the admin's mail.
Deploys stop if this fails. Prints EMAIL SORT CHECK PASSED or lists what failed. Run on its own: python3 scripts/check_email_sort.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lk-email-sort-"))
(TMP / "profiles" / "data" / "0001").mkdir(parents=True)
(TMP / "profiles" / "registry.json").write_text(json.dumps({"version": 1, "profiles": {"home": {
    "profile_id": "home", "display_name": "Home", "storage_id": "0001", "created_at": "2026-01-01T00:00:00+00:00",
    "last_active_at": "2026-01-01T00:00:00+00:00", "attached_gmail_account_ids": [], "active_gmail_account_id": None}}}))
(TMP / "profiles" / "active-profile.json").write_text(json.dumps({"profile_id": "home"}))
(TMP / "legacy-google").mkdir()
os.environ.update({"LK_PROFILES_ROOT": str(TMP / "profiles"), "LK_CALENDAR_DB": str(TMP / "no-legacy.db"), "LYTEKEEPR_PASSWORD": "sort-test",
                   "LYTEKEEPR_SECRET_KEY": "sort-test", "LYTEKEEPR_ADMIN_EMAIL": "owner@example.com", "LK_GMAIL_DAILY": "0",
                   "LK_GOOGLE_AUTOSYNC": "0", "LK_FATHOM_AUTOSYNC": "0", "OPENAI_API_KEY": "", "LK_URL_PREFIX": ""})
sys.path.insert(0, str(APP_DIR))
import profiles.store as _profiles  # noqa: E402
if Path(_profiles.PROFILE_ROOT).resolve() != (TMP / "profiles").resolve():
    print("EMAIL SORT CHECK REFUSED TO RUN: the app points at the real data. Run this script on its own.")
    sys.exit(2)
import google_oauth.store as gstore  # noqa: E402
gstore.LEGACY_DIR = TMP / "legacy-google"
import auth_gate  # noqa: E402
auth_gate._notify_signup = lambda email, url: None
import server  # noqa: E402
from crm import db as crm_db  # noqa: E402
from gmail import storage  # noqa: E402
from lk_calendar import claude_connector, superadmin  # noqa: E402
superadmin._alert = lambda *a: None
import logging  # noqa: E402
server.app.logger.setLevel(logging.CRITICAL)

ME = "me@example.com"
app = server.app
problems = []
asked_mailbox = []
SENT_TO = {"old.friend@client.test"}           # the mailbox knows the user wrote to them, years ago (not in what was synced)


def check(ok, what):
    if not ok:
        problems.append(what)


def email(mid, sender, name, subject, body, bulk=False, when="2026-10-20T10:00:00+00:00"):
    m = {"id": mid, "threadId": "t" + mid, "accountId": ME, "historyId": "", "from": {"name": name, "email": sender}, "to": [{"name": "Me", "email": ME}],
         "cc": [], "bcc": [], "subject": subject, "receivedAt": when, "sentAt": when, "snippet": body[:100], "textBody": body, "htmlBody": "",
         "labelIds": ["INBOX"], "isUnread": True, "isStarred": False, "isBulk": bulk, "hasAttachments": False, "attachments": []}
    storage.save_pending(ME, m)
    storage.upsert_scan_entry(ME, mid, {"status": "pending_review", "isRelevant": True, "reason": "", "lastScannedAt": when, "historyId": "",
                                        "senderName": name, "senderEmail": sender, "subject": subject, "receivedAt": when, "snippet": body[:100],
                                        "hasAttachments": False, "unread": True, "labelIds": ["INBOX"]})
    return mid


def main():
    a = app.test_client()
    a.post("/login", data={"password": "sort-test"})
    server.get_active_account_id = lambda: ME
    server.gmail_client.list_message_ids = lambda q: (asked_mailbox.append(q), {"messages": [{"id": "x"}] if q.split("to:")[-1] in SENT_TO else []})[1]
    email("e1", "old.friend@client.test", "Old Friend", "Lunch next week?", "Are you free Tuesday?")
    email("e2", "old.friend@client.test", "Old Friend", "Re: lunch", "Great, see you then")
    email("e3", "noreply@shop.test", "Shop", "Your order", "Thanks for your order")
    email("e4", "news@brand.test", "Brand", "October news", "Lots happening", bulk=True)
    email("e5", "rep@saas.test", "Sales Rep", "Quick question", "Would you be open to book a demo? 15 minutes is all I need.")
    email("e6", "stranger@unknown.test", "Stranger", "Hello", "I saw your website and have a question about working together.")

    groups = {g["group"]: [s["sender"] for s in g["senders"]] for g in a.get("/api/email/senders").get_json()["groups"]}
    check(groups.get("written") == ["old.friend@client.test"], f"someone the user wrote to (found by asking the mailbox) is not in 'written': {groups}")
    check(set(groups.get("automatic", [])) == {"noreply@shop.test", "news@brand.test"}, f"no-reply and mailing-list senders are not 'automatic': {groups}")
    check(groups.get("selling") == ["rep@saas.test"], f"a sales pitch is not in 'selling': {groups}")
    check(groups.get("never") == ["stranger@unknown.test"], f"someone the user never wrote to is not in 'never': {groups}")
    friend = next(s for g in a.get("/api/email/senders").get_json()["groups"] for s in g["senders"] if s["sender"] == "old.friend@client.test")
    check(friend["emails"] == 2 and friend["example"]["subject"], f"a sender's count or example email is missing: {friend}")
    before = len(asked_mailbox)
    a.get("/api/email/senders")
    check(len(asked_mailbox) == before, "the mailbox is asked again about senders it already answered for")

    # a team member never sees or sorts the admin's mail
    m = app.test_client()
    a.post("/api/team", json={"email": "member@example.com", "name": "M"})
    server.login_identity = lambda code: {"email": "member@example.com", "email_verified": True, "name": "M"}
    m.get(f"/auth/google/callback?state={server._oauth_state_signer().dumps('google-login')}&code=x")
    check(m.get("/api/email/senders").status_code == 403, "a team member can see the admin's waiting email")
    check(m.post("/api/email/senders/sort", json={"remove": ["stranger@unknown.test"]}).status_code == 403, "a team member can sort the admin's email")

    # Claude: shown first, nothing changes before the yes
    def call(method, path, body=None):
        r = a.open(path, method=method, json=body)
        return r.status_code, r.get_json()
    def need(status, data):
        if status >= 400:
            raise RuntimeError(f"{status} {data}")
        return data
    inbox = claude_connector._run_tool(call, "email_inbox", {})
    check(len(inbox["groups"]) == 4, f"Claude does not get the sender groups: {inbox}")
    args = {"keep": ["old.friend@client.test", "Stranger@Unknown.test"], "remove": ["noreply@shop.test", "news@brand.test", "rep@saas.test"],
            "names": {"Stranger@Unknown.test": "Dana Whitfield"}}
    shown = claude_connector._run_tool(call, "sort_email_inbox", dict(args))
    text = shown.get("show_the_user") or ""
    check("Old Friend (old.friend@client.test), 2 emails" in text and "Dana Whitfield (Stranger@Unknown.test)" in text and "rep@saas.test" in text
          and "30 days" in text, f"the sort is not shown first as people (name, address, count): {text}")
    check(len(storage.get_pending_review_ids(ME)) == 6, "sorting changed something before the yes")
    done = claude_connector._run_tool(call, "sort_email_inbox", dict(args, confirm_code=shown.get("confirm_code")))
    check(len(done.get("kept", [])) == 2 and len(done.get("removed", [])) == 3, f"the sort did not do what was shown: {done}")
    check(storage.get_pending_review_ids(ME) == [], f"emails are still waiting after sorting: {storage.get_pending_review_ids(ME)}")
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        friend_acct = crm_db.find_channel_account(conn, "email", "old.friend@client.test")
        check(friend_acct is not None and friend_acct["contact_status"] == "trusted", "a kept sender did not become a contact")
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE account_id = ?", (friend_acct["account_id"],)).fetchone()[0] if friend_acct else 0
        check(n == 2, f"a kept sender's emails are not on their timeline ({n})")
        check(crm_db.find_channel_account(conn, "email", "rep@saas.test") is None, "a removed sender became a contact")
        named = conn.execute("""SELECT p.name FROM people p JOIN channel_accounts ca ON ca.person_id = p.person_id
                                WHERE lower(ca.channel_identifier) = 'stranger@unknown.test'""").fetchone()
        check(named and named[0] == "Dana Whitfield", f"a kept sender's contact did not get the name Claude found: {named}")
    finally:
        conn.close()
    removed = a.get("/api/email/senders").get_json()["removed"]
    check(set(removed) == {"noreply@shop.test", "news@brand.test", "rep@saas.test"}, f"the removed senders are not listed for bringing back: {removed}")

    # later emails: a kept sender's go straight on, a removed sender's skip review
    email("e7", "old.friend@client.test", "Old Friend", "One more thing", "Also, the contract")
    email("e8", "rep@saas.test", "Sales Rep", "Following up", "Just bumping this, book a demo?")
    server.scan_for_candidates = lambda since_date=None: {"account_id": ME, "new_candidate_ids": ["e7", "e8"]}
    server.run_gmail_scan(None)
    state = storage.load_scan_state(ME)
    check(state["e7"]["status"] == "saved", f"a kept sender's new email waits for review: {state['e7']['status']}")
    check(state["e8"]["status"] == "dismissed", f"a removed sender's new email waits for review: {state['e8']['status']}")

    # an existing contact is never removed from here, even if an email of theirs waits for review
    conn = crm_db.get_connection()
    try:
        crm_db.get_or_create_person_for_channel(conn, "email", "client@known.test", "Known Client")
        conn.commit()
    finally:
        conn.close()
    email("e9", "client@known.test", "Known Client", "Invoice question", "Can you resend the invoice?")
    kept_safe = a.post("/api/email/senders/sort", json={"remove": ["client@known.test"]}).get_json()
    conn = crm_db.get_connection()
    try:
        check(kept_safe.get("already_contacts") == ["client@known.test"] and not kept_safe.get("removed")
              and not crm_db.is_contact_excluded(conn, "email", "client@known.test"), f"an existing contact could be removed: {kept_safe}")
    finally:
        conn.close()

    # the test switch: off, the mailbox must be the Google account signed in with; on (one named login only), any mailbox
    asked = []
    server.build_authorization_url = lambda state, login_hint=None: (asked.append((server._oauth_state_signer().loads(state), login_hint)), "https://accounts.test/")[1]
    a.get("/auth/google/start")
    check(asked and asked[-1][0]["e"] == "owner@example.com", f"without the test switch the mailbox is not tied to the login: {asked}")
    os.environ["LK_TEST_ANY_MAILBOX"] = "someone-else@example.com"
    a.get("/auth/google/start")
    check(asked[-1][0]["e"] == "owner@example.com", "the test switch opened up a workspace it does not name")
    os.environ["LK_TEST_ANY_MAILBOX"] = "owner@example.com"
    a.get("/auth/google/start")
    check(asked[-1][0]["e"] == "" and asked[-1][1] is None, f"the test switch did not open up the named test login: {asked[-1]}")
    os.environ.pop("LK_TEST_ANY_MAILBOX")

    # bring back within 30 days: the set-aside emails wait for review again, later ones come in again
    back = a.post("/api/email/senders/sort", json={"bring_back": ["rep@saas.test"]}).get_json()
    check(back.get("brought_back") and back["brought_back"][0]["emails"] >= 1, f"a removed sender could not be brought back: {back}")
    check("e5" in storage.get_pending_review_ids(ME), "a brought-back sender's emails are not waiting for review again")
    conn = crm_db.get_connection()
    try:
        check(not crm_db.is_contact_excluded(conn, "email", "rep@saas.test"), "a brought-back sender is still blocked")
    finally:
        conn.close()


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
    print("EMAIL SORT CHECK FAILED")
    sys.exit(1)
print("EMAIL SORT CHECK PASSED")
