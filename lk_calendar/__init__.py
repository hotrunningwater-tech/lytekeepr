"""Personal calendar (tasks, meetings, reminders across projects).

Self-contained: own SQLite file, own API under /api/calendar, own static
assets under /lk-calendar. The only hooks into the rest of the app are
register(app) in server.py and the CALENDAR tab in index.html.
"""

import logging

from . import auto_sync, contacts, leads, store, users
from .routes import register as _register_routes


def register(app):
    _register_routes(app)
    try:                                   # the first workspace takes over the old shared calendar and Google tokens now, not on first use
        from google_oauth import store as google_store
        store.db_path()
        google_store._google_dir()
    except Exception:
        logging.getLogger(__name__).exception("could not move the shared calendar or Google tokens into the first workspace")
    users.register(app)
    from . import calls
    calls.register(app)
    from . import member_google
    member_google.register(app)
    from . import opportunities
    opportunities.register(app)
    from . import relationships
    relationships.register(app)
    from . import brands
    brands.register(app)
    from . import interactions
    interactions.register(app)
    from . import email_lookup
    email_lookup.register(app)
    from . import coming_up
    coming_up.register(app)
    from . import meeting_notes
    meeting_notes.register(app)
    from . import autopilot
    autopilot.register(app)
    from . import inbox_watch
    inbox_watch.register(app)
    from . import onboarding
    onboarding.register(app)
    from . import company_view
    company_view.register(app)
    from . import funnel_drafts
    funnel_drafts.register(app)
    from mailserver import own as own_mail
    own_mail.register(app)
    from . import quiet
    quiet.register(app)
    from . import linkedin_link
    linkedin_link.register(app)
    from . import account_data, getting_started, recycle, support, work
    getting_started.register(app)
    support.register(app)
    work.register(app)
    recycle.register(app)
    account_data.register(app)
    from . import goals
    goals.register(app)
    from . import docs
    docs.register(app)
    from . import claude_connector
    claude_connector.register(app)
    from pathlib import Path
    from . import superadmin
    superadmin.register(app, str(Path(__file__).resolve().parent.parent))
    try:
        users.ensure_admin()
    except Exception:
        logging.getLogger(__name__).exception("could not add the admin to the team list; it will retry on the next sign-in")
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
    calls.start()          # hourly Fathom fetch in each workspace that connected it


__all__ = ["register"]
