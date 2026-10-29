# Plan: fewer, clearer Claude tools

Status: built (2026-10-28); 101 → 92 tools. Written 2026-10-28.

## Why

Claude has **101 tools**. Some do almost the same job, and Claude sometimes picks the wrong one. On 2026-10-28 Claude saved 2 drafts with `save_email_draft`, then looked for them with `list_unsent_drafts` (only drafts **2+ days old**), got nothing back and said there was nothing. The right tool, `list_drafts`, would have shown both.

Three parts:
1. **One tool per job:** merge look-alikes (below, group by group).
2. **Scheduled-run tools out of normal chat** (Part 2).
3. **An empty answer says what it covered:** for example *"no drafts older than 2 days"*, never just "nothing". This applies to every merged tool.

Each group is built, tested and deployed **one at a time**, after you approve that group.

---

## Part 1: merge look-alikes (checked against the code, not the names)

### A. Email drafts: 11 tools → 6

What the code actually does today:
- There are **two separate stores**. A draft to **one person, not a reply** goes into one store (`autopilot_drafts`, kind "manual"). A draft to **several people, or any reply (even to one person)** goes into the other (`funnel_drafts`). That's why Katie's and Vicky's one-person replies are "several-people drafts".
- `get_email_draft` only reads the first store; `preview_group_draft` only the second. Claude has to know which store a draft is in.
- `list_unsent_drafts` = drafts older than 2 days (the scheduled run already gets these in its queue).
- `group_send_status` = progress of a batch being sent.

| Today | After |
|---|---|
| `save_email_draft` | **`save_email_draft`** (unchanged) |
| `list_drafts`, `list_unsent_drafts` | **`list_drafts`**: every unsent draft (one person, several people, and the scheduled run's follow-up/welcome drafts), each with how long it has been waiting |
| `get_email_draft`, `preview_group_draft`, `group_send_status` | **`open_draft`**: any draft, from either store; shows the text, who it goes to, who is skipped, and sending progress if it's being sent |
| `update_group_draft` | **`update_draft`**: change the text or subject of any draft, and the people of a several-people draft |
| `discard_email_draft`, `delete_group_draft` | **`discard_draft`**: by draft, or by contact (takes them out of every draft) |
| `send_group_draft` | **`send_draft`**: sends a saved draft (asks about the follow-up once) |

Stays separate (different job): `send_email` (sends a new email that isn't saved as a draft), `reply_email`.

**Decided:** `send_draft` sends any saved draft, one person or several, after showing it and getting a yes.

### B. Reading messages and emails: 6 tools → 4

What the code actually does:
- `check_reply`: did they write back, on LinkedIn, Instagram or email. **For email it gives only the date, not the text.**
- `email_latest`: last email from them and from you, **full text**, and whether they replied. It overlaps `check_reply` for email.
- `search_messages`: searches **LinkedIn and Instagram only** (email removed).
- `email_lookup`: searches **emails only**.
- `email_thread`: one whole email conversation.
- `get_contact_history`: everything with a contact over time; **emails show the subject only**.

There's an inconsistency: three tools hide email text ("email text stays in LyteKeepr"), while `email_latest` and `email_thread` give the full text. The hiding is left over from before emails were shared with Claude.

| Today | After |
|---|---|
| `check_reply`, `email_latest` | **`last_exchange`**: their last message and yours, on any channel, with the text, and whether they replied |
| `search_messages`, `email_lookup` | **`search_messages`**: one search across email, LinkedIn and Instagram (contact, words, channel, since) |
| `email_thread` | **`email_thread`** (unchanged) |
| `get_contact_history` | **`get_contact_history`** (unchanged; each email gets its id so Claude can open it with `email_thread`) |

**Decided:** email text is shown everywhere (Claude already reads it through `email_latest` and `email_thread`).

### C. Tasks, reminders, follow-ups: remove 1, decide 1

What the code actually does (they are **not** the same thing):
- `create_task`: a task in the LyteKeepr calendar. It shows on Home under Tasks, or under Actions when due. It has a brand.
- `create_reminder`: a row in a **different** list that shows inside Home → **LinkedIn follow-ups** ("No specific contact (custom)"). No brand, no contact. This is where Claude's "Anna demo" reminder would have gone. Hard to find.
- `add_opportunity_task`: a task linked to an opportunity.
- `set_follow_up_date`: moves an outreach follow-up (tied to a contact and channel).
- `mark_task_done`, `update_calendar_item`, `move_overdue`: act on tasks.

| Today | After |
|---|---|
| `create_task`, `create_reminder` | **`create_task`**: "remind me" becomes a task on Home. Claude stops using the LinkedIn follow-ups list (the app's own form there stays). |
| the rest | unchanged |

**Decided:** a task can be linked to a contact, so "remind me about Anna's demo" also shows on Anna's page.

### D. Meetings: remove 1, fix 1

What the code actually does (all different, except one):
- `save_meeting_prep`: Claude's prep, inside the meeting's private notes.
- `save_meeting_brief`: **the same as `save_meeting_prep`**, under an old name.
- `write_meeting_notes`: **your** private notes (asks before saving).
- `set_meeting_agenda`: the Google description that **guests see**.
- `set_meeting_status`: scheduled, completed, no-show, cancelled. It also takes `notes`, which writes into your private notes **without asking first**, by-passing the rule `write_meeting_notes` follows.

| Today | After |
|---|---|
| `save_meeting_brief` | removed (after checking your Claude scheduled task doesn't name it; today's prompt only says "call autopilot_queue") |
| `set_meeting_status` `notes` | removed; notes go through `write_meeting_notes` (which asks first) |

### E. Finding people: remove 1

- `find`: contacts, their opportunities, opportunities by name/company, funnels. "START HERE".
- `search_contacts`: contacts only, with a brand filter, or the newest contacts.
- `find_opportunities`: opportunities by name, company or participant. Everything it finds, `find` also finds.
- `get_company`: one company.

| Today | After |
|---|---|
| `find_opportunities` | removed (`find` covers it) |
| `find`, `search_contacts`, `get_company` | unchanged (different jobs) |

### F. Notes: remove 1 duplicate path

- `update_contact` has an `add_note` field that does exactly what `add_contact_note` does.

| Today | After |
|---|---|
| `update_contact` `add_note` | removed; notes only through `add_contact_note` |

### Checked and left as they are

- **Documents** (14 tools): each does one clear thing (list, search, read, create, write, rename/move, delete, for documents, tabs and folders).
- **Opportunities** (8): add, change or remove participants, lead, stage, task. Distinct.
- **Goals** (4), **suggestions and connections** (5), **brands and funnels** (5), **calls** (`calls_to_review`, `get_call`, `save_call_summary`, `link_call_to_contact`).
- `move_contact_stage` vs `set_outreach_status`: both can move a funnel card, but they answer different requests ("move her to In conversation" vs "I emailed her"). Kept.

**Result:** 101 → 91 tools, and no two tools that answer the same question.

---

## Part 2: keep the scheduled-run tools out of normal chat

### The constraint

The scheduled run and your chats use the **same connector**. LyteKeepr can't tell which one is calling, so it can't simply hide tools from the chat.

### Which tools are scheduled-run only

| Tool | Used by the run | In chat |
|---|---|---|
| `autopilot_queue` | yes, the start of every run | never |
| `save_message_review` | yes (new messages) | never |
| `save_onboarding` | yes (newly won deals) | never |
| `save_followup_draft` | yes (after a call or no-show) | never (in chat Claude uses `save_email_draft`) |
| `list_unsent_drafts` | no: the queue already lists them | removed in Part 1 A |
| `save_meeting_brief` | only very old run prompts | removed in Part 1 D |

Used by **both** (stay normal): `save_meeting_prep`, `save_call_summary`, `get_call`, `get_contact`, `get_contact_history`, `get_meeting_notes`.

### The plan

1. **Run key:** `autopilot_queue` hands out a short-lived key with each run (valid ~1 hour). `save_message_review`, `save_onboarding` and `save_followup_draft` refuse to work without it, and say: *"This is for the scheduled run only. In a chat, use save_email_draft."* So even if Claude reaches for one in a chat, nothing happens and it's pointed to the right tool.
2. **Clear names and labels:** their titles start with "Scheduled run:", and their descriptions start with "Scheduled run only, never in a chat."
3. **Nothing changes for you:** the scheduled task prompt stays "call autopilot_queue and follow its instructions"; the queue tells the run to pass the key.

Considered and not chosen: a second, separate connector for the run. It hides the tools completely, but you'd have to add and sign in to a second connector in Claude.

---

## Part 3: the contact page

1. **"Coming up" box near the top** of a contact's page: their upcoming meetings (they're invited), tasks linked to them ("send Anna the demo"), and their outreach follow-up. Only today and later: a meeting whose time has passed never shows there, even if nobody marked it done.
2. **"Back to People" button:** moved to the left, next to the name, and made easier to see (darker, filled), keeping its shape.
3. Claude's `create_task` takes a `contact_id`, so "remind me to send Anna the demo" lands on Home and in Anna's "Coming up".

---

## Order

1. Part 2 (small, no tool removed except two dead ones).
2. A: drafts (the one that went wrong).
3. B: messages and emails (after your answer on email text).
4. C–F: the small removals.

Each step: build → tests (including a check that every tool the scheduled run names still exists) → you approve → deploy → you start a new Claude chat to pick up the tools.
