# Claude + CRM features: Tier 0 (free) and Tier 1 (paid)

The list came from people's comments about Claude + CRM features. Tier 0 is free and Tier 1 is paid; both are must-haves for critical mass.
Each feature was checked against the code on 2026-10-24. The rating shows how close it is to fully built: 10 = fully built.

| # | Feature | Status | Rating |
|---|---|---|---|
| 4 | CRM updates through Claude | Built: update contact, notes, stage moves, opportunities, tasks, follow-up dates | 9 |
| 2 | Customer lookup | Built: search_contacts, get_contact, full history | 9 |
| 5 | Email drafting on demand | Built: LinkedIn drafts, email send/reply (you approve the text first), "Draft follow-up" after a call | 9 |
| 7 | Meeting prep on demand | Built: agenda with guests, contact history, private notes, writing the agenda into the Google event | 9 |
| 6 | Summaries on demand | Built: call transcripts and summaries, email threads, meeting notes | 9 |
| 1 | Unified customer history | LinkedIn messages, emails, calls, notes and logged conversations are all in it. Team members only see messages if you give them access | 9 |
| 10 | Basic CRM management | Built | 9 |
| 9 | Contact creation and organization | Company is a field on contacts, and opportunities have a main company. Brands and funnels are built. Missing: a separate company page or record | 8 |
| 3 | Search across communications | Messages, notes and docs are searchable. Email search works by a contact's address, not free text across the whole Gmail | 8 |
| 11 | Unanswered emails (free) | Gmail syncs every 15 minutes. A reply from a lead switches the lead to "replied" and creates a "Reply to …" task. This covers leads only, not the whole inbox | 7 |
| P1 | Neglected lead detection | Built: automatic follow-up tasks for leads, plus "Gone quiet" on Home (funnel contacts with no contact for 14/30/60/90 days, with Not now). Added 2026-10-24 | 9 |
| P5 | Post-meeting processing | Fathom pulls the recording within minutes after a call and links it to the contacts. Home shows "Calls to review" with Summarize and Draft follow-up. The summary only runs when you click | 6 |
| 8 | Customer and deal analysis | Claude can analyze on request. "Not contacted recently" is built: the contacts_gone_quiet tool and the Gone quiet list (by days and funnel). Added 2026-10-24 | 8 |
| 12 | Commitments and promises (free) | Partly built: promises.py spots a promised time in a lead's message ("next week", "Monday works") and moves the follow-up date. Other kinds of commitment ("I'll send the deck") aren't tracked | 4 |
| P7 | Automatic meeting prep | Not built. The parts exist, but nothing runs before a meeting | 3 |
| P8 | Voice notes and business cards | Not built. Closest thing: the contact assistant turns pasted notes into a proposed contact update | 2 |
| P2 | New-customer onboarding | Not built. There is no trigger when a deal is won | 1 |

## Build groups (features to build or finish together)

Only the features rated below 9 are grouped. Each group shares the same underlying piece of work.

1. **(Done 2026-10-24) "Gone quiet" detection: P1 Neglected leads + #8 Not contacted recently.** Both need a "last contact" date per contact, from emails, LinkedIn, calls and logged conversations. With that in place, P1 is a Home row ("Funnel contacts not touched in 14 days") and #8 is a filter plus a tool for Claude.
2. **Meetings on autopilot: P7 Automatic meeting prep + P5 Post-meeting processing.** Both run on the meeting timeline, and the Fathom timer that checks for recordings after calls is already there. Before a meeting, a brief is generated from history and notes. After it, the summary and the follow-up draft are ready without a click.
3. **Gmail beyond your contacts: #11 Unanswered emails for the whole inbox + #3 Free-text email search.** Both need Gmail access beyond contact addresses, and both need the same filter to keep newsletters and automated mail out.
4. **Commitments: #12.** Extends promises.py from "a promised time" to "a promised action" ("I'll send the deck"), which becomes a task. Do it after group 2, so action items from call transcripts go through the same step.
5. **Company records: #9.** A company page that lists its people, opportunities and history.
6. **Capture on the go: P8 Voice notes and business cards.** Upload on the phone, turn it into text, then show it as a proposed contact update through the existing contact assistant.
7. **Onboarding: P2.** When a deal is won, a set of tasks or emails starts.

Update a row whenever a feature is built.
