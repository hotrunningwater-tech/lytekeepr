"""One-way, read-only pull of the PRIMARY Google Calendar into the calendar's events table.

- Only timed events are imported (all-day events are skipped). Repeating events arrive already
  expanded into single occurrences (singleEvents=true), so no repeat rules are translated.
- Rows are keyed by external_id and marked source='google', project='google'. Title, time, length and the
  Google description are overwritten on every sync; your own notes (my_notes) and meeting status are never touched.
- An event that disappears from Google inside the synced window is deleted, unless you added notes/status/a follow-up
  to it: then it is kept and marked cancelled. Nothing outside the window is ever removed, and nothing is removed
  at all unless the whole fetch succeeded.
- Manual events, lead follow-ups and leads are never read or written here.
"""

import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import store

API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
PAST_DAYS, FUTURE_DAYS = 30, 180
CALL_RE = re.compile(r"(zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|webex\.com|whereby\.com|gotomeeting\.com|gotomeet\.me)", re.I)
FIELDS = ("nextPageToken,items(id,status,summary,description,location,hangoutLink,start,end,"
          "conferenceData(entryPoints(uri,entryPointType)),attendees(email,displayName,self,organizer))")


class GoogleSyncError(Exception):
    pass


class GoogleAuthError(GoogleSyncError):
    """Google authorization is missing, expired or lacks Calendar permission: the user has to reconnect Google."""

    def __init__(self, message: str, never_connected: bool = False):
        super().__init__(message)
        self.never_connected = never_connected


_LOCK = threading.Lock()      # the hourly job, the button and the chat command all take turns: no overlapping syncs
RECONNECT_HINT = "Reconnect Google (open /auth/google/start), approve Calendar access, then sync again."


def is_connected() -> bool:
    """True if a Google refresh token has ever been saved on this machine (it may still have expired)."""
    try:
        from google_oauth.store import has_refresh_token
        return bool(has_refresh_token())
    except Exception:       # no Google module/config here (e.g. the live server): nothing to sync
        return False


def can_create_meetings() -> bool:
    """True if the saved Google connection includes the "create calendar events" permission (connections made before it was added do not)."""
    try:
        from google_oauth.store import load_tokens
        scopes = ((load_tokens() or {}).get("scope") or "").split()
    except Exception:
        return False
    return any(s.endswith("/auth/calendar.events") or s.endswith("/auth/calendar") for s in scopes)


def _access_token() -> str:
    try:
        from google_oauth.errors import GoogleOAuthError      # imported here so a machine without Google set up still runs
        from google_oauth.tokens import get_valid_access_token
    except ImportError as exc:
        raise GoogleAuthError("Google is not set up on this machine.", never_connected=True) from exc
    try:
        return get_valid_access_token()
    except GoogleOAuthError as exc:
        text = str(exc).lower()
        if "could not reach" in text:                          # network blip: not an authorization problem
            raise GoogleSyncError(f"Could not reach Google to refresh authorization: {exc}") from exc
        if "not connected" in text or "missing google_" in text:
            raise GoogleAuthError("Google is not connected on this machine. " + RECONNECT_HINT, never_connected=True) from exc
        raise GoogleAuthError("Google authorization expired or was revoked. " + RECONNECT_HINT) from exc


def _fetch_events(window_start: datetime, window_end: datetime) -> list[dict]:
    """All events (every page) between two UTC datetimes. Separate function so tests can replace the network."""
    token = _access_token()
    items, page = [], None
    while True:
        params = {"timeMin": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeMax": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "singleEvents": "true", "orderBy": "startTime", "maxResults": "250", "fields": FIELDS}
        if page:
            params["pageToken"] = page
        req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            low = body.lower()
            if exc.code == 401:
                raise GoogleAuthError("Google rejected the saved authorization. " + RECONNECT_HINT) from exc
            if exc.code == 403 and ("insufficientpermissions" in low or "scope_insufficient" in low or "insufficient authentication scopes" in low):
                raise GoogleAuthError("Calendar permission has not been granted. " + RECONNECT_HINT) from exc
            if exc.code == 403 and ("accessnotconfigured" in low or "has not been used in project" in low or "is disabled" in low):
                raise GoogleSyncError("The Google Calendar API is not enabled for your Google project (turn it on in Google Cloud Console, APIs & Services).") from exc
            raise GoogleSyncError(f"Google Calendar API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise GoogleSyncError(f"Could not reach Google: {exc}") from exc
        items.extend(data.get("items", []))
        page = data.get("nextPageToken")
        if not page:
            return items


def _local(dt_text: str) -> datetime:
    return datetime.fromisoformat(dt_text).astimezone(store.TZ).replace(tzinfo=None)


def _clean_text(text: str) -> str:
    text = re.sub(r"<\s*br\s*/?>|</p>|</div>|</li>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _convert(item: dict) -> dict | None:
    """A Google event -> our columns, or None for cancelled / all-day / malformed events."""
    if item.get("status") == "cancelled":
        return None
    start, end = item.get("start", {}), item.get("end", {})
    if "dateTime" not in start or "dateTime" not in end:      # all-day events carry "date" only
        return None
    try:
        s, e = _local(start["dateTime"]), _local(end["dateTime"])
    except ValueError:
        return None
    minutes = int((e - s).total_seconds() // 60)
    entry_points = [p.get("uri", "") for p in (item.get("conferenceData") or {}).get("entryPoints", [])]
    join = item.get("hangoutLink") or next((u for u in entry_points if u), "")
    location = item.get("location") or ""
    description = _clean_text(item.get("description") or "")
    has_call = bool(join) or bool(CALL_RE.search(f"{location} {description}"))
    notes = "\n".join(x for x in (f"Join: {join}" if join else "", f"Location: {location}" if location else "") if x)
    notes = (notes + "\n\n" + description).strip() if description else notes
    attendees = [{"email": (a.get("email") or "").lower(), "name": a.get("displayName") or ""}
                 for a in item.get("attendees") or [] if a.get("email") and not a.get("self")]      # everyone invited except you
    return {"external_id": "google:primary:" + item["id"], "title": (item.get("summary") or "(no title)").strip()[:200],
            "start_date": s.date().isoformat(), "start_time": s.strftime("%H:%M"),
            "duration_min": min(max(minutes, 5), 24 * 60), "notes": notes[:2000], "has_call": 1 if has_call else 0,
            "attendees": json.dumps(attendees), "join_url": join[:500]}


def _sync_locked() -> dict:
    now = store.now_local()
    local_start = datetime.combine(now.date() - timedelta(days=PAST_DAYS), datetime.min.time())
    local_end = datetime.combine(now.date() + timedelta(days=FUTURE_DAYS), datetime.min.time())
    to_utc = lambda d: d.replace(tzinfo=store.TZ).astimezone(timezone.utc)
    items = _fetch_events(to_utc(local_start), to_utc(local_end))      # raises before anything is written if Google fails

    wanted, skipped = {}, 0
    for item in items:
        row = _convert(item)
        if row:
            wanted[row["external_id"]] = row
        elif item.get("status") != "cancelled":
            skipped += 1                                                 # all-day or unreadable
    result = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "kept_as_cancelled": 0, "skipped_all_day": skipped,
              "window": [local_start.date().isoformat(), local_end.date().isoformat()]}

    with store._conn() as conn:
        for ext, row in wanted.items():
            cur = conn.execute("SELECT * FROM events WHERE external_id = ?", (ext,)).fetchone()
            fields = store.clean({**row, "project": "google", "kind": "meeting", "source": "google"})
            if cur is None:
                conn.execute("""INSERT OR IGNORE INTO events (title, project, kind, start_date, start_time, duration_min, repeat, repeat_days,
                                notes, source, lead_ref, created_at, external_id, has_call, attendees, join_url)
                                VALUES (:title, :project, :kind, :start_date, :start_time, :duration_min, :repeat, :repeat_days,
                                :notes, 'google', '', :created_at, :external_id, :has_call, :attendees, :join_url)""",
                             {**fields, "created_at": store.now_local().isoformat(), "external_id": ext, "has_call": row["has_call"],
                              "attendees": row["attendees"], "join_url": row["join_url"]})
                result["added"] += 1       # INSERT OR IGNORE + the unique index mean two racing syncs can never create a duplicate
                continue
            same = (all(cur[k] == fields[k] for k in ("title", "start_date", "start_time", "duration_min", "notes")) and cur["has_call"] == row["has_call"]
                    and cur["attendees"] == row["attendees"] and cur["join_url"] == row["join_url"])
            if same:
                result["unchanged"] += 1
                continue
            conn.execute("UPDATE events SET title=?, start_date=?, start_time=?, duration_min=?, notes=?, has_call=?, attendees=?, join_url=? WHERE id=?",
                         (fields["title"], fields["start_date"], fields["start_time"], fields["duration_min"], fields["notes"],
                          row["has_call"], row["attendees"], row["join_url"], cur["id"]))
            result["updated"] += 1
            store.meeting_follow_up(conn, dict(conn.execute("SELECT * FROM events WHERE id = ?", (cur["id"],)).fetchone()))

        gone = conn.execute("SELECT * FROM events WHERE source = 'google' AND start_date BETWEEN ? AND ?", result["window"]).fetchall()
        for ev in gone:
            if ev["external_id"] in wanted:
                continue
            has_followup = conn.execute("SELECT 1 FROM events WHERE lead_ref = ? AND source = 'auto'", (f"meeting:{ev['id']}",)).fetchone()
            if ev["my_notes"] or ev["meeting_status"] or has_followup:
                if not ev["meeting_status"]:
                    conn.execute("UPDATE events SET meeting_status = 'cancelled' WHERE id = ?", (ev["id"],))
                    result["kept_as_cancelled"] += 1
            else:
                conn.execute("DELETE FROM events WHERE id = ?", (ev["id"],))
                result["removed"] += 1
    return result


def _record(trigger: str, ok: bool, message: str = "", needs_reconnect: bool = False, result: dict | None = None) -> None:
    now = store.now_local().strftime("%Y-%m-%d %H:%M")
    store.set_state("google_last_attempt", str(time.time()))
    store.set_state("google_sync_status", json.dumps({"at": now, "trigger": trigger, "ok": ok, "message": message,
                                                      "needs_reconnect": needs_reconnect, "result": result}))
    if ok:
        store.set_state("last_google_sync", now)
        store.set_state("last_google_sync_result", json.dumps(result))


def sync(trigger: str = "manual") -> dict:
    """Run one sync. Safe to call from the hourly job, the button and the chat at the same time."""
    with _LOCK:
        try:
            result = _sync_locked()
        except GoogleAuthError as exc:
            _record(trigger, False, str(exc), needs_reconnect=not exc.never_connected)
            raise
        except GoogleSyncError as exc:
            _record(trigger, False, str(exc))
            raise
        _record(trigger, True, result=result)
        return result


def status() -> dict:
    raw = store.get_state("google_sync_status")
    last = json.loads(raw) if raw else {}
    return {"last_sync": store.get_state("last_google_sync"), "connected": is_connected(),
            "last_attempt": last.get("at"), "last_trigger": last.get("trigger"), "last_ok": last.get("ok"),
            "message": last.get("message", ""), "needs_reconnect": bool(last.get("needs_reconnect")),
            "can_create_meetings": can_create_meetings()}


# ------------------------------------------------------------------ creating a meeting (needs the "create calendar events" permission)
def delete_meeting(event_id: int) -> bool:
    """Delete a Google meeting from the user's Google Calendar (people invited get Google's cancellation email) and from here."""
    ev = store.get_event(event_id)
    if not ev:
        return False
    if ev["source"] != "google" or not (ev["external_id"] or "").startswith("google:primary:"):
        raise GoogleSyncError("This is not a Google Calendar meeting.")
    google_id = ev["external_id"].split(":", 2)[2]
    token = _access_token()
    req = urllib.request.Request(f"{API}/{urllib.parse.quote(google_id)}?sendUpdates=all", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        low = exc.read().decode(errors="replace").lower()
        if exc.code in (404, 410):
            pass                                    # already gone from Google: just remove it here too
        elif exc.code == 401 or (exc.code == 403 and ("insufficient" in low or "scope" in low)):
            raise GoogleAuthError("Google hasn't allowed LyteKeepr to change your calendar yet. Press Reconnect Google and tick "
                                  "every box, then try again.") from exc
        else:
            raise GoogleSyncError(f"Google could not delete the meeting (error {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise GoogleSyncError(f"Could not reach Google: {exc}") from exc
    with store._conn() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return True


def create_meeting(title: str, start_date: str, start_time: str, duration_min: int, invitees: list, add_meet: bool = True,
                   notes: str = "", expect_recording: bool = True, project: str = "google") -> dict:
    """Put a meeting in the user's Google Calendar, with a Google Meet link if asked, and send the invites. It is then kept here like
    any synced Google meeting (with its invitees), and marked to expect a Fathom recording."""
    import uuid
    if not title.strip():
        raise GoogleSyncError("Please enter a title.")
    start = datetime.fromisoformat(f"{start_date}T{start_time}")
    end = start + timedelta(minutes=int(duration_min or 30))
    zone = str(store.TZ)
    body = {"summary": title.strip()[:200], "description": notes or "",
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": zone},
            "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": zone},
            "attendees": [{"email": e} for e in invitees]}
    if add_meet:
        body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    token = _access_token()
    params = urllib.parse.urlencode({"conferenceDataVersion": 1 if add_meet else 0, "sendUpdates": "all" if invitees else "none"})
    req = urllib.request.Request(f"{API}?{params}", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            item = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        low = exc.read().decode(errors="replace").lower()
        if exc.code == 401 or (exc.code == 403 and ("insufficient" in low or "scope" in low)):
            raise GoogleAuthError("Google hasn't allowed LyteKeepr to create meetings yet. Press Reconnect Google and tick "
                                  "every box, then save again.") from exc
        raise GoogleSyncError(f"Google could not create the meeting (error {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise GoogleSyncError(f"Could not reach Google: {exc}") from exc
    row = _convert(item)
    if not row:
        raise GoogleSyncError("Google created the meeting but sent back something unreadable; it will appear after the next sync.")
    with store._conn() as conn:
        fields = store.clean({**row, "project": "google", "kind": "meeting", "source": "google", "notes": row["notes"]})
        conn.execute("""INSERT OR IGNORE INTO events (title, project, kind, start_date, start_time, duration_min, repeat, repeat_days,
                        notes, source, lead_ref, created_at, external_id, has_call, attendees, join_url, expect_recording)
                        VALUES (:title, :project, :kind, :start_date, :start_time, :duration_min, :repeat, :repeat_days,
                        :notes, 'google', '', :created_at, :external_id, :has_call, :attendees, :join_url, :expect)""",
                     {**fields, "created_at": store.now_local().isoformat(), "external_id": row["external_id"], "has_call": row["has_call"],
                      "attendees": row["attendees"], "join_url": row["join_url"], "expect": 1 if expect_recording else 0})
        ev = conn.execute("SELECT * FROM events WHERE external_id = ?", (row["external_id"],)).fetchone()
    return {"id": ev["id"], "title": ev["title"], "start_date": ev["start_date"], "start_time": ev["start_time"], "join_url": ev["join_url"],
            "attendees": json.loads(ev["attendees"] or "[]")}
