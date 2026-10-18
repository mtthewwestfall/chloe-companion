"""Conversion fix plan (2026-10-18) — Item C: record the moments.

Before this module, the funnel's decisive moments were recorded
NOWHERE: the backend returned 402s for usage walls without logging
them, Plans views and checkout starts left no trace, and only
completed-payment webhooks existed. This is the lightweight ledger:
one table, fail-open writes (a recording failure must never break a
request), and one admin read that returns daily counts per kind —
including checkout_abandoned, computed at read time (a checkout_started
with no checkout_completed for the same user within 24h).

Kinds:
  wall_hit            meta {wall: <cap kind>} — every 402/cap response
  plans_view          the pricing/Plans surface was opened (web)
  checkout_started    meta {plan: <key>} — a Stripe checkout was opened
  checkout_completed  a completed-payment webhook credited a user
  checkout_abandoned  computed — started, nothing completed in 24h
  trial_offer_shown   Item D: the $0.99 trial was offered in character
  trial_offer_tapped  Item D: he tapped through from the offer
  milestone_<n>       Item E6: lifetime message count crossed n
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import config

CLIENT_KINDS = ("plans_view", "checkout_started", "trial_offer_tapped")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversion_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL DEFAULT '',
            kind       TEXT NOT NULL,
            meta       TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_events_kind_day "
        "ON conversion_events(kind, created_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_events_user "
        "ON conversion_events(user_id, kind)")


def record_event(conn: sqlite3.Connection, user_id: str | None,
                 kind: str, meta: dict[str, Any] | None = None) -> None:
    """Append one event. Fail-open by design: recording must never
    break the request that produced the moment."""
    if not config.CONV_EVENTS:
        return
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO conversion_events (user_id, kind, meta) "
            "VALUES (?, ?, ?)",
            (user_id or "", kind, json.dumps(meta or {})))
        conn.commit()
    except Exception:
        pass


def _abandoned_by_day(conn: sqlite3.Connection,
                      since: str) -> dict[str, int]:
    """checkout_started events at least 24h old with no
    checkout_completed for the same user within 24h after the start,
    bucketed by the start's day."""
    now = datetime.utcnow()
    starts = conn.execute(
        "SELECT user_id, created_at FROM conversion_events "
        "WHERE kind = 'checkout_started' AND created_at >= ?",
        (since,)).fetchall()
    completed = conn.execute(
        "SELECT user_id, created_at FROM conversion_events "
        "WHERE kind = 'checkout_completed'").fetchall()
    done_by_user: dict[str, list[datetime]] = {}
    for r in completed:
        try:
            done_by_user.setdefault(r["user_id"], []).append(
                datetime.fromisoformat(r["created_at"]))
        except Exception:
            continue
    out: dict[str, int] = {}
    for r in starts:
        try:
            started = datetime.fromisoformat(r["created_at"])
        except Exception:
            continue
        if started + timedelta(hours=24) > now:
            continue  # still inside the 24h completion window
        if any(started <= d <= started + timedelta(hours=24)
               for d in done_by_user.get(r["user_id"], [])):
            continue
        day = r["created_at"][:10]
        out[day] = out.get(day, 0) + 1
    return out


def daily_counts(conn: sqlite3.Connection,
                 days: int = 14) -> dict[str, Any]:
    """Daily counts per event kind for the last `days` days, oldest
    first: {"days": [{"date": "YYYY-MM-DD", "counts": {kind: n}}]}."""
    _ensure_table(conn)
    since = (datetime.utcnow() - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, kind, COUNT(*) AS n "
        "FROM conversion_events WHERE created_at >= ? "
        "GROUP BY day, kind ORDER BY day", (since,)).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[r["kind"]] = r["n"]
    for day, n in _abandoned_by_day(conn, since).items():
        by_day.setdefault(day, {})["checkout_abandoned"] = n
    return {"days": [{"date": day, "counts": by_day[day]}
                     for day in sorted(by_day)]}
