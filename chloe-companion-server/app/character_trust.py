"""Per-character trust posture (2026-10-16): each girl's own relationship clock.

trust.py holds the GLOBAL per-user trust state (backward compatible; the
admin panel and older paths still read it). This module is the
per-character evolution used by the chat path: get_state / maybe_advance
keyed by (user_id, character_id), so Mara's milestones never inherit
Chloe's -- each relationship deepens on its own clock.

Design (unchanged from trust.py):
- Every (user, character) starts at M5 ("Trusted", ~58/100): she treats him
  as already someone she likes from the very first message.
- The upper climb M5->M6->M7->M8 only gates how much of HERSELF she
  volunteers (pinned self-facts revealed per stage). Warmth is never gated.
- STAGE_DAYS are the minimum REAL days at each stage before deepening.

Personality evolution: when maybe_advance deepens a stage, a beat is
written to her typed memories ("tone") so she carries the history -- not
just the milestone number. The beat surfaces through the normal memory
recall (relevant_memories, callbacks), so she references their growth
naturally.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import store, trust


def _cid(character_id: str | None) -> str:
    """Normalize a character id for trust keys (matches characters.py)."""
    return (character_id or "chloe").strip().lower() or "chloe"


def get_state(conn: sqlite3.Connection, user_id: str,
              character_id: str = "chloe") -> dict[str, Any]:
    """This (user, character)'s trust state, creating it at M5 on first use.

    Requires the per-character trust_state schema (trust_migration.migrate,
    run first in db.init_db).
    """
    cid = _cid(character_id)
    store.get_or_create_user(conn, user_id)  # trust_state FK references users
    row = conn.execute(
        "SELECT milestone, stage_entered_at, updated_at FROM trust_state "
        "WHERE user_id = ? AND character_id = ?",
        (user_id, cid),
    ).fetchone()
    if row:
        return {"user_id": user_id, "character_id": cid, **dict(row)}
    conn.execute(
        "INSERT INTO trust_state (user_id, character_id, milestone) "
        "VALUES (?, ?, ?)",
        (user_id, cid, trust.START_MILESTONE),
    )
    conn.commit()
    row = conn.execute(
        "SELECT milestone, stage_entered_at, updated_at FROM trust_state "
        "WHERE user_id = ? AND character_id = ?",
        (user_id, cid),
    ).fetchone()
    return {"user_id": user_id, "character_id": cid, **dict(row)}


def _parse_ts(ts: str) -> datetime:
    # SQLite datetime('now') -> 'YYYY-MM-DD HH:MM:SS' (UTC, naive).
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def maybe_advance(conn: sqlite3.Connection, user_id: str,
                  character_id: str = "chloe",
                  now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Check the stage clock for this (user, character); advance AT MOST one
    milestone per call. Returns (state, advanced).

    On advance, writes an evolution beat to her typed memories so the
    deepening becomes part of what she remembers -- not just a number.
    Never gates warmth.
    """
    cid = _cid(character_id)
    state = get_state(conn, user_id, cid)
    milestone = state["milestone"]
    if milestone >= trust.MAX_MILESTONE:
        return state, False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    entered = _parse_ts(state["stage_entered_at"])
    elapsed_days = (now - entered).total_seconds() / 86400.0
    required = trust.STAGE_DAYS[milestone - trust.START_MILESTONE]
    if elapsed_days < required:
        return state, False
    conn.execute(
        """UPDATE trust_state SET milestone = ?, stage_entered_at = datetime('now'),
                              updated_at = datetime('now')
           WHERE user_id = ? AND character_id = ?""",
        (milestone + 1, user_id, cid),
    )
    conn.commit()
    new_state = get_state(conn, user_id, cid)
    _write_advance_beat(conn, user_id, cid, new_state["milestone"])
    return new_state, True


def _write_advance_beat(conn: sqlite3.Connection, user_id: str,
                        character_id: str, new_milestone: int) -> None:
    """Personality evolution: when the relationship deepens a stage, write a
    durable beat so she remembers they grew -- not just the milestone.
    Deduped: one beat per milestone. Never raises (a beat must not break chat).
    """
    try:
        label = trust.stage_label(new_milestone)
        existing = store.recall_memories(
            conn, user_id, mem_type="tone", keyword=f"M{new_milestone}",
            character_id=character_id, limit=5,
        )
        if any(f"M{new_milestone}" in m["text"] for m in existing):
            return
        text = (
            f"We've grown closer -- our relationship deepened to {label}. "
            f"He keeps coming back, and that means something to me."
        )
        store.write_memory(conn, user_id, "tone", text, character_id)
    except Exception:  # noqa: BLE001 -- a beat must never break chat
        pass


def reset_for_character(conn: sqlite3.Connection, user_id: str,
                        character_id: str) -> None:
    """Wipe this (user, character)'s trust state (admin reset: a clean first
    meeting). Other characters' clocks are untouched."""
    cid = _cid(character_id)
    conn.execute(
        "DELETE FROM trust_state WHERE user_id = ? AND character_id = ?",
        (user_id, cid),
    )
    conn.commit()
