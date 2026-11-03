"""Funnels made from a template (lk_calendar/funnel_templates.json; the agreed list is docs/funnel-templates.md, the rules
docs/funnel-automation.md): what moves a contact between their stages.

Stage names are free; each stage may carry a mark (new, contacted, conversation, booked, won, lost) and each funnel its pattern:
  P1 Outbound   - first message sent -> contacted, they replied -> conversation
  P2 / P3 / P4  - messages and replies are only logged (no move)
  every pattern - a calendar booking with them -> the funnel's first booked stage, if they haven't reached it; clearly not
                  interested -> lost;
                  a bounce moves nothing; never automatically to won.
Moves only go forward. The standard funnel (pattern '') keeps the older rules by stage name (contacts.sync_stage and friends).
"""

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

MARKS = ("new", "contacted", "conversation", "booked", "won", "lost")
PATTERNS = ("P1", "P2", "P3", "P4")
STATUS_MARK = {"sent": "contacted", "message_sent": "contacted", "accepted": "contacted",
               "replied": "conversation", "call booked": "conversation", "call done": "conversation",
               "done": "lost", "non customer": "lost"}
MOVES = {"P1": {"contacted", "conversation", "lost"}, "P2": {"lost"}, "P3": {"lost"}, "P4": {"lost"}}
BOOKING_DAYS_BACK = 30          # a meeting this recent or later counts as booked


@lru_cache(maxsize=1)
def templates() -> tuple:
    return tuple(json.loads((Path(__file__).with_name("funnel_templates.json")).read_text()))


def template(number) -> dict | None:
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None
    return next((t for t in templates() if t["number"] == n), None)


def pattern(c, funnel_id) -> str:
    row = c.execute("SELECT pattern FROM funnels WHERE funnel_id = ?", (funnel_id,)).fetchone()
    return (row["pattern"] or "") if row else ""


def stages(c, funnel_id) -> list:
    return [dict(r) for r in c.execute("SELECT stage_id, name, position, mark FROM funnel_stages WHERE funnel_id = ? ORDER BY position", (funnel_id,))]


def status_moves(pattern_: str, status: str) -> str:
    """The mark a status moves a contact to in this pattern, or '' (no move)."""
    mark = STATUS_MARK.get(status, "")
    return mark if mark in MOVES.get(pattern_, set()) else ""


def target_for_statuses(c, funnel_id, statuses) -> int | None:
    """The furthest stage the person's statuses reach in a template funnel (None: no move)."""
    pat = pattern(c, funnel_id)
    marks = {status_moves(pat, s) for s in statuses} - {""}
    hits = [s for s in stages(c, funnel_id) if s["mark"] in marks]
    return hits[-1]["stage_id"] if hits else None


def statuses_for_stage(pattern_: str, mark: str) -> list:
    """A card moved by hand to a stage with this mark: the statuses that agree with it (the opposite of status_moves)."""
    if mark not in MOVES.get(pattern_, set()):
        return []
    return [s for s, m in STATUS_MARK.items() if m == mark]


def _person_emails(c, person_id) -> set:
    return {(r["channel_identifier"] or "").strip().lower() for r in
            c.execute("SELECT channel_identifier FROM channel_accounts WHERE person_id = ? AND channel = 'email'", (person_id,))} - {""}


def apply_bookings() -> int:
    """A calendar meeting with someone in a template funnel (not declined, not cancelled, from the last 30 days on) moves them to the
    funnel's first booked stage, if they're before it. Never further (a later booked stage, e.g. #9's Demo Booked, is moved by the user
    or Claude), never to won, never back. Returns how many moved. Safe to run again."""
    from . import contacts, store
    since = (date.today() - timedelta(days=BOOKING_DAYS_BACK)).isoformat()
    with store._conn() as cal:
        invited: dict = {}
        for e in cal.execute("SELECT attendees, meeting_status FROM events WHERE start_date >= ? AND attendees != '' AND meeting_status != 'cancelled'", (since,)):
            try:
                people = json.loads(e["attendees"] or "[]")
            except ValueError:
                continue
            for a in people:
                if a.get("email") and not a.get("declined"):
                    invited[a["email"].strip().lower()] = True
    if not invited:
        return 0
    moved = 0
    with contacts._crm() as c:
        rows = c.execute("""SELECT p.person_id, p.funnel_id, p.funnel_stage_id FROM people p JOIN funnels f ON f.funnel_id = p.funnel_id
                            WHERE f.pattern != ''""").fetchall()
        for r in rows:
            if not (_person_emails(c, r["person_id"]) & invited.keys()):
                continue
            order = stages(c, r["funnel_id"])
            ids = [s["stage_id"] for s in order]
            now = ids.index(r["funnel_stage_id"]) if r["funnel_stage_id"] in ids else -1
            first = next((i for i, s in enumerate(order) if s["mark"] == "booked"), None)
            if first is not None and now < first:          # only up to the FIRST booked stage: the same meeting never counts twice
                c.execute("UPDATE people SET funnel_stage_id = ? WHERE person_id = ?", (order[first]["stage_id"], r["person_id"]))
                moved += 1
    return moved


def apply_bookings_quietly() -> None:
    """For hooks after the calendar changes: a problem here never breaks the sync."""
    import logging
    try:
        apply_bookings()
    except Exception:
        logging.getLogger(__name__).exception("moving booked contacts failed; it runs again after the next calendar sync")
