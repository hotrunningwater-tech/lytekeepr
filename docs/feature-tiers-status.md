# Claude + CRM features: Tier 0 (free) and Tier 1 (paid)

The list came from people's comments about Claude + CRM features (our saved Reddit research). Tier 0 is free and Tier 1 is paid ($10–20/month). Both are must-haves for launch, and they come first: no Tier 2 or 3 work until Tier 0 and Tier 1 are excellent and reliable.
Ratings show how close each feature is to fully built (10 = fully built), checked against the code. Last updated 2026-10-24.

## How features run

- **Client-requested:** the customer asks Claude, and Claude uses LyteKeepr's tools. No setup.
- **Scheduled:** plug-and-play. LyteKeepr defines the whole workflow (what to check, what to do, every case). The customer pastes **one** ready-made prompt into a scheduled task in the Claude app once (Settings → Claude on desktop; the clock circle at the top left on the phone), and it runs every 2 hours.
  - The prompt never lists features. It only says "call autopilot_queue and follow its instructions". The queue carries each item's instructions, so new scheduled features start working without anyone pasting again.
  - LyteKeepr decides on the server what each customer's queue holds (by tier, once gating exists).
  - The AI work uses the customer's Claude subscription, not LyteKeepr's paid AI.
- **Event-driven:** none. Scheduled runs detect events (a deal won, a meeting ended) and act on them.
- **Plain code:** a scheduled feature whose rules are clear can run in LyteKeepr itself with no AI and no setup (P1 Gone quiet). This is cheaper and more reliable than a Claude run.

## Tier 0 — Free

| # | Feature | Type | Status | Rating |
|---|---|---|---|---|
| 1 | Unified customer history | Client-requested | LinkedIn messages, emails, calls, notes and logged conversations are all in it. Team members only see messages if given access | 9 |
| 2 | Customer information lookup | Client-requested | search_contacts, get_contact, full history | 9 |
| 3 | Search across communications | Client-requested | Messages, notes and docs are searchable. Email search works by a contact's address, not free text across the whole Gmail | 8 |
| 4 | CRM updates through Claude | Client-requested | Update contact, notes, stage moves, opportunities, tasks, follow-up dates | 9 |
| 5 | Email and follow-up drafting | Client-requested | LinkedIn drafts, email send/reply (the text is approved first) | 9 |
| 6 | Conversation and transcript summaries | Client-requested | Calls, email threads, meeting notes | 9 |
| 7 | Meeting preparation | Client-requested | Agenda with guests, contact history, private notes, agenda into the Google event | 9 |
| 8 | Customer and deal analysis | Client-requested | Claude analyzes on request. "Not contacted recently" is built: the contacts_gone_quiet tool (by days and funnel) | 8 |
| 9 | Contact creation and organization | Client-requested | Contacts, company as a field, brands, funnels. Missing: a company record or page | 8 |
| 10 | Basic CRM management | Client-requested | Contacts, funnels, notes, tasks, reminders. Companies have the same gap as #9 | 9 |
| 11 | Unanswered emails and potential customers | Scheduled (Claude) | Today plain code plus our AI: a reply from an outreach lead switches the lead to "replied" and creates a "Reply to …" task, and promises.py uses OpenAI to spot promised dates. Not built: Claude reviewing new incoming mail on the schedule, telling real interest from spam, and a small list of flags | 4 |
| 12 | Business commitments and promises | Scheduled (Claude) | Today only promised dates for leads (promises.py, our AI). Not built: Claude finding clear promises, deadlines and deliverables in emails and transcripts on the schedule, without duplicates | 3 |

## Tier 1 — Paid

| # | Feature | Need | Type | Status | Rating |
|---|---|---|---|---|---|
| P1 | Neglected lead detection | 9/10 | Plain code (always on) | Gone quiet on Home: funnel contacts with no contact for 14/30/60/90 days, with Not now; Claude tools contacts_gone_quiet and snooze_quiet_contact. Lead follow-up tasks are automatic too | 9 |
| P2 | New-customer onboarding | 7/10 | Scheduled (Claude) | Not built. Funnels have no "won / closed" stage yet, which it needs first | 1 |
| P5 | Post-meeting processing | 8/10 | Scheduled (Claude) | Fathom pulls the recording. The scheduled run writes the call summary (shown on the call and in the contact's history) and a follow-up draft, plus a reschedule draft after a no-show. Drafts wait on Home and are never sent on their own | 9 |
| P7 | Automatic meeting preparation | 8/10 | Scheduled (Claude) | The scheduled run writes a brief for each meeting with a contact in the next 26 hours, shown on Home and on the meeting | 9 |
| P8 | Customer information capture | 7/10 | Client-requested | The Claude app takes the voice note or business-card photo itself and calls create_contact / add_contact_note, which exist. Not tested end to end yet | 6 |

## Not built yet, needed for both tiers

- **Tier gating:** nothing decides yet which features a customer gets (free or paid). The queue and the tools will check the customer's tier on the server.
- **Our paid AI in places Claude could do the work:** promises.py (reply and promised-date detection), the Summarize and Draft follow-up buttons, the contact assistant, and the Ask LyteKeepr chat panel use OpenAI. The plan: move scheduled work to the Claude run (groups 3 and 4). Whether the buttons and the chat panel stay is a product decision still open.

## Build groups (features built or finished together)

1. **Done 2026-10-24: Gone quiet (P1 + #8).** A "last contact" date per contact from every source. Home chip, Not now, and Claude tools.
2. **Done 2026-10-24: Meetings on autopilot (P7 + P5).** The scheduled queue, briefs, call summaries, follow-up and no-show drafts, and the single setup prompt (Settings, plus the phone's clock circle).
3. **New messages on autopilot (#11 + #12).** Both read the same feed: new incoming emails and messages (and new transcripts) since the last run. The queue hands Claude the new items; Claude saves only clear flags (interested, waiting for an answer, a commitment with its deadline) through save tools; Home shows a short list with Dismiss. This replaces our AI in promises.py. Precision first: no newsletters, spam or casual remarks, and no duplicates.
4. **Won deals and onboarding (P2).** A "won" mark on funnel stages, then the scheduled run finds newly won contacts and saves a welcome email draft and onboarding reminders.
5. **Tier gating.** Free vs paid per workspace; the queue and the tools respect it.
6. **Capture test (P8).** Test in the Claude app: a voice note and a business-card photo become a contact and a note. Fix anything the test shows.
7. **Company records (#9, #10).** A company record and page with its people, opportunities and history, plus Claude tools.
8. **Free-text email search (#3).** Search all synced Gmail by words, not only by a contact's address.

Update a row whenever a feature is built.
