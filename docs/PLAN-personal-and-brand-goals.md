# Plan: Personal space, and goals at every level

Status: built 2026-11-01 (checks in scripts/check_brands.py, run before every deploy). Agreed: one structure for brands and Personal; long/mid/short goals reachable, not in view.

## Why

LyteKeepr is where the user works on the go, mostly by talking to Claude: building and testing the product, three brands,
and personal things. Tasks, goals and thoughts said to Claude should land in an organized place and come back when asked.
Light: no boards, no Gantt, nothing to set up. Not a project-management tool.

## The structure

| | Funnels | Docs | Goals | Tasks |
|---|---|---|---|---|
| Each brand (Nexy, Aspan, LyteKeepr) | yes | yes (already) | new | yes (already) |
| Personal | no | new | new | yes (built 2026-11-01) |

- **Personal** works like a brand without funnels. Each person has their own; only they see it, the admin included
  (the same rule as personal tasks).
- **Goal levels**, for a brand or Personal:
  - Long term: 1 year or more
  - Mid term: 2 to 4 months
  - Short term: about 1 month
  - Weekly
  - Daily
  Each goal has a tick box and, for long/mid/short, a target date (Claude suggests one from the level; the user can change it).
- **Ticking off records when:** the date and time it was done is kept and shown ("done Nov 3, 4:10 PM"); reopening clears it.
- **Goals can be changed:** wording, level, target date, brand or Personal, done or not.
- **Sub-goals** for a big goal: one level deep (a goal and its steps, no steps of steps). Each sub-goal has its own tick and
  date; the goal shows "2 of 5 steps done". A sub-goal can be at a shorter level (a long-term goal with monthly steps).

## What is where

1. **Home, right column:** the Daily goals card stays as it is (today's goals, 3 visible, more by scrolling). Weekly goals
   stay as the tab on the left.
2. **All goals, only when wanted:** a small "Goals" button at the top of the right column opens a goals view in place of
   the chat (a "Back to chat" button returns). It lists every level, filtered by Personal or a brand. Visible as a button
   while we test; it can be hidden later.
3. **Docs:** a Personal place next to the brands, for the user's own notes and plans, private to them.
4. **Claude:** add, change, tick off and read goals at any level and for any brand or Personal ("mid-term goals for Nexy?",
   "add a long-term personal goal: ..."); write and read Personal docs. Every change is shown first, as with every tool.

## Not in this version

Personal goals on Home, sub-goals of sub-goals, progress counted from tasks, Projects.

## Storage (reuse what exists)

- Goals: the existing goals table. Periods gain `month`, `mid`, `year`; one column for the brand ('' = personal), one for
  the parent goal (sub-goals), and the target date in the existing `period_start`. The tick time is the existing `done_at`.
  Existing daily and weekly goals stay personal to their person, as today.
- Personal docs: the docs table with brand `personal`; the owner is the existing `created_by_id` (no new column).
- Personal tasks: already done.

## Who does what

1. User: reads this plan and says what to change.
2. Claude Code: builds it after the user's go, with tests and a deploy-time check that nobody sees another person's
   Personal goals or docs.
3. Claude Code: deploys after a second go.
4. User: tests (add goals at each level by talking to Claude, open the Goals view, check a team member sees none of it).
