# Personal tasks and reminders

Status: built 2026-11-01 (checks in scripts/check_brands.py, run before every deploy; test in tests/test_connector_tools.py).

## What it is

A task or reminder that isn't work ("call mom", "dentist Friday"). No brand, no contact, no funnel. It lives on Home with the
other tasks (the calendar shows meetings only), marked "Personal".

## Who sees it

Only the person who made it, whatever their access: a team member never sees the admin's, the admin never sees a member's,
and "Everything" doesn't change that. Nobody else can open, change, tick off or delete it.

## How

- Stored as an ordinary task with brand (`project`) = `personal`, a reserved key like `google` (no brand can take it). Who
  made it is the existing `owner_id` (no owner = the admin's). No new columns.
- The rule is in the task store (`store.is_mine`): every list (Home, agenda, in-app chat, Claude, the scheduled run,
  "move overdue") and every change (edit, delete, done) goes through it.
- A meeting can't be personal, and a personal task can't be linked to a contact or deal.
- Home: a "Personal" tick box next to "Add task", and a filter All tasks / Work / Personal.
- Claude: create_task has `personal: true` ("remind me to call mom Friday"), shown first like every change.

## Not in this version

Alerts at the due time (phone or email), personal contacts, personal meetings on the calendar.
