# Plan: Claude shows every change before making it

Status: approved 2026-10-29.

## Why

On 2026-10-28 an email meant for Katie went to Vicky. The fix for sending (2026-10-29): LyteKeepr itself shows the email exactly as it
will go out, and sends only with the code that came with that exact version. Every other tool that adds, changes or deletes something
still relies on Claude asking first, so the same kind of mistake can happen with a wrong contact, a wrong date or a delete.

## The rule (built into LyteKeepr, so it holds for every user whatever their Claude permission settings)

Claude's 92 tools fall into three groups (each tool is already marked, and Claude's own settings use these marks):

1. **Read** (31): search, look up, list. No change, nothing to confirm.
2. **Write** (48): add, update, move.
3. **Destructive** (13): send, delete, cancel, remove.

Every write and destructive tool works in two calls:

1. **First call:** nothing happens. LyteKeepr answers with exactly what would happen, in plain words, with real names, not ids:
   which contact (name and email), which meeting or task (title and date), what changes (old value → new value), the full text being
   saved, who gets emailed (invites, agenda changes), and how many items for a bulk change (with who).
2. Claude shows that word for word and asks "Do it? yes / no".
3. **Second call, after the yes:** with the code from the first call. The code matches only that exact change, on the data as it was
   shown. If anything differs (other details, or the data changed in the meantime), nothing happens and the new version comes back to show.

Adds (a new task, note, draft, document) go through the same two calls: one rule for everything, nothing to remember per tool.

## Exceptions

- **Sending email** (send_email, reply_email, send_draft): already work this way, unchanged.
- **The scheduled run** has nobody to ask. Its own tools already need the run key. The two it shares with chats (save_meeting_prep,
  save_call_summary) skip the showing when called with a valid run key, and only then.
- A tool that refuses for a missing detail (a contact with no way to reach them, a funnel with a name already used) says so on the
  first call, before anything is shown.

## What goes away

The older, weaker confirmations that this replaces: `user_confirmed` (create contact, create funnel), `user_approved_text` (notes,
documents, meeting notes: the cleaned-up text is now in what is shown), and `preview` on remove_follow_ups. Claude still cleans up
dictated text before the first call.

## Checks

- Tests: every write tool refuses without the code; a code for different details is refused; reads never ask; the scheduled run's
  two shared tools work with a run key.
- After deploy: start a new Claude chat; ask for a task, a note, a stage move and a delete, and see each one shown first.
