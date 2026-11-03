# Funnel templates and automatic stage moves

Agreed 2026-11-03 (Peter, with GPT's analysis checked by Claude). Built 2026-11-03: lk_calendar/funnel_rules.py, check: scripts/check_funnels.py. The list of funnel templates goes in
`docs/funnel-templates.md`.

## Why

A new USER tells Claude about their business in their own words; Claude picks the best-fitting standard funnel and creates it.
Stage names can be anything the business uses. LyteKeepr must still move contacts automatically where that is reliable.

Today the automatic moves go by stage NAME, with one rule set for all funnels ("email sent" → the stage named Contacted, etc.,
lk_calendar/store.py DEFAULT_STAGES / status_stages). That breaks as soon as a funnel uses other names, and renaming a stage in one
funnel changes the shared rules for every funnel. This replaces that.

## Decisions

1. **One engine, stage roles.** Every stage has a free name plus an optional internal role. The engine moves a contact to the
   stage carrying the role an event proves, never by name. Roles: `new`, `contacted`, `conversation_started`, `meeting_booked`,
   `meeting_held`, `appointment_booked`, `proposal_sent`, `contract_signed`, `won`, `lost` (won already exists as `is_won`).
   A funnel without a stage for a role: that move doesn't happen there.
2. **Four automation patterns**, chosen per template (not 47 rule sets):

   | Pattern | What drives progress |
   |---|---|
   | P1 Outbound Sales | contact, reply, meeting, conversation |
   | P2 Consultation Sales | a specific sales call, consultation or demo |
   | P3 Appointment Bookings | an actual service reservation |
   | P4 Milestone Sales | quotes, contracts, approvals, payments (moved by USER or by Claude from the messages, shown first) |

   | Signal | P1 | P2 | P3 | P4 |
   |---|---|---|---|---|
   | First message sent | → contacted | log | log | log |
   | They replied | → conversation_started | log | log | log |
   | Meeting booked | → the funnel's Booked stage, if it has one (every pattern) | same | same | same |
   | Call recorded | log | log | log | log |
   | Bounce | address unreachable | same | same | same |
   | Clearly not interested | → lost | same | same | same |
   | No reply after X days | follow-up due | same | same | same |

3. **Safeguards**
   - No reply never moves a stage; it makes a follow-up (as LyteKeepr already does).
   - A bounce marks the email address unreachable; it does NOT make the deal Lost (another channel may work). Today a bounce moves
     to Lost: this changes.
   - Only an explicit, unambiguous no is Lost; "not now" / "next month" is a follow-up.
   - A calendar booking moves a contact only to the funnel's FIRST Booked stage, if they haven't reached it (so one meeting never
     counts twice; #9's second one, Demo Booked, is moved by USER or Claude), never to a completed, approved, signed or won stage. Only an
     explicit confirmation closes a booking-based deal (#23, #31, #33...).
   - A calendar invite is not an accepted or held meeting; use the attendee's response and the meeting date.
   - A recorded call proves only that a call happened (and that the contact was on it), not agreement.
   - Forward only by default; won and lost are final unless USER reopens.
   - Events belong to the right deal: a person on an opportunity moves the opportunity, not every deal they're on.
4. **Claude moves milestones from evidence** (proposal sent, quote accepted, contract signed, payment received), always shown to
   USER first. Most templates are P4, so this is where most progress comes from.
5. **Outbound prospecting is a sales method, not an industry.** If USER says they find clients by reaching out (cold email,
   LinkedIn), Claude picks the outbound funnel even if they also call themselves a consultant, agency or freelancer.
6. **Existing funnels keep working exactly as today**: they keep the older rules by stage name (pattern ''), untouched; only funnels
   made from a template use patterns and marks. The deploy checks prove this.

## Later signals worth adding (detectable from email, calendar, calls)

First inbound inquiry (→ new lead), meeting accepted/cancelled, proposal or quote sent, explicit acceptance, contract signed,
payment received. The three most valuable: proposal sent, explicit acceptance, contract signed.

## Not taken from the analysis

- LinkedIn access limits: LyteKeepr reads LinkedIn through USER's own add-on, so they don't apply.
