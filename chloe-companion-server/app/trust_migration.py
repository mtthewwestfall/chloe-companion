"""Trust posture schema migration: per-character relationship clocks (2026-10-16).

Each girl gets her own trust state -- Mara no longer inherits Chloe's
milestones, and a stranger-origin character starts at M5 no matter how deep
things are with Chloe.

This module is the AUTHORITATIVE schema for trust_state. The SCHEMA script
in db.py retains the legacy per-user definition, but it never executes:
this migration runs first in init_db, so by the time the schema script
runs, the table already exists and CREATE TABLE IF NOT EXISTS is a no-op.

- Fresh installs: creates trust_state with (user_id, character_id) PK.
- Existing installs: rebuilds the legacy per-user table, re-homing each
  user's single global row to 'chloe' (her history is preserved; every
  other character starts at M5, the designed starting point).
- Already migrated: no-op (character_id column present).

Idempotent and safe to run on every boot.
"""
from __future__ import annotations

import sqlite3

# Authoritative trust_state DDL: one row per (user, character).
_TRUST_STATE_DDL = """CREATE TABLE trust_state (
    user_id         TEXT NOT NULL,
    character_id    TEXT NOT NULL DEFAULT 'chloe',
    milestone       INTEGER NOT NULL DEFAULT 5,
    stage_entered_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, character_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
)"""


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


def migrate(conn: sqlite3.Connection) -> None:
    """Ensure trust_state is keyed by (user_id, character_id).

    Runs first in db.init_db, before the SCHEMA script. Safe on every boot.
    """
    if not _table_exists(conn, "trust_state"):
        # Fresh install: create the per-character table directly.
        conn.execute(_TRUST_STATE_DDL)
        conn.commit()
        return
    if _column_exists(conn, "trust_state", "character_id"):
        return  # Already migrated.
    # Existing install: rebuild, re-homing the global row to 'chloe'.
    conn.execute("ALTER TABLE trust_state RENAME TO trust_state_old")
    conn.execute(_TRUST_STATE_DDL)
    conn.execute(
        """INSERT INTO trust_state (user_id, character_id, milestone,
                                    stage_entered_at, updated_at)
           SELECT user_id, 'chloe', milestone, stage_entered_at, updated_at
           FROM trust_state_old"""
    )
    conn.execute("DROP TABLE trust_state_old")
    conn.commit()
