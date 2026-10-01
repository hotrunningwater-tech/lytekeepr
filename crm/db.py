"""
Local CRM storage — SQLite, one file per profile (same convention as every
other per-profile store in this app: profiles/store.py's
profile_component_dir()). Four tables, exactly as specced:

  Companies       -> companies
  People          -> people
  Channel Accounts -> channel_accounts
  Messages        -> messages

No cross-channel identity matching happens here. Every new contact from a
channel becomes its own Person until a human explicitly merges two people
together (merge_people(), added once a second channel makes matching a
real problem worth solving) — see get_or_create_person_for_channel().
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from profiles.store import profile_component_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    company_status TEXT NOT NULL DEFAULT 'active',
    notes TEXT
);

-- A funnel is a named pipeline made of ordered stages (funnel_stages).
-- A person is in at most one funnel, in at most one stage of it, at a
-- time (people.funnel_id / people.funnel_stage_id) — not a join table,
-- matches "move to funnel/stage" being a single action, same pattern as
-- the rest of this schema.
CREATE TABLE IF NOT EXISTS funnels (
    funnel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funnel_stages (
    stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    funnel_id INTEGER NOT NULL REFERENCES funnels(funnel_id),
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funnel_stages_funnel ON funnel_stages(funnel_id, position);

CREATE TABLE IF NOT EXISTS people (
    person_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(company_id),
    funnel_id INTEGER REFERENCES funnels(funnel_id),
    funnel_stage_id INTEGER REFERENCES funnel_stages(stage_id),
    name TEXT NOT NULL,
    job_title TEXT,
    person_status TEXT NOT NULL DEFAULT 'active',
    notes TEXT
);

-- contact_status: 'trusted' (shows in the main conversation list) or
-- 'pending_review' (inbound-only, never-contacted-before sender — sits in
-- the Inbox tab until approved or removed, so daily spam/ads don't flood
-- the main CRM view). See integrations/linkedin/sync.py for the rule.
CREATE TABLE IF NOT EXISTS channel_accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES people(person_id),
    channel TEXT NOT NULL,
    channel_identifier TEXT,
    display_name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    contact_status TEXT NOT NULL DEFAULT 'trusted',
    unread INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES people(person_id),
    account_id INTEGER NOT NULL REFERENCES channel_accounts(account_id),
    thread_id TEXT,
    channel TEXT NOT NULL,
    sender TEXT,
    direction TEXT NOT NULL,
    message_text TEXT,
    timestamp TEXT,
    external_message_id TEXT,
    UNIQUE (channel, external_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_person ON messages(person_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_channel_accounts_person ON channel_accounts(person_id);

-- One active outreach sequence per (person, channel). follow_up_type is
-- 'new_lead' | 'dormant_customer' | 'warm_stalled' — stored for future use,
-- doesn't currently change timing/behavior (all sequences use the same
-- short test interval for now; real per-type cadences come later).
-- status: 'active' (waiting for next_follow_up_at) ->
--   'awaiting_approval' (a draft is staged, waiting on the user) -> back to
--   'active' after a send, or terminal: 'cancelled' (got a real reply),
--   'stopped' (asked to stop), 'completed' (ran out of stages).
CREATE TABLE IF NOT EXISTS follow_ups (
    follow_up_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES people(person_id),
    channel TEXT NOT NULL,
    conversation_ref TEXT,
    follow_up_type TEXT NOT NULL DEFAULT 'new_lead',
    stage INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    status_reason TEXT,
    last_outreach_at TEXT,
    next_follow_up_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_follow_ups_person ON follow_ups(person_id, channel);

-- Recorded whenever a person is removed via the CRM Remove action, keyed by
-- the same (channel, channel_identifier) used to match contacts on
-- ingestion — so a removed contact never silently comes back on the next
-- sync. delete_person() populates this from the person's channel_accounts
-- right before deleting them.
CREATE TABLE IF NOT EXISTS excluded_contacts (
    channel TEXT NOT NULL,
    channel_identifier TEXT NOT NULL,
    excluded_at TEXT NOT NULL,
    PRIMARY KEY (channel, channel_identifier)
);

-- Structured diagnostic trail. Every major action (sync attempts, sends,
-- follow-up ticks, CRM writes) logs one row here with its REAL outcome —
-- not just an HTTP 200, which tells you nothing about what actually
-- happened inside. Queryable directly (see log_activity/list_activity_log)
-- instead of reconstructing events from raw server output after the fact.
-- run_id groups every step of ONE operation (one sync call, one send, one
-- follow-up tick) together, so the log for a single run reads as the
-- documented step sequence with real data filled in, not an unordered
-- pile of independent rows.
CREATE TABLE IF NOT EXISTS activity_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_num INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    component TEXT NOT NULL,
    step TEXT NOT NULL,
    detail TEXT,
    data TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_log_run ON activity_log(run_id, step_num);
CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at);
"""


def _db_path() -> Path:
    return profile_component_dir("crm") / "lytekeepr.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader/writer (e.g. log_activity's own short-lived
    # connection) proceed while another connection has an open transaction
    # elsewhere in the app; busy_timeout makes any remaining brief
    # contention retry instead of throwing "database is locked" immediately.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)
        # people.company_checked: added after the original schema, so an
        # existing db needs the column added rather than re-created.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(people)")}
        if "company_checked" not in existing_columns:
            conn.execute("ALTER TABLE people ADD COLUMN company_checked INTEGER NOT NULL DEFAULT 0")
        if "funnel_id" not in existing_columns:
            conn.execute("ALTER TABLE people ADD COLUMN funnel_id INTEGER REFERENCES funnels(funnel_id)")
        if "funnel_stage_id" not in existing_columns:
            conn.execute("ALTER TABLE people ADD COLUMN funnel_stage_id INTEGER REFERENCES funnel_stages(stage_id)")

        existing_ca_columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_accounts)")}
        if "contact_status" not in existing_ca_columns:
            conn.execute("ALTER TABLE channel_accounts ADD COLUMN contact_status TEXT NOT NULL DEFAULT 'trusted'")
        if "unread" not in existing_ca_columns:
            conn.execute("ALTER TABLE channel_accounts ADD COLUMN unread INTEGER NOT NULL DEFAULT 0")

        # Backfill: funnels created before funnel_stages existed have none —
        # seed them the same as a brand-new funnel, and drop anyone already
        # in that funnel into its first stage.
        now = datetime.now().isoformat()
        stageless_funnels = conn.execute(
            "SELECT f.funnel_id FROM funnels f "
            "LEFT JOIN funnel_stages s ON s.funnel_id = f.funnel_id "
            "WHERE s.stage_id IS NULL"
        ).fetchall()
        for row in stageless_funnels:
            fid = row["funnel_id"]
            conn.executemany(
                "INSERT INTO funnel_stages (funnel_id, name, position, created_at) VALUES (?, ?, ?, ?)",
                [(fid, stage_name, i, now) for i, stage_name in enumerate(DEFAULT_FUNNEL_STAGES)],
            )
            first_stage = conn.execute(
                "SELECT stage_id FROM funnel_stages WHERE funnel_id = ? ORDER BY position LIMIT 1", (fid,)
            ).fetchone()
            conn.execute(
                "UPDATE people SET funnel_stage_id = ? WHERE funnel_id = ? AND funnel_stage_id IS NULL",
                (first_stage["stage_id"], fid),
            )
        conn.commit()
    finally:
        conn.close()


# Seeded on every new funnel; renamed/reordered by editing funnel_stages
# rows directly — no stage-editing UI yet, this is just a sane starting
# pipeline for outreach (not the invoicing-heavy stage set a sales CRM
# would use).
DEFAULT_FUNNEL_STAGES = ["New lead", "Contacted", "In conversation", "Decision pending", "Won", "Lost"]


def create_funnel(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO funnels (name, created_at) VALUES (?, ?)",
        (name, datetime.now().isoformat()),
    )
    funnel_id = cur.lastrowid
    now = datetime.now().isoformat()
    conn.executemany(
        "INSERT INTO funnel_stages (funnel_id, name, position, created_at) VALUES (?, ?, ?, ?)",
        [(funnel_id, stage_name, i, now) for i, stage_name in enumerate(DEFAULT_FUNNEL_STAGES)],
    )
    return funnel_id


def list_funnels(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT f.funnel_id, f.name, f.created_at, COUNT(p.person_id) AS people_count
        FROM funnels f
        LEFT JOIN people p ON p.funnel_id = f.funnel_id
        GROUP BY f.funnel_id
        ORDER BY f.created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def list_funnel_stages(conn: sqlite3.Connection, funnel_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.stage_id, s.name, s.position, COUNT(p.person_id) AS people_count
        FROM funnel_stages s
        LEFT JOIN people p ON p.funnel_stage_id = s.stage_id
        WHERE s.funnel_id = ?
        GROUP BY s.stage_id
        ORDER BY s.position
        """,
        (funnel_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_funnel_board(conn: sqlite3.Connection, funnel_id: int) -> dict | None:
    funnel_row = conn.execute("SELECT funnel_id, name FROM funnels WHERE funnel_id = ?", (funnel_id,)).fetchone()
    if funnel_row is None:
        return None
    stages = conn.execute(
        "SELECT stage_id, name, position FROM funnel_stages WHERE funnel_id = ? ORDER BY position",
        (funnel_id,),
    ).fetchall()
    people_rows = conn.execute(
        """
        SELECT p.person_id, p.name, p.funnel_stage_id, c.company_name
        FROM people p
        LEFT JOIN companies c ON c.company_id = p.company_id
        WHERE p.funnel_id = ?
        ORDER BY p.name
        """,
        (funnel_id,),
    ).fetchall()
    people_by_stage: dict[int, list[dict]] = {}
    for row in people_rows:
        people_by_stage.setdefault(row["funnel_stage_id"], []).append(
            {"person_id": row["person_id"], "name": row["name"], "company_name": row["company_name"]}
        )
    return {
        "funnel_id": funnel_row["funnel_id"],
        "name": funnel_row["name"],
        "stages": [
            {
                "stage_id": s["stage_id"],
                "name": s["name"],
                "position": s["position"],
                "people": people_by_stage.get(s["stage_id"], []),
            }
            for s in stages
        ],
    }


def delete_funnel(conn: sqlite3.Connection, funnel_id: int) -> None:
    """Unassigns everyone in this funnel (same as move_people_to_funnel's
    funnel_id=None — they're kept, just no longer in a funnel/stage),
    then removes the funnel's stages and the funnel itself."""
    conn.execute(
        "UPDATE people SET funnel_id = NULL, funnel_stage_id = NULL WHERE funnel_id = ?",
        (funnel_id,),
    )
    conn.execute("DELETE FROM funnel_stages WHERE funnel_id = ?", (funnel_id,))
    conn.execute("DELETE FROM funnels WHERE funnel_id = ?", (funnel_id,))


def move_people_to_funnel(conn: sqlite3.Connection, person_ids: list[int], funnel_id: int | None) -> None:
    """funnel_id=None takes people out of whatever funnel they're in.
    Otherwise drops them into that funnel's first stage."""
    first_stage_id = None
    if funnel_id is not None:
        row = conn.execute(
            "SELECT stage_id FROM funnel_stages WHERE funnel_id = ? ORDER BY position LIMIT 1",
            (funnel_id,),
        ).fetchone()
        first_stage_id = row["stage_id"] if row else None
    placeholders = ",".join("?" for _ in person_ids)
    conn.execute(
        f"UPDATE people SET funnel_id = ?, funnel_stage_id = ? WHERE person_id IN ({placeholders})",
        [funnel_id, first_stage_id, *person_ids],
    )


def move_person_to_stage(conn: sqlite3.Connection, person_id: int, stage_id: int) -> None:
    row = conn.execute("SELECT funnel_id FROM funnel_stages WHERE stage_id = ?", (stage_id,)).fetchone()
    if row is None:
        raise ValueError(f"No such stage: {stage_id}")
    conn.execute(
        "UPDATE people SET funnel_id = ?, funnel_stage_id = ? WHERE person_id = ?",
        (row["funnel_id"], stage_id, person_id),
    )


def get_or_create_company(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT company_id FROM companies WHERE company_name = ?", (name,)).fetchone()
    if row is not None:
        return row["company_id"]
    cur = conn.execute("INSERT INTO companies (company_name) VALUES (?)", (name,))
    return cur.lastrowid


def list_people_missing_company(conn: sqlite3.Connection, channel: str) -> list[dict]:
    """People on this channel who haven't been checked for a company yet —
    company_checked stays true even when nothing was found, so this never
    re-searches the same person forever."""
    rows = conn.execute(
        """
        SELECT DISTINCT p.person_id, p.name
        FROM people p
        JOIN channel_accounts ca ON ca.person_id = p.person_id AND ca.channel = ?
        WHERE p.company_checked = 0
        """,
        (channel,),
    ).fetchall()
    return [dict(row) for row in rows]


def set_person_company(conn: sqlite3.Connection, person_id: int, company_id: int | None) -> None:
    conn.execute(
        "UPDATE people SET company_id = ?, company_checked = 1 WHERE person_id = ?",
        (company_id, person_id),
    )


def mark_company_checked(conn: sqlite3.Connection, person_id: int) -> None:
    conn.execute("UPDATE people SET company_checked = 1 WHERE person_id = ?", (person_id,))


def update_person(
    conn: sqlite3.Connection,
    person_id: int,
    name: str,
    company_name: str | None,
    job_title: str | None,
    notes: str | None,
) -> None:
    """Manual edit from the Contact detail view — a contact's name/company
    is often just whatever a message's sender line said (e.g. "Peter A"),
    which can be wrong or incomplete; this is the correction path. An
    empty company_name clears the company (get_or_create_company is only
    called when one was actually given)."""
    company_id = get_or_create_company(conn, company_name) if company_name else None
    conn.execute(
        "UPDATE people SET name = ?, company_id = ?, job_title = ?, notes = ? WHERE person_id = ?",
        (name, company_id, job_title or None, notes, person_id),
    )


def upgrade_channel_identifier(conn: sqlite3.Connection, person_id: int, channel: str, real_identifier: str) -> None:
    """Replaces the channel_identifier only if it's still the name-based
    fallback ("name:X", used when we never got a real slug/address for
    this channel — see get_or_create_person_for_channel) — never
    overwrites an identifier that's already real. display_name (the
    person's actual name, shown in the UI regardless) is untouched."""
    conn.execute(
        "UPDATE channel_accounts SET channel_identifier = ? "
        "WHERE person_id = ? AND channel = ? AND channel_identifier LIKE 'name:%'",
        (real_identifier, person_id, channel),
    )


def find_channel_account(conn: sqlite3.Connection, channel: str, channel_identifier: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM channel_accounts WHERE channel = ? AND channel_identifier = ?",
        (channel, channel_identifier),
    ).fetchone()


def create_person(conn: sqlite3.Connection, name: str, company_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO people (company_id, name, person_status) VALUES (?, ?, 'active')",
        (company_id, name),
    )
    return cur.lastrowid


def create_channel_account(
    conn: sqlite3.Connection,
    person_id: int,
    channel: str,
    channel_identifier: str | None,
    display_name: str | None,
    contact_status: str = "trusted",
) -> int:
    cur = conn.execute(
        "INSERT INTO channel_accounts (person_id, channel, channel_identifier, display_name, contact_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (person_id, channel, channel_identifier, display_name, contact_status),
    )
    return cur.lastrowid


def get_or_create_person_for_channel(
    conn: sqlite3.Connection,
    channel: str,
    channel_identifier: str,
    display_name: str,
    contact_status: str = "trusted",
) -> tuple[int, int]:
    """No cross-channel matching: this only ever looks within `channel`
    itself. A fresh contact on a channel we've never seen this identifier
    on before always becomes a brand new Person. contact_status only
    applies to a brand-new channel_account — an existing one is never
    downgraded by a later call. Returns (person_id, account_id)."""
    existing = find_channel_account(conn, channel, channel_identifier)
    if existing is not None:
        return existing["person_id"], existing["account_id"]

    person_id = create_person(conn, display_name)
    account_id = create_channel_account(conn, person_id, channel, channel_identifier, display_name, contact_status)
    return person_id, account_id


def set_contact_status(conn: sqlite3.Connection, account_id: int, contact_status: str) -> None:
    conn.execute("UPDATE channel_accounts SET contact_status = ? WHERE account_id = ?", (contact_status, account_id))


def set_unread(conn: sqlite3.Connection, account_id: int, unread: bool) -> None:
    conn.execute("UPDATE channel_accounts SET unread = ? WHERE account_id = ?", (1 if unread else 0, account_id))


def get_notification_counts(conn: sqlite3.Connection) -> dict:
    """Everything the HOME tab badge needs in one call — real, live counts,
    not tied to whether a popup was dismissed."""
    followups = conn.execute(
        "SELECT COUNT(*) AS n FROM follow_ups WHERE status IN ('awaiting_approval', 'needs_review')"
    ).fetchone()["n"]
    unread_linkedin = conn.execute(
        "SELECT COUNT(*) AS n FROM channel_accounts WHERE channel = 'linkedin' AND unread = 1 AND contact_status = 'trusted'"
    ).fetchone()["n"]
    return {"followups": followups, "unread_linkedin": unread_linkedin, "total": followups + unread_linkedin}


def insert_message(
    conn: sqlite3.Connection,
    person_id: int,
    account_id: int,
    thread_id: str,
    channel: str,
    sender: str | None,
    direction: str,
    message_text: str | None,
    timestamp: str | None,
    external_message_id: str | None,
) -> bool:
    """Returns True if a new row was inserted, False if it already existed
    (deduped by (channel, external_message_id))."""
    try:
        conn.execute(
            "INSERT INTO messages "
            "(person_id, account_id, thread_id, channel, sender, direction, "
            " message_text, timestamp, external_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                person_id,
                account_id,
                thread_id,
                channel,
                sender,
                direction,
                message_text,
                timestamp,
                external_message_id,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate external_message_id for this channel


def list_conversations(conn: sqlite3.Connection, channel: str, contact_status: str = "trusted") -> list[dict]:
    """One row per person on this channel matching contact_status, with
    their most recent message as a preview — what the CRM actually has
    stored, not a live re-search. contact_status='trusted' is the normal
    main view; 'pending_review' is the Inbox tab of unvetted, inbound-only
    senders."""
    rows = conn.execute(
        """
        SELECT
            p.person_id,
            p.name AS participant_name,
            c.company_name,
            ca.account_id,
            ca.contact_status,
            m.thread_id,
            m.message_text AS last_message_text,
            m.timestamp AS last_message_at,
            m.direction AS last_message_direction
        FROM people p
        JOIN channel_accounts ca ON ca.person_id = p.person_id AND ca.channel = ? AND ca.contact_status = ?
        LEFT JOIN companies c ON c.company_id = p.company_id
        LEFT JOIN messages m ON m.message_id = (
            SELECT message_id FROM messages
            WHERE person_id = p.person_id AND channel = ?
            ORDER BY timestamp DESC, message_id DESC
            LIMIT 1
        )
        ORDER BY m.message_id DESC
        """,
        (channel, contact_status, channel),
    ).fetchall()
    return [dict(row) for row in rows]


def search_messages(
    conn: sqlite3.Connection,
    channel: str | None = None,
    person_name: str | None = None,
    query: str | None = None,
    since_days: float | None = 30,
    limit: int = 50,
) -> list[dict]:
    """Chronological search over stored messages, across every channel by
    default (pass channel to narrow to one). Used by the AI chat tool so it
    can answer questions about CRM conversation history regardless of which
    channel a contact was reached on."""
    sql = (
        "SELECT p.name AS participant_name, c.company_name, m.channel, "
        "m.sender, m.direction, m.message_text, m.timestamp "
        "FROM messages m "
        "JOIN people p ON p.person_id = m.person_id "
        "LEFT JOIN companies c ON c.company_id = p.company_id "
        # Unvetted, inbound-only senders (Inbox tab candidates) shouldn't
        # surface in AI answers as if they were real relationship history.
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM channel_accounts ca WHERE ca.person_id = p.person_id "
        "  AND ca.channel = m.channel AND ca.contact_status = 'pending_review'"
        ")"
    )
    params: list = []
    if channel:
        sql += " AND m.channel = ?"
        params.append(channel)
    if person_name:
        sql += " AND p.name LIKE ?"
        params.append(f"%{person_name}%")
    if query:
        sql += " AND m.message_text LIKE ?"
        params.append(f"%{query}%")
    if since_days:
        cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
        sql += " AND (m.timestamp IS NULL OR m.timestamp >= ?)"
        params.append(cutoff)
    sql += " ORDER BY m.timestamp ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_messages_for_person(conn: sqlite3.Connection, channel: str, person_id: int) -> list[dict]:
    # message_id, not timestamp, decides order here: LinkedIn gives no real
    # per-message timestamp, only a display string we approximate from (see
    # _parse_activity_text in integrations/linkedin/sync.py) — that guess is
    # fine for showing a rough "when", but proved unreliable enough to
    # invert real chronological order. message_id is always correct because
    # messages are only ever inserted in the true order LinkedIn returns
    # them within one conversation fetch.
    rows = conn.execute(
        "SELECT * FROM messages WHERE channel = ? AND person_id = ? "
        "ORDER BY message_id ASC",
        (channel, person_id),
    ).fetchall()
    return [dict(row) for row in rows]


def get_person_detail(conn: sqlite3.Connection, person_id: int) -> dict | None:
    """Full record for the Contacts detail view: company, every channel
    identity, every follow-up sequence, and a single timeline merging
    messages from all channels. Cross-channel ordering uses `timestamp`
    (not message_id, unlike list_messages_for_person) since message_id is
    only guaranteed to reflect true chronological order *within* one
    channel's own ingestion — across channels it isn't comparable. Note
    LinkedIn's timestamp is itself an approximation (see
    list_messages_for_person), so a merged LinkedIn/email ordering can be
    slightly off around messages sent close together across channels."""
    row = conn.execute(
        """
        SELECT p.person_id, p.name, p.job_title, p.person_status, p.notes,
               co.company_name,
               fn.funnel_id, fn.name AS funnel_name,
               fs.stage_id AS funnel_stage_id, fs.name AS funnel_stage_name
        FROM people p
        LEFT JOIN companies co ON co.company_id = p.company_id
        LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id
        LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id
        WHERE p.person_id = ?
        """,
        (person_id,),
    ).fetchone()
    if row is None:
        return None

    channels = [
        dict(r)
        for r in conn.execute(
            "SELECT account_id, channel, channel_identifier, display_name, contact_status "
            "FROM channel_accounts WHERE person_id = ? ORDER BY channel",
            (person_id,),
        ).fetchall()
    ]

    messages = [
        dict(r)
        for r in conn.execute(
            "SELECT message_id, channel, sender, direction, message_text, timestamp "
            "FROM messages WHERE person_id = ? ORDER BY timestamp ASC, message_id ASC",
            (person_id,),
        ).fetchall()
    ]

    follow_ups = [
        dict(r)
        for r in conn.execute(
            "SELECT follow_up_id, channel, follow_up_type, stage, status, status_reason, "
            "last_outreach_at, next_follow_up_at FROM follow_ups WHERE person_id = ? "
            "ORDER BY follow_up_id DESC",
            (person_id,),
        ).fetchall()
    ]

    result = dict(row)
    result["channels"] = channels
    result["messages"] = messages
    result["follow_ups"] = follow_ups
    return result


def count_people(conn: sqlite3.Connection, channel: str | None = None) -> int:
    if channel:
        row = conn.execute(
            "SELECT COUNT(DISTINCT person_id) AS n FROM channel_accounts WHERE channel = ?",
            (channel,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM people").fetchone()
    return row["n"]


def count_messages(conn: sqlite3.Connection, channel: str | None = None) -> int:
    if channel:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE channel = ?", (channel,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    return row["n"]


def wipe_channel(conn: sqlite3.Connection, channel: str) -> None:
    """Deletes every message and channel account for one channel, and any
    person left with zero remaining channel accounts afterward (i.e. a
    person who only ever existed because of this channel). Used for
    resetting a single integration's test data without touching others.
    Also clears any follow_ups rows for people about to be deleted —
    foreign keys are enforced, so leaving them would crash the delete."""
    conn.execute("DELETE FROM messages WHERE channel = ?", (channel,))
    conn.execute("DELETE FROM channel_accounts WHERE channel = ?", (channel,))
    conn.execute(
        "DELETE FROM follow_ups WHERE channel = ? AND person_id NOT IN "
        "(SELECT DISTINCT person_id FROM channel_accounts)",
        (channel,),
    )
    conn.execute(
        "DELETE FROM people WHERE person_id NOT IN "
        "(SELECT DISTINCT person_id FROM channel_accounts)"
    )


def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Fully removes one person and everything tied to them — messages,
    channel accounts, the person row itself. Used for the CRM tab's Remove
    action (e.g. a contact added by mistake, or spam approved into the
    Inbox tab by mistake). Does not touch other people or the company they
    may have been linked to. Records each of their channel identities into
    excluded_contacts first, so a future sync never silently re-creates
    them — removal actually sticks instead of being undone by the next
    backfill/auto-sync. Also clears any follow_ups row for this person —
    foreign keys are enforced, so leaving one would crash this delete."""
    accounts = conn.execute(
        "SELECT channel, channel_identifier FROM channel_accounts WHERE person_id = ? AND channel_identifier IS NOT NULL",
        (person_id,),
    ).fetchall()
    now = datetime.now().isoformat()
    for account in accounts:
        conn.execute(
            "INSERT OR REPLACE INTO excluded_contacts (channel, channel_identifier, excluded_at) VALUES (?, ?, ?)",
            (account["channel"], account["channel_identifier"], now),
        )
    conn.execute("DELETE FROM follow_ups WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM messages WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM channel_accounts WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM people WHERE person_id = ?", (person_id,))


def is_contact_excluded(conn: sqlite3.Connection, channel: str, channel_identifier: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM excluded_contacts WHERE channel = ? AND channel_identifier = ?",
        (channel, channel_identifier),
    ).fetchone()
    return row is not None


# --- Dev/test-only ----------------------------------------------------
# Never called from any normal app flow (sync, send, follow-up, merge,
# CRM UI). Exists purely so a contact/funnel/follow-up/merge test can be
# re-run repeatedly, which delete_person() can't support: it tombstones
# every channel identity the person had, permanently (no un-exclude
# exists), so a deleted test contact can never be recreated by a future
# sync. This resets a narrower slice, with no tombstone written.


def dev_reset_contact_for_testing(conn: sqlite3.Connection, person_id: int, channel: str) -> None:
    """Clears the person's funnel/stage, hard-deletes their follow_ups
    (not just marked 'stopped'), and removes only the given channel's
    identity (its messages + channel_accounts row) — no excluded_contacts
    row is written, so a fresh sync can recreate that same channel
    identity for another test pass. Any other channel the person has
    (e.g. real LinkedIn history from a prior merge) is left untouched. If
    this was their only channel, the now-identity-less person row is
    deleted too (nothing references it, and it isn't tombstoned either,
    so it's harmless to drop and cheap to recreate)."""
    conn.execute(
        "UPDATE people SET funnel_id = NULL, funnel_stage_id = NULL WHERE person_id = ?",
        (person_id,),
    )
    conn.execute("DELETE FROM follow_ups WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM messages WHERE person_id = ? AND channel = ?", (person_id, channel))
    conn.execute("DELETE FROM channel_accounts WHERE person_id = ? AND channel = ?", (person_id, channel))
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM channel_accounts WHERE person_id = ?", (person_id,)
    ).fetchone()["n"]
    if remaining == 0:
        conn.execute("DELETE FROM people WHERE person_id = ?", (person_id,))


def new_run_id() -> str:
    """One id per operation (one sync call, one send, one follow-up tick).
    Pass the same id to every log_activity() call within that operation so
    its whole step sequence groups together in list_activity_log()."""
    import uuid

    return uuid.uuid4().hex[:12]


def log_activity(
    run_id: str,
    step_num: int,
    component: str,
    step: str,
    detail: str = "",
    data: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Writes one row to the diagnostic trail — one row per named step of
    the documented sequence (see integrations/linkedin/sync.py's module
    docstring for LinkedIn sync's steps), not a final ok/error verdict.

    Pass `conn` when the caller already has an open connection to this same
    database (e.g. mid-transaction inside _ingest_conversation) — reusing it
    avoids a second writer ever contending for the SQLite write lock against
    the caller's own open transaction, which is a real "database is locked"
    risk otherwise, not just a hypothetical one (caught by this exact
    diagnostic system: see activity_log run history around the WAL/
    busy_timeout fix in get_connection()). When no conn is given, opens and
    commits its own short-lived one (safe for standalone use, e.g. logging
    outside of any other transaction)."""
    import json as _json

    row = (
        run_id,
        step_num,
        datetime.now().isoformat(),
        component,
        step,
        detail[:2000] if detail else None,
        _json.dumps(data)[:4000] if data is not None else None,
    )
    sql = "INSERT INTO activity_log (run_id, step_num, created_at, component, step, detail, data) VALUES (?, ?, ?, ?, ?, ?, ?)"

    if conn is not None:
        conn.execute(sql, row)
        return

    own_conn = get_connection()
    try:
        own_conn.execute(sql, row)
        own_conn.commit()
    finally:
        own_conn.close()


def list_activity_log(
    conn: sqlite3.Connection,
    component: str | None = None,
    runs: int = 10,
) -> list[dict]:
    """Returns the most recent `runs` operations in FULL — every step of
    each, in order — not a flat row limit that could cut a run off
    mid-sequence. Each row's run_id groups it with the rest of its
    operation; step_num gives you the documented step order within it."""
    run_sql = "SELECT DISTINCT run_id, MAX(log_id) AS latest_log_id FROM activity_log"
    params: list = []
    if component:
        run_sql += " WHERE component = ?"
        params.append(component)
    run_sql += " GROUP BY run_id ORDER BY latest_log_id DESC LIMIT ?"
    params.append(runs)
    run_ids = [row["run_id"] for row in conn.execute(run_sql, params).fetchall()]
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT * FROM activity_log WHERE run_id IN ({placeholders}) ORDER BY log_id DESC, step_num ASC",
        run_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def merge_people(conn: sqlite3.Connection, keep_person_id: int, merge_person_id: int) -> None:
    """Manual merge action (per the explicit decision not to auto-match
    across channels): moves every channel account, message, and follow-up
    sequence from merge_person_id onto keep_person_id, then deletes the
    now-empty person. Moved channel_accounts also get their display_name
    updated to the kept person's name — otherwise a channel identity
    carries over with the merged-away contact's stale name attached even
    though it now belongs to someone else. Follow-ups must be moved (not
    left behind) — foreign keys are enforced, so an orphaned one would
    crash the delete below. If the kept person has no company on file but
    the merged-away one did, that company carries over instead of
    silently being dropped."""
    keep_name = conn.execute(
        "SELECT name FROM people WHERE person_id = ?", (keep_person_id,)
    ).fetchone()["name"]
    conn.execute(
        "UPDATE channel_accounts SET person_id = ?, display_name = ? WHERE person_id = ?",
        (keep_person_id, keep_name, merge_person_id),
    )
    conn.execute(
        "UPDATE messages SET person_id = ? WHERE person_id = ?",
        (keep_person_id, merge_person_id),
    )
    conn.execute(
        "UPDATE follow_ups SET person_id = ? WHERE person_id = ?",
        (keep_person_id, merge_person_id),
    )
    conn.execute(
        """
        UPDATE people SET company_id = (
            SELECT company_id FROM people WHERE person_id = :merge_id
        )
        WHERE person_id = :keep_id AND company_id IS NULL
        """,
        {"merge_id": merge_person_id, "keep_id": keep_person_id},
    )
    conn.execute("DELETE FROM people WHERE person_id = ?", (merge_person_id,))


def get_active_follow_up(conn: sqlite3.Connection, person_id: int, channel: str) -> sqlite3.Row | None:
    """The one non-terminal sequence for this person+channel, if any —
    status 'active' or 'awaiting_approval'. There's at most one at a time."""
    return conn.execute(
        "SELECT * FROM follow_ups WHERE person_id = ? AND channel = ? "
        "AND status IN ('active', 'awaiting_approval') "
        "ORDER BY follow_up_id DESC LIMIT 1",
        (person_id, channel),
    ).fetchone()


def start_follow_up(
    conn: sqlite3.Connection,
    person_id: int,
    channel: str,
    conversation_ref: str | None,
    next_follow_up_at: str,
    follow_up_type: str = "new_lead",
) -> int:
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO follow_ups "
        "(person_id, channel, conversation_ref, follow_up_type, stage, status, "
        " last_outreach_at, next_follow_up_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 'active', ?, ?, ?, ?)",
        (person_id, channel, conversation_ref, follow_up_type, now, next_follow_up_at, now, now),
    )
    return cur.lastrowid


def advance_follow_up(conn: sqlite3.Connection, follow_up_id: int, next_follow_up_at: str) -> None:
    """Called after a scheduled follow-up send succeeds: bump the stage,
    record the send, go back to 'active' and wait for the next interval."""
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE follow_ups SET stage = stage + 1, status = 'active', "
        "last_outreach_at = ?, next_follow_up_at = ?, updated_at = ? WHERE follow_up_id = ?",
        (now, next_follow_up_at, now, follow_up_id),
    )


def set_follow_up_awaiting_approval(conn: sqlite3.Connection, follow_up_id: int) -> None:
    """A draft has been generated and is waiting on the user — stops the
    scheduler from generating another one on the next tick."""
    conn.execute(
        "UPDATE follow_ups SET status = 'awaiting_approval', updated_at = ? WHERE follow_up_id = ?",
        (datetime.now().isoformat(), follow_up_id),
    )


def end_follow_up(conn: sqlite3.Connection, follow_up_id: int, status: str, reason: str) -> None:
    """Terminal state: 'cancelled' (real reply), 'stopped' (asked to stop),
    or 'completed' (ran out of stages)."""
    conn.execute(
        "UPDATE follow_ups SET status = ?, status_reason = ?, updated_at = ? WHERE follow_up_id = ?",
        (status, reason, datetime.now().isoformat(), follow_up_id),
    )


def list_due_follow_ups(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.*, p.name AS person_name FROM follow_ups f "
        "JOIN people p ON p.person_id = f.person_id "
        "WHERE f.status = 'active' AND f.next_follow_up_at <= ?",
        (now_iso,),
    ).fetchall()


def list_all_follow_ups(conn: sqlite3.Connection) -> list[dict]:
    """Every sequence still needing attention, for the CRM tab and the
    Home/chat notification surfaces — not just due ones. needs_review is
    technically terminal for the sequence itself, but stays visible here
    since it means a real reply is sitting there unanswered by a human."""
    rows = conn.execute(
        "SELECT f.*, p.name AS person_name FROM follow_ups f "
        "JOIN people p ON p.person_id = f.person_id "
        "WHERE f.status IN ('active', 'awaiting_approval', 'needs_review') "
        "ORDER BY f.next_follow_up_at ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def list_recent_activity(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Home's cross-contact triage widget: the `limit` most recently
    active contacts, each with their single most recent message. This is
    "which contacts were most recently touched," not a message feed — a
    person with 5 messages in the last hour still contributes exactly one
    row here. People with no messages at all are excluded (nothing to
    show as "activity" for them). Ordered by `timestamp`, not message_id —
    this spans people and channels, and message_id only orders reliably
    within one channel's own ingestion (see list_messages_for_person)."""
    rows = conn.execute(
        """
        SELECT
            p.person_id, p.name, co.company_name,
            fn.name AS funnel_name, fs.name AS funnel_stage_name,
            m.channel AS last_channel, m.direction AS last_message_direction,
            m.message_text AS last_message_text, m.timestamp AS last_message_at
        FROM people p
        LEFT JOIN companies co ON co.company_id = p.company_id
        LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id
        LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id
        JOIN messages m ON m.message_id = (
            SELECT message_id FROM messages WHERE person_id = p.person_id
            ORDER BY message_id DESC LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM channel_accounts ca
            WHERE ca.person_id = p.person_id AND ca.contact_status = 'trusted'
        )
        ORDER BY m.timestamp DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_overview(conn: sqlite3.Connection) -> dict:
    """Everything the CRM tab needs in one call: counts across all four
    tables, every company with its people count, and every person with
    their company, channels, and most recent message — so a raw table dump
    never has to be shown to explain what a channel_account/message row
    even means without its person context."""
    counts = {
        "companies": conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"],
        "people": conn.execute("SELECT COUNT(*) AS n FROM people").fetchone()["n"],
        "channel_accounts": conn.execute("SELECT COUNT(*) AS n FROM channel_accounts").fetchone()["n"],
        "messages": conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"],
    }

    companies = [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.company_id, c.company_name, c.company_status,
                   COUNT(p.person_id) AS people_count
            FROM companies c
            LEFT JOIN people p ON p.company_id = c.company_id
            GROUP BY c.company_id
            ORDER BY c.company_name COLLATE NOCASE
            """
        ).fetchall()
    ]

    people_rows = conn.execute(
        """
        SELECT
            p.person_id, p.name, p.job_title, p.person_status,
            co.company_name,
            fn.funnel_id, fn.name AS funnel_name,
            fs.stage_id AS funnel_stage_id, fs.name AS funnel_stage_name,
            m.channel AS last_channel,
            m.message_text AS last_message_text,
            m.timestamp AS last_message_at
        FROM people p
        LEFT JOIN companies co ON co.company_id = p.company_id
        LEFT JOIN funnels fn ON fn.funnel_id = p.funnel_id
        LEFT JOIN funnel_stages fs ON fs.stage_id = p.funnel_stage_id
        LEFT JOIN messages m ON m.message_id = (
            SELECT message_id FROM messages WHERE person_id = p.person_id
            ORDER BY message_id DESC LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM channel_accounts ca
            WHERE ca.person_id = p.person_id AND ca.contact_status = 'trusted'
        )
        ORDER BY m.message_id DESC
        """
    ).fetchall()

    people = []
    for row in people_rows:
        channels = [
            r["channel"]
            for r in conn.execute(
                "SELECT DISTINCT channel FROM channel_accounts WHERE person_id = ?",
                (row["person_id"],),
            ).fetchall()
        ]
        follow_up = conn.execute(
            "SELECT stage, status, next_follow_up_at FROM follow_ups "
            "WHERE person_id = ? AND status IN ('active', 'awaiting_approval', 'needs_review') "
            "ORDER BY follow_up_id DESC LIMIT 1",
            (row["person_id"],),
        ).fetchone()
        person = dict(row)
        person["channels"] = channels
        person["follow_up"] = dict(follow_up) if follow_up else None
        people.append(person)

    return {"counts": counts, "companies": companies, "people": people}
