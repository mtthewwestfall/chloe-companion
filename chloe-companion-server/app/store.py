"""Per-user store: memory (typed, Thoughtspace pattern) + message banks.

Isolation: user_id is a required parameter on every public function and is
always bound into the WHERE clause. Cross-user leakage is impossible by
construction as long as callers use this module (tested).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import config, db

MEM_TYPES = ("core", "fact", "preference", "boundary", "tone")


# ---------------------------------------------------------------------------
# Users / banks
# ---------------------------------------------------------------------------
def get_or_create_user(conn: sqlite3.Connection, user_id: str, name: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    tier = config.TIERS["basic"]
    conn.execute(
        """INSERT INTO users (user_id, name, tier, messages_remaining,
                              voice_notes_remaining, proactive_enabled)
           VALUES (?, ?, 'basic', ?, ?, 0)""",
        (user_id, name, tier["messages_per_month"], tier["voice_notes_per_month"]),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def set_tier(conn: sqlite3.Connection, user_id: str, tier: str) -> dict[str, Any]:
    """Set tier and TOP UP banks to the tier allocation (no stacking)."""
    if tier not in config.TIERS:
        raise ValueError(f"unknown tier: {tier}")
    t = config.TIERS[tier]
    get_or_create_user(conn, user_id)  # ensure row exists
    conn.execute(
        """UPDATE users SET tier = ?, messages_remaining = ?,
                              voice_notes_remaining = ?,
                              proactive_enabled = ?,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (tier, t["messages_per_month"], t["voice_notes_per_month"],
         1 if t["proactive"] else 0, user_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def consume_message(conn: sqlite3.Connection, user_id: str) -> bool:
    """Decrement the message bank by one. Returns False (no crash, no
    negative balance) when the bank is empty."""
    row = conn.execute(
        "SELECT messages_remaining FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return False
    if row["messages_remaining"] <= 0:
        return False
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining - 1,
                              messages_used_total = messages_used_total + 1,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (user_id,),
    )
    conn.commit()
    return True


def consume_voice_note(conn: sqlite3.Connection, user_id: str) -> bool:
    row = conn.execute(
        "SELECT voice_notes_remaining FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or row["voice_notes_remaining"] <= 0:
        return False
    conn.execute(
        """UPDATE users SET voice_notes_remaining = voice_notes_remaining - 1,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (user_id,),
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Memory — typed per Thoughtspace pattern
# ---------------------------------------------------------------------------
def write_memory(conn: sqlite3.Connection, user_id: str, mem_type: str, text: str) -> int:
    if mem_type not in MEM_TYPES:
        raise ValueError(f"mem_type must be one of {MEM_TYPES}")
    get_or_create_user(conn, user_id)  # ensure row exists (memory belongs to a user)
    cur = conn.execute(
        "INSERT INTO memories (user_id, mem_type, text) VALUES (?, ?, ?)",
        (user_id, mem_type, text),
    )
    conn.commit()
    return cur.lastrowid


def recall_memories(
    conn: sqlite3.Connection,
    user_id: str,
    mem_type: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Recall memories for ONE user. Never returns another user's rows."""
    sql = "SELECT id, mem_type, text, created_at FROM memories WHERE user_id = ?"
    params: list[Any] = [user_id]
    if mem_type:
        sql += " AND mem_type = ?"
        params.append(mem_type)
    if keyword:
        sql += " AND text LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def export_memories(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    return recall_memories(conn, user_id, limit=100000)


def delete_all_memories(conn: sqlite3.Connection, user_id: str) -> int:
    cur = conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM conversation WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Conversation log (per user, for recall of "last conversation")
# ---------------------------------------------------------------------------
def log_turn(conn: sqlite3.Connection, user_id: str, role: str, text: str) -> None:
    conn.execute(
        "INSERT INTO conversation (user_id, role, text) VALUES (?, ?, ?)",
        (user_id, role, text),
    )
    conn.commit()


def recent_turns(conn: sqlite3.Connection, user_id: str, limit: int = 12) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT role, text FROM conversation WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    ][::-1]
