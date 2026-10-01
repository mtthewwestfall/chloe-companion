"""SQLite schema + connection helpers.

Isolation rule (enforced by construction): EVERY row carries user_id, and
EVERY read/write goes through a store function that takes user_id as a
required argument. There is no code path that reads memories or banks
without a user_id filter — see tests/test_memory_isolation.py.

Character namespacing (2026-09-29): memory, conversation, and digest rows
also carry character_id (default 'chloe'), so each character remembers him
separately. Banks and trust state stay per-user (shared across characters).
"""
from __future__ import annotations

import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id              TEXT PRIMARY KEY,
    name                 TEXT,
    email                TEXT,  -- NULL for pre-auth rows (2026-09-30)
    password_hash        TEXT,
    email_verified_at    TEXT,
    tier                 TEXT NOT NULL DEFAULT 'basic',
    messages_remaining   INTEGER NOT NULL DEFAULT 300,
    voice_notes_remaining INTEGER NOT NULL DEFAULT 0,
    proactive_enabled    INTEGER NOT NULL DEFAULT 0,
    timezone             TEXT NOT NULL DEFAULT 'America/New_York',
    proactive_day        TEXT NOT NULL DEFAULT '',
    proactive_sent_today INTEGER NOT NULL DEFAULT 0,
    messages_used_total  INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    character_id TEXT NOT NULL DEFAULT 'chloe',
    mem_type   TEXT NOT NULL,   -- core | fact | preference | boundary | tone
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user_char ON memories(user_id, character_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    character_id TEXT NOT NULL DEFAULT 'chloe',
    role       TEXT NOT NULL,   -- user | <character_id>
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversation(user_id);
CREATE INDEX IF NOT EXISTS idx_conversation_user_char ON conversation(user_id, character_id);

-- Purchase fulfillment (2026-09-29): Stripe webhook idempotency + identity.
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stripe_customers (
    stripe_customer_id     TEXT PRIMARY KEY,
    stripe_subscription_id TEXT NOT NULL DEFAULT '',
    user_id                TEXT NOT NULL,
    tier                   TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stripe_customers_sub ON stripe_customers(stripe_subscription_id);
CREATE TABLE IF NOT EXISTS trial_grants (
    user_id    TEXT PRIMARY KEY,
    granted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- JVZoo affiliate channel (2026-09-30): JVZIPN idempotency + subscription
-- identity. Email is the identity key — JVZoo buyers check out on jvzoo.com,
-- so there is no client_reference_id. A $0 trial SALE only records the
-- subscription; BILL (paid rebill) fulfills the tier; RFND/rebill-cancel
-- downgrades to basic. Tiers only — no Keyhole show grants on this path.
CREATE TABLE IF NOT EXISTS jvzoo_events (
    paykey     TEXT PRIMARY KEY,
    event_type TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS jvzoo_subscriptions (
    paykey     TEXT PRIMARY KEY,
    email      TEXT NOT NULL DEFAULT '',
    user_id    TEXT NOT NULL DEFAULT '',
    tier       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'trial',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jvzoo_subs_email ON jvzoo_subscriptions(email);

-- Free verified-signup grant (2026-09-30): one row per user that received
-- the first-FREE_SIGNUP_CAP free 50-message grant.
CREATE TABLE IF NOT EXISTS free_grants (
    user_id    TEXT PRIMARY KEY,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Trust posture: per-user relationship stage. Starts at M5 ("Trusted") on
-- first use; only gates how much of herself she volunteers, never warmth.
CREATE TABLE IF NOT EXISTS local_characters (
    id               TEXT PRIMARY KEY,   -- slug, lowercase, e.g. 'nova'
    name             TEXT NOT NULL,
    avatar_url       TEXT NOT NULL DEFAULT '',
    persona          TEXT NOT NULL DEFAULT '',
    companion_voice  TEXT NOT NULL DEFAULT 'liora',
    blurb            TEXT NOT NULL DEFAULT '',
    door_title       TEXT NOT NULL DEFAULT '',
    sort_order       INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-girl footage labels (2026-09-30): admin-labeled video clips for
-- companion-local girls. Labels: typing-main | main | give | activity.
-- The companion app only ever plays typing-main / main / activity;
-- 'give' is Keyhole-only and is never served to the companion app.
CREATE TABLE IF NOT EXISTS girl_footage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    girl_id    TEXT NOT NULL,
    label      TEXT NOT NULL,   -- typing-main | main | give | activity
    url        TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
    -- NOTE: no FOREIGN KEY on girl_id — it may hold the reserved builtin id
    -- 'chloe', which has no local_characters row. Deletes cascade manually in
    -- store.delete_local_character.
);
CREATE INDEX IF NOT EXISTS idx_girl_footage_girl ON girl_footage(girl_id);

CREATE TABLE IF NOT EXISTS trust_state (
    user_id         TEXT PRIMARY KEY,
    milestone       INTEGER NOT NULL DEFAULT 5,
    stage_entered_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Email auth (2026-09-30): login is required, no anonymous use. Sessions
-- are opaque tokens stored server-side; verification tokens are single-use.
CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS email_verifications (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Layer 2 of the memory stack: one short rolling digest per user AND
-- character, refreshed by the slower brain path after each turn. Injected
-- into chat context.
CREATE TABLE IF NOT EXISTS memory_digest (
    user_id    TEXT NOT NULL,
    character_id TEXT NOT NULL DEFAULT 'chloe',
    summary    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, character_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Custom media requests ("request-a-pic", 2026-10-01).
-- media_requests: one row per ask. kind = regular (user prompt -> Sogni
-- on-demand generation, async) | give (pre-approved library, instant, $0).
-- media = pic | video. pool_source remembers which bucket the request drew
-- from (included | overage_regular | overage_give) so a failed generation
-- refunds to the right place.
CREATE TABLE IF NOT EXISTS media_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    girl_id    TEXT NOT NULL DEFAULT 'chloe',
    kind       TEXT NOT NULL,   -- regular | give
    media      TEXT NOT NULL,   -- pic | video
    prompt     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | ready | failed | rejected
    result_url TEXT NOT NULL DEFAULT '',
    cost_usd   REAL NOT NULL DEFAULT 0,
    pool_source TEXT NOT NULL DEFAULT 'included',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_requests_user ON media_requests(user_id);

-- Per-user request pool. included_remaining refills to the tier allocation
-- monthly (no stacking); overage_* balances only grow via the $5.99
-- overage pack (and the free-signup give grant) and persist across months.
CREATE TABLE IF NOT EXISTS request_pools (
    user_id                TEXT PRIMARY KEY,
    period                 TEXT NOT NULL DEFAULT '',  -- YYYY-MM of included pool
    included_remaining     INTEGER NOT NULL DEFAULT 0,
    overage_regular_videos INTEGER NOT NULL DEFAULT 0,
    overage_give_videos    INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Per-user give history: which pre-approved give clips each user has
-- already received, per girl. Drives the no-repeat rotation.
CREATE TABLE IF NOT EXISTS user_give_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    girl_id    TEXT NOT NULL,
    footage_id INTEGER NOT NULL,
    sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_give_history_user_girl ON user_give_history(user_id, girl_id);
"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        r["name"] == column
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _migrate_character_columns(conn: sqlite3.Connection) -> None:
    """2026-09-29: namespace memory per character. Existing installs get
    character_id added with DEFAULT 'chloe' (existing rows belong to Chloe).
    memory_digest's PK becomes (user_id, character_id), so the table is
    rebuilt; existing digests carry over as Chloe's.

    Runs BEFORE the SCHEMA script (whose new indexes reference
    character_id); on fresh installs the tables don't exist yet and the
    schema script creates them with the column directly.
    """
    for table in ("memories", "conversation"):
        if _table_exists(conn, table) and not _column_exists(conn, table, "character_id"):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN character_id "
                "TEXT NOT NULL DEFAULT 'chloe'"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_user_char "
                f"ON {table}(user_id, character_id)"
            )
    if _table_exists(conn, "memory_digest") and not _column_exists(
        conn, "memory_digest", "character_id"
    ):
        conn.execute("ALTER TABLE memory_digest RENAME TO memory_digest_old")
        conn.execute(
            """CREATE TABLE memory_digest (
                user_id    TEXT NOT NULL,
                character_id TEXT NOT NULL DEFAULT 'chloe',
                summary    TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, character_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """INSERT INTO memory_digest (user_id, character_id, summary, updated_at)
               SELECT user_id, 'chloe', summary, updated_at FROM memory_digest_old"""
        )
        conn.execute("DROP TABLE memory_digest_old")
    conn.commit()


def _migrate_auth_columns(conn: sqlite3.Connection) -> None:
    """2026-09-30: email login required (no anonymous use). Existing installs
    get email/password_hash/email_verified_at on users; pre-existing rows
    keep NULL email (they can never log in — no paying users existed).
    Runs BEFORE the SCHEMA script.
    """
    if _table_exists(conn, "users"):
        for column, ddl in (
            ("email", "TEXT"),
            ("password_hash", "TEXT"),
            ("email_verified_at", "TEXT"),
        ):
            if not _column_exists(conn, "users", column):
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)"
        )
    conn.commit()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn



def _migrate_girl_footage_no_fk(conn: sqlite3.Connection) -> None:
    """One-time rebuild of girl_footage without the local_characters FK."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'girl_footage'"
    ).fetchone()
    if not row or "REFERENCES" not in (row[0] or "").upper():
        return
    conn.execute(
        """CREATE TABLE girl_footage_new (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            girl_id    TEXT NOT NULL,
            label      TEXT NOT NULL,
            url        TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        "INSERT INTO girl_footage_new (id, girl_id, label, url, sort_order, created_at) "
        "SELECT id, girl_id, label, url, sort_order, created_at FROM girl_footage"
    )
    conn.execute("DROP TABLE girl_footage")
    conn.execute("ALTER TABLE girl_footage_new RENAME TO girl_footage")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_girl_footage_girl ON girl_footage(girl_id)"
    )
    conn.commit()


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        # Migration first: on existing installs it adds character_id before
        # the schema script's new indexes reference the column. On fresh
        # installs the tables don't exist yet and the schema script creates
        # them with the column directly.
        _migrate_character_columns(conn)
        _migrate_auth_columns(conn)
        conn.executescript(SCHEMA)
        _migrate_girl_footage_no_fk(conn)
        conn.commit()
    finally:
        conn.close()
