# Backups

Status: live since 2026-11-03.

## What runs

| When (UTC) | What | Where it is kept |
|---|---|---|
| 07:00 nightly | A copy of each workspace's databases | On the server (`/opt/lytekeepr-calendar/backups`), 14 days |
| 07:30 nightly | An encrypted copy of all LyteKeepr data | Backblaze, bucket `lytekeepr-backups`, 14 days |
| 09:00 Sundays | The newest Backblaze copy is downloaded, decrypted and checked | Nothing kept |

The Backblaze copy has every workspace (databases, saved emails, Google connections, recordings), the directory of accounts and
the server's settings file (its key is needed to read the Google connections). Workspaces already deleted or closed are left out,
so they are erased on schedule (30 days after closing; copies made before that are gone 14 days later).

If a night fails, does not run, or a Sunday check fails, the owner gets a task on Home ("Fix backups") and an email, once a day,
until it works again (`lk_calendar/backup_watch.py`).

## What is needed to restore, and where it is

1. **The Backblaze login** (Backblaze account of the owner): in the owner's password manager.
2. **The backup password**: in the owner's password manager. Without it no copy can be opened, by anyone.
3. A server with Python 3 and openssl.

The server also holds the Backblaze key and the password, in `/opt/lytekeepr-calendar/backblaze.env` (root only). If the server is
lost, make a new application key in Backblaze (limited to `lytekeepr-backups`, read and write).

## Restore

1. On the new server, put `scripts/offsite_backup.py` and a `backblaze.env` with `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET=lytekeepr-backups`
   and `BACKUP_PASSWORD` in one folder (`LK_OFFSITE_ROOT` points to it).
2. `python3 offsite_backup.py restore /root/restore`: the newest copy, decrypted and checked, lands in `/root/restore/lytekeepr`.
3. Deploy the app as usual, stop it, then copy `lytekeepr/profiles/*` into `/opt/lytekeepr-calendar/app/profiles/` and
   `lytekeepr/lytekeepr.env` to `/opt/lytekeepr-calendar/lytekeepr.env`; give the files to the service user (`chown -R lkcal`), start it.
4. An older copy: list the bucket in Backblaze, download the file, and use `unpack` from the script with the password.

The first restore was tested on 2026-11-03: every database sound, and contacts, messages, events and goals matched the live data.
