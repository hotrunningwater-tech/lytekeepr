# Testing plan: Tier 0 and Tier 1 features

Every feature sorted by how hard it is to test. Status and ratings are in `feature-tiers-status.md`. Written 2026-10-25.

## Level 1: test right now, alone, on your existing data (no setup), 11 features

- Customer history (#1), lookup (#2), search (#3)
- CRM updates through Claude (#4)
- Summaries of existing calls and emails (#6)
- Meeting prep on demand (#7)
- Not contacted recently / Gone quiet (#8, P1)
- Contacts, companies, funnels (#9, #10)
- Capture from a business card photo or voice note (P8)
- The "Follow up?" picker after a send (status picker, calendar buttons, Claude)
- Clearing follow-ups through Claude ("clear Instagram follow-ups older than a week")

## Level 2: a little setup, still just you (your second email or account), 5 features

- Email drafting and sending (#5): send to your own other address.
- Meeting brief (P7): book a meeting with your second email as a guest (it must be a contact), then wait for the next scheduled run.
- After-call summary and follow-up draft (P5): a short Fathom-recorded call with yourself or Sergiy, with some real talking.
- No-show draft: book a meeting, don't join, mark it No-show.
- Onboarding (P2): mark a stage Won, then move a test contact with your own email into it.

## Level 3: complex, needs real traffic over days, 2 features

- Unanswered emails and interested people (#11)
- Commitments and promises (#12)

They need real people writing to you; only days of real mail show whether the flags are good (few and right).

## Not testable yet

- Tier gating (Group 5): not built.
