# Plan: connect any mailbox (Gmail, your own mail server, Outlook)

Status: plan only, nothing built. Written 2026-10-27.

## Goal

Each workspace connects one mailbox: **Gmail** (as today), **its own mail server** (IMAP to read, SMTP to send, e.g. through Brevo), or later **Outlook / Microsoft 365**. Everything else in LyteKeepr keeps working the same, whichever mailbox it is.

## Why this is cleaner than it sounds

Only two things talk to Gmail today:
1. **Syncing:** every 15 minutes, new emails are fetched and turned into LyteKeepr's own saved copy (`gmail/sync.py`, `gmail/client.py`).
2. **Sending:** `assistant/email_send_flow.py` sends through `gmail_client.send_message`.

Everything else reads LyteKeepr's saved copy (`gmail/storage.py`): contacts' emails, search, Who is waiting, the scheduled run (new messages, suggestions), drafts, funnel emails, the phone's Email tab. If the new mailbox types fill the **same saved copy in the same format**, all of that works unchanged.

## The pieces

### 1. One mail layer, three ways in
A small interface that each mailbox type implements:
- `fetch_new(cursor)` → new emails, already in LyteKeepr's format (sender, recipients, subject, date, text, thread, folder), plus the next cursor.
- `send(email, reply_to=None)` → sends, and returns the id and thread it was given.
- `check()` → "can I log in and send?" for the Settings test button.

Gmail's existing code becomes the first implementation; nothing about how Gmail works changes.

### 2. Your own server (IMAP + SMTP)
- **Reading:** IMAP, the Inbox and Sent folders. First run: the last 90 days, like Gmail. Then every 15 minutes it reads only what's new, using the server's message numbers (UIDs) as the bookmark.
- **Threads:** worked out from the standard email headers (Message-ID, In-Reply-To, References), so a reply lands in the right conversation. Gmail gives this for free; here we compute it.
- **Noise filter:** Gmail's Promotions/Social sorting doesn't exist on a plain server. Mailing-list, newsletter and automated mail is recognized by its headers (List-Unsubscribe, Precedence: bulk, Auto-Submitted) and kept out, the same way Promotions is kept out now.
- **Sending:** SMTP straight to Brevo with your Brevo login. A copy is then saved in your Sent folder over IMAP, so your mail app shows it too. Replies get the right headers so they thread for the other person.
- **Your volume** (about 20 a day) is far under Brevo's limit. Funnel emails show the limit before a batch anyway.

### 3. Settings → Email
- Choose **Gmail** (sign in with Google, as now) or **Own mail server**: IMAP server, port, username, password; SMTP server (Brevo), port, username, password; your From name and address.
- **Test connection:** logs in to both and sends a test email to yourself.
- Passwords are stored encrypted on the server (not readable in the database), and only for that workspace.

### 4. Outlook / Microsoft 365 (second phase)
Microsoft has switched off plain password login for IMAP, so Outlook needs Microsoft's own sign-in (an app registration with Microsoft, like the Google one), then reading and sending through Microsoft Graph. It plugs into the same mail layer; nothing above it changes.

## What stays exactly the same
Contacts' email history, search, Who is waiting, From messages (the scheduled run), drafts to one contact, funnel emails, reply in thread, the phone's Email tab, permissions (who may see which emails).

## Things to know
- **Deliverability:** emails sent through Brevo need your domain verified in Brevo (their DKIM/SPF). Since your emails already go out fine, this is probably done.
- **Attachments:** at first, attachments are listed (name, size) but not downloaded into LyteKeepr. The same as most of Gmail's handling today.
- **Team members:** as today, the connected mailbox is the admin's.

## Order and size
1. The mail layer, with Gmail moved onto it unchanged, and checked that nothing broke. About half a day.
2. Own server: reading, threads, noise filter. About a day.
3. Own server: sending through Brevo, Sent copy, replies. About half a day.
4. Settings → Email, with the test button. About half a day.
5. Testing: on a throwaway mailbox first, then your real one with a test send. About half a day.
6. Later: Outlook.

Roughly **3 days** for your own server. Outlook later, about 1–2 days once the layer exists.

## Open before building
- The IMAP and SMTP details of your server and Brevo (host names, ports). Passwords are only typed into Settings, never sent in chat.
- Whether you ever want two mailboxes connected at once, e.g. Gmail and your own server together. This plan assumes one per workspace.
