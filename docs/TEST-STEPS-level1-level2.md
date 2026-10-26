# Test steps: Level 1 and Level 2

Each step: what to do, then what proves it works. "Claude" means the Claude app (phone or web) with the LyteKeepr connector on.
**Where to test:** (Phone) = all of it in the Claude app on your phone. (Phone + Mac) = the Claude part on the phone, plus one check on a desktop screen (or ask Claude to confirm instead). (Mac only) = the result shows only on desktop Home.
**No waiting for the schedule:** wherever a step says "after the next autopilot run", you can paste the autopilot prompt (Settings → Claude, or the clock circle on the phone) into a Claude chat to run it right away.

---

## Level 1: alone, on your existing data

**1. (Phone) Customer history (#1)**
Ask Claude: *"Show me everything that happened with Minnie."*
✅ Works if: her emails, calls and notes come back in date order, with dates.

**2. (Phone) Lookup (#2)**
Ask Claude: *"Where did we leave off with Julius?"*
✅ Works if: you get his last messages, his funnel stage, and his next follow-up.

**3. (Phone) Search (#3)**
Ask Claude: *"Find emails that mention pilot."*
✅ Works if: matching emails come back, including ones from people who are not contacts yet (marked as not saved).

**4. (Phone) CRM updates through Claude (#4)**
a. Ask Claude: *"Add a note to Ernie: prefers mornings."*
✅ Works if: Claude first shows the cleaned-up note and asks; after your "yes", the note is on Ernie's page in the CRM.
b. Ask Claude: *"Move Ernie to In conversation."*
✅ Works if: his card is in that stage on the funnel board.

**5. (Phone) Summaries (#6)**
Ask Claude: *"Summarize my call 'Nexy Pilot — Intro Call with Minnie'."*
✅ Works if: you get what was discussed, the decisions and the next steps, and Claude says it saved it on the call (it shows on Minnie's contact page).
Ask again: ✅ Claude warns there is already a summary and replaces it only if you say yes.

**6. (Phone) Meeting prep on demand (#7)**
Ask Claude: *"Prepare me for my next meeting."*
✅ Works if: you get who is coming, their history with you, and what to aim for, and it is saved in that meeting's **My private notes** as "Prep by Claude" (your own notes there untouched).
Ask again: ✅ Claude says it is already prepped (and when) and redoes it only if you insist.
Ask *"Prepare me for a meeting with someone I'm not meeting"*: ✅ Claude says there is no such meeting and prepares nothing.

**7. (Phone + Mac) Who is waiting / gone quiet (#8, P1)**
a. Home → Automatic → **Gone quiet** (it only appears when someone qualifies). It has two sections: **Waiting on you** (they wrote last) and **Waiting on them** (you wrote last).
b. Ask Claude: *"Who am I leaving hanging?"* and *"Who hasn't answered me?"*
✅ Works if: Claude asks which brand (or all), then names the same people as the matching section, each labelled with their side.
c. Click **Not now** on one.
✅ Works if: they disappear from the list.

**8. (Phone + Mac) Contacts and companies (#9, #10)**
a. CRM → Contacts → Companies → **Open company** on one you know.
✅ Works if: you see its people, opportunities and recent history.
b. Ask Claude: *"Show me the company [name]."* → same people and opportunities.
c. Ask Claude: *"Add a note to [company]: prefers annual billing."* → it asks first; say yes.
✅ Works if: the note shows when you Open company again.

**9. (Phone) Capture (P8)**
a. In Claude, send a photo of a business card: *"Add this person to LyteKeepr."*
✅ Works if: Claude lists name, title, company, email and phone, asks before saving, and the contact is then found in the CRM search.
b. Say: *"I just met Anna Lee from Newco, anna@newco.com, she wants a demo next week. Add her."*
✅ Works if: Anna is a contact with the company, the email, and a note about the demo.

**10. (Phone + Mac) "Follow up?" after a send**
Statuses are changed on the funnel board, so first put Anna in a funnel (tell Claude: *"Move Anna to the [funnel] funnel."*).
a. CRM → Funnels → that funnel → on Anna's card, click her **Email** status line → pick **email sent** → press Save without choosing anything.
✅ Works if: it stops you with "Follow up? Choose…".
b. Choose **In 2 days** → Save.
✅ Works if: on Anna's contact page the Email line says "follow-up" with the date 2 days from today.
c. Tell Claude: *"I sent Anna another email."*
✅ Works if: Claude asks "follow up in 3 days, other days, or no?". Answer **no**, and no new follow-up appears.

**11. (Phone) Clearing follow-ups**
Ask Claude: *"How many Instagram follow-ups are older than a week? Clear them."*
✅ Works if: Claude tells you the number first and clears only after your yes; the Follow-ups count on Home drops by that number.

---

## Level 2: a little setup, still just you

**Setup once:** add your second email address as a contact named **Test Me**.

**12. (Phone) Email drafting and sending (#5)**
Ask Claude: *"Write a short hello email to Test Me and send it."*
✅ Works if: Claude shows the text and asks before sending, the email arrives in your second inbox, and it is in Test Me's history.

**13. (Phone + Mac) Meeting brief before a meeting (P7)**
Calendar → **+ New meeting** for tomorrow, invite Test Me. Then wait for the next autopilot run.
✅ Works if: on Home under Meetings the meeting has a **Prep** button, and the meeting's **My private notes** start with "Prep by Claude".

**14. (Mac only) After-call summary and draft (P5)**
Have a 3–5 minute Google Meet call recorded by Fathom with your second account or Sergiy, and talk about real next steps. When it ends, wait up to an hour for the recording and the next autopilot run.
✅ Works if: Home → Calls to review shows the call as "summary ready", **Summary** opens the summary, and **Review draft** opens a follow-up email to the guest in the send card. Nothing is sent unless you press Send now.

**15. (Mac only) No-show draft**
Book a 15-minute meeting with Test Me starting in 10 minutes, with Google Meet and Fathom on, and don't join.
✅ Works if: 2 hours after its end, Home → Calls to review asks "No recording… was it a no-show?". After you click **No-show** and the next autopilot run, a card "No-show — reschedule email ready" appears with **Review draft**.

**16. (Phone + Mac) Onboarding after a won deal (P2)**
CRM → Funnels → your funnel → **Edit stages** → **Won?** on your won stage (e.g. Completed). Then move Test Me into that stage.
✅ Works if: after the next autopilot run, Home → Calls to review shows "Deal won — welcome email ready", and Home → Tasks shows the onboarding reminders for Test Me.
