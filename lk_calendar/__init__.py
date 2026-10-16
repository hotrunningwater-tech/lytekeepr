"""Personal calendar (tasks, meetings, reminders across projects).

Self-contained: own SQLite file, own API under /api/calendar, own static
assets under /lk-calendar. The only hooks into the rest of the app are
register(app) in server.py and the CALENDAR tab in index.html.
"""

import logging

from . import auto_sync, contacts, leads
from .routes import register as _register_routes


def register(app):
    _register_routes(app)
    try:
        leads.migrate_buckets()
    except Exception:
        logging.getLogger(__name__).exception("cold/warm upgrade failed; it will retry on the next start")
    try:
        leads.migrate_statuses_v2()
        contacts.migrate_funnel_stages()
    except Exception:
        logging.getLogger(__name__).exception("status/stage upgrade failed; it will retry on the next start")
    try:
        contacts.fix_email_urls()
        contacts.backfill_from_accounts()
        contacts.link_leads()
        contacts.fix_added_dates()
    except Exception:
        logging.getLogger(__name__).exception("linking leads to people failed; it will retry on the next start")
    auto_sync.start()      # hourly Google sync; idle unless Google is connected on this machine


__all__ = ["register"]
