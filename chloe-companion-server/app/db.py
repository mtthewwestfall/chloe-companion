"""SQLite schema + connection helpers.

Isolation rule (enforced by construction): EVERY row carries user_id, and
EVERY read/write goes through a store function that takes user_id as a
required argument. There is no code path that reads memories or banks
without a user_id filter — see tests/test_memory_isolation.py.
"""
from __future__ import annotations

import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id              TEXT PRIMARY KEY,
    name                 TEXT,
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
    mem_type   TEXT NOT NULL,   -- core | fact | preference | boundary | tone
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);

CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,   -- user | chloe
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversation(user_id);

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

-- Trust posture: per-user relationship stage. Starts at M5 ("Trusted") on
-- first use; only gates how much of herself she volunteers, never warmth.
CREATE TABLE IF NOT EXISTS trust_state (
    user_id         TEXT PRIMARY KEY,
    milestone       INTEGER NOT NULL DEFAULT 5,
    stage_entered_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Layer 2 of the memory stack: one short rolling digest per user, refreshed
-- by the slower brain path after each turn. Injected into chat context.
CREATE TABLE IF NOT EXISTS memory_digest (
    user_id    TEXT PRIMARY KEY,
    summary    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
