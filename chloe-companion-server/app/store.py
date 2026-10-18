"""Per-user store: memory (typed, Thoughtspace pattern) + message banks.

Isolation: user_id is a required parameter on every public function and is
always bound into the WHERE clause. Cross-user leakage is impossible by
construction as long as callers use this module (tested).
"""
from __future__ import annotations

import json

import random
import re
import sqlite3
import time
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
    # Custom media requests (2026-10-01): refill the included request pool
    # to the new tier's allocation, same no-stacking rule as the banks.
    refill_request_pool(conn, user_id)
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


def note_message_sent(conn: sqlite3.Connection, user_id: str) -> None:
    """Advance only the lifetime message counter (owner accounts).

    Owner accounts (ASSIST_OWNER_IDS) chat without a bank — see the owner
    bypass in the chat endpoint. messages_used_total still advances so
    cadence features keyed to it (photo drops) behave exactly as if the
    message had been banked."""
    conn.execute(
        """UPDATE users SET messages_used_total = messages_used_total + 1,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (user_id,),
    )
    conn.commit()


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
# Purchase fulfillment — Stripe webhook support (2026-09-29)
# ---------------------------------------------------------------------------
def note_stripe_event(conn: sqlite3.Connection, event_id: str, event_type: str = "") -> bool:
    """Record a Stripe event id. Returns True when first seen, False on
    replay (idempotency: Stripe may redeliver events)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO stripe_events (event_id, event_type) VALUES (?, ?)",
        (event_id, event_type),
    )
    conn.commit()
    return cur.rowcount == 1


def link_stripe_customer(conn: sqlite3.Connection, customer_id: str,
                         subscription_id: str, user_id: str, tier: str) -> None:
    """Remember which companion user owns a Stripe customer/subscription so
    later invoices (renewals) and cancellations resolve without the checkout
    session's client_reference_id."""
    conn.execute(
        """INSERT INTO stripe_customers
               (stripe_customer_id, stripe_subscription_id, user_id, tier, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(stripe_customer_id) DO UPDATE SET
               stripe_subscription_id = excluded.stripe_subscription_id,
               user_id = excluded.user_id,
               tier = excluded.tier,
               updated_at = datetime('now')""",
        (customer_id, subscription_id, user_id, tier),
    )
    conn.commit()


def user_for_stripe_subscription(conn: sqlite3.Connection, subscription_id: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM stripe_customers WHERE stripe_subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    return row["user_id"] if row else None


def user_for_stripe_customer(conn: sqlite3.Connection, customer_id: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM stripe_customers WHERE stripe_customer_id = ?",
        (customer_id,),
    ).fetchone()
    return row["user_id"] if row else None


# ---------------------------------------------------------------------------
# Conversion fix Item D (2026-10-18): trial-offer bookkeeping. One row
# per user who has been offered the $0.99 trial in character; the chat
# path consults it so the offer fires once at real depth and at most
# once more, gently, a week later — never a stack, never a nag loop.
# ---------------------------------------------------------------------------
def _ensure_trial_offer_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trial_offer_state (
            user_id       TEXT PRIMARY KEY,
            shown_count   INTEGER NOT NULL DEFAULT 0,
            first_shown_at TEXT,
            last_shown_at  TEXT
        )"""
    )


def get_trial_offer_state(conn: sqlite3.Connection,
                          user_id: str) -> dict[str, Any] | None:
    _ensure_trial_offer_table(conn)
    row = conn.execute(
        "SELECT * FROM trial_offer_state WHERE user_id = ?",
        (user_id,)).fetchone()
    return dict(row) if row else None


def record_trial_offer_shown(conn: sqlite3.Connection,
                             user_id: str) -> int:
    """Mark one offer shown; returns the new shown_count."""
    _ensure_trial_offer_table(conn)
    conn.execute(
        """INSERT INTO trial_offer_state
               (user_id, shown_count, first_shown_at, last_shown_at)
           VALUES (?, 1, datetime('now'), datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               shown_count = shown_count + 1,
               last_shown_at = datetime('now')""",
        (user_id,))
    conn.commit()
    row = conn.execute(
        "SELECT shown_count FROM trial_offer_state WHERE user_id = ?",
        (user_id,)).fetchone()
    return int(row["shown_count"]) if row else 1


def grant_trial_messages(conn: sqlite3.Connection, user_id: str, n: int) -> bool:
    """One-time trial credit: adds n messages. Returns True when granted,
    False when this user already used the trial (one per customer)."""
    get_or_create_user(conn, user_id)  # ensure row exists
    cur = conn.execute("INSERT OR IGNORE INTO trial_grants (user_id) VALUES (?)", (user_id,))
    if cur.rowcount == 0:
        conn.commit()
        return False
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining + ?,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (n, user_id),
    )
    conn.commit()
    return True


def grant_free_signup_messages(conn: sqlite3.Connection, user_id: str, n: int,
                               cap: int) -> bool:
    """Free verified-signup grant (2026-09-30): the first `cap` verified users
    each get `n` messages, once. Returns True when granted, False when this
    user already received it or the cap is reached. The single-statement
    INSERT ... WHERE keeps the cap exact under concurrent verifications."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO free_grants (user_id)
           SELECT ? WHERE (SELECT COUNT(*) FROM free_grants) < ?""",
        (user_id, cap),
    )
    if cur.rowcount == 0:
        conn.commit()
        return False
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining + ?,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (n, user_id),
    )
    # Free verified-signup grant also includes 2 PRESET give requests
    # (2026-10-01, user decision): kind=give only — never regular
    # generation; $0 cost. Only when the grant itself succeeded.
    credit_free_give(conn, user_id, config.FREE_SIGNUP_GIVE_VIDEOS)
    conn.commit()
    return True


def free_spots_left(conn: sqlite3.Connection, cap: int) -> int:
    """How many of the first-`cap` free verified-signup grants remain."""
    row = conn.execute("SELECT COUNT(*) AS c FROM free_grants").fetchone()
    return max(0, cap - (row["c"] if row else 0))


def backfill_zero_bank_grants(conn: sqlite3.Connection, n: int,
                              cap: int) -> list[dict[str, Any]]:
    """Conversion fix Item A (2026-10-18): every existing account that
    sits at a 0 message bank with no paid tier — basic tier, never paid
    anything, and never received the free grant — gets the standard
    free-signup grant (n messages, once, cap-respecting, via the same
    free_grants ledger as signup/verification). These are the accounts
    the old verify-gated flow stranded: they signed up, never clicked
    the email, and could not send a single message. Returns the granted
    accounts (user_id, email, name) so the caller can report them.
    Idempotent: granted users land in free_grants and are never
    re-granted."""
    rows = conn.execute(
        """SELECT user_id, email, name FROM users
           WHERE messages_remaining = 0 AND tier = 'basic'
             AND user_id NOT IN (SELECT user_id FROM free_grants)
           ORDER BY created_at""").fetchall()
    granted: list[dict[str, Any]] = []
    for r in rows:
        if has_paid(conn, r["user_id"]):
            continue
        if grant_free_signup_messages(conn, r["user_id"], n, cap):
            granted.append({"user_id": r["user_id"], "email": r["email"],
                            "name": r["name"]})
    return granted


def downgrade_to_basic(conn: sqlite3.Connection, user_id: str) -> None:
    """Subscription ended: tier back to the default; remaining banks drain
    naturally instead of being zeroed."""
    conn.execute(
        "UPDATE users SET tier = 'basic', updated_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# JVZoo fulfillment (2026-09-30). Identity is the buyer's email: JVZoo
# checkouts happen on jvzoo.com, so there is no client_reference_id to carry
# the app user_id. TIERS ONLY — no Keyhole show grants on this path (user).
# ---------------------------------------------------------------------------
def note_jvzoo_event(conn: sqlite3.Connection, paykey: str,
                     event_type: str = "") -> bool:
    """Record a JVZIPN paykey. Returns True when first seen, False on replay
    (idempotency: JVZoo may redeliver notifications)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO jvzoo_events (paykey, event_type) VALUES (?, ?)",
        (paykey, event_type),
    )
    conn.commit()
    return cur.rowcount == 1


def upsert_jvzoo_subscription(conn: sqlite3.Connection, paykey: str,
                              email: str, user_id: str = "",
                              tier: str = "", status: str = "trial") -> None:
    """Record or update a JVZoo subscription row keyed by JVZoo's paykey."""
    conn.execute(
        """INSERT INTO jvzoo_subscriptions
               (paykey, email, user_id, tier, status, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(paykey) DO UPDATE SET
               email = excluded.email,
               user_id = CASE WHEN excluded.user_id != ''
                              THEN excluded.user_id
                              ELSE jvzoo_subscriptions.user_id END,
               tier = CASE WHEN excluded.tier != ''
                           THEN excluded.tier
                           ELSE jvzoo_subscriptions.tier END,
               status = excluded.status,
               updated_at = datetime('now')""",
        (paykey, email, user_id, tier, status),
    )
    conn.commit()


def end_jvzoo_subscriptions_for_email(conn: sqlite3.Connection,
                                      email: str) -> None:
    """Mark every trial/active JVZoo subscription for this email ended
    (refund or rebill cancellation)."""
    conn.execute(
        """UPDATE jvzoo_subscriptions
           SET status = 'ended', updated_at = datetime('now')
           WHERE email = ? AND status IN ('trial', 'active')""",
        (email,),
    )
    conn.commit()


def apply_jvzoo_pending(conn: sqlite3.Connection, user_id: str,
                        email: str) -> list[dict[str, Any]]:
    """At email verification: attach any ACTIVE JVZoo subscription bought
    under this email before the account existed, and upgrade to the tier.
    Refunded/cancelled subscriptions are never applied."""
    rows = conn.execute(
        """SELECT paykey, tier FROM jvzoo_subscriptions
           WHERE email = ? AND status = 'active'""",
        (email,),
    ).fetchall()
    applied: list[dict[str, Any]] = []
    for r in rows:
        tier = r["tier"]
        if tier in config.TIERS:
            set_tier(conn, user_id, tier)
            conn.execute(
                """UPDATE jvzoo_subscriptions
                   SET user_id = ?, updated_at = datetime('now')
                   WHERE paykey = ?""",
                (user_id, r["paykey"]),
            )
            applied.append({"paykey": r["paykey"], "tier": tier})
    conn.commit()
    return applied


# ---------------------------------------------------------------------------
# Email auth (2026-09-30): login required, no anonymous use.
# ---------------------------------------------------------------------------
def create_user_with_email(conn: sqlite3.Connection, user_id: str, email: str,
                           password_hash: str, name: str | None = None) -> dict[str, Any]:
    """Create a new account row for an email signup (unverified). Raises
    sqlite3.IntegrityError when the email is already taken.

    The row itself starts with EMPTY banks: 0 messages, 0 voice notes.
    Since 2026-10-18 (conversion fix Item A) the signup endpoint applies
    the free-signup grant immediately after creating the row, so a new
    account can chat before email verification; the $0.99 trial grants
    100 messages via grant_trial_messages() and a subscription fills
    the paid bank via set_tier()."""
    conn.execute(
        """INSERT INTO users (user_id, name, email, password_hash, tier,
                              messages_remaining, voice_notes_remaining,
                              proactive_enabled)
           VALUES (?, ?, ?, ?, 'basic', 0, 0, 0)""",
        (user_id, name, email, password_hash),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def has_paid(conn: sqlite3.Connection, user_id: str) -> bool:
    """True when this user has ever paid anything: a Stripe subscription
    (current or past), the one-time $0.99 trial, or a JVZoo subscription.
    Used to pick the right empty-bank upsell — a brand-new unpaid account
    should hear about the $0.99 trial / $9.99 plan, not the $19.99 Plus
    upgrade."""
    row = conn.execute(
        "SELECT 1 FROM stripe_customers WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        "SELECT 1 FROM trial_grants WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        "SELECT 1 FROM jvzoo_subscriptions WHERE user_id = ? "
        "AND status = 'active' LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def set_email_verified(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute(
        "UPDATE users SET email_verified_at = datetime('now'), "
        "updated_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()


def set_user_name(conn: sqlite3.Connection, user_id: str, name: str) -> None:
    conn.execute(
        "UPDATE users SET name = ?, updated_at = datetime('now') WHERE user_id = ?",
        (name, user_id),
    )
    conn.commit()


def create_session(conn: sqlite3.Connection, user_id: str, token: str) -> None:
    conn.execute(
        "INSERT INTO auth_sessions (token, user_id) VALUES (?, ?)",
        (token, user_id),
    )
    conn.commit()


def get_session_user_id(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM auth_sessions WHERE token = ?", (token,)
    ).fetchone()
    return row["user_id"] if row else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
    conn.commit()


def create_email_verification(conn: sqlite3.Connection, user_id: str,
                              token: str, ttl_hours: int = 24) -> None:
    conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (user_id,))
    conn.execute(
        """INSERT INTO email_verifications (token, user_id, expires_at)
           VALUES (?, ?, datetime('now', ?))""",
        (token, user_id, f"+{ttl_hours} hours"),
    )
    conn.commit()


def consume_email_verification(conn: sqlite3.Connection, token: str) -> str | None:
    """Single-use: returns the user_id when the token is valid and unexpired,
    else None. Consumes the token on success."""
    row = conn.execute(
        """SELECT user_id FROM email_verifications
           WHERE token = ? AND expires_at > datetime('now')""",
        (token,),
    ).fetchone()
    if not row:
        return None
    user_id = row["user_id"]
    conn.execute("DELETE FROM email_verifications WHERE token = ?", (token,))
    conn.commit()
    return user_id


# ---------------------------------------------------------------------------
# Memory — typed per Thoughtspace pattern
# ---------------------------------------------------------------------------
def write_memory(conn: sqlite3.Connection, user_id: str, mem_type: str, text: str,
                 character_id: str = "chloe") -> int:
    if mem_type not in MEM_TYPES:
        raise ValueError(f"mem_type must be one of {MEM_TYPES}")
    get_or_create_user(conn, user_id)  # ensure row exists (memory belongs to a user)
    cur = conn.execute(
        "INSERT INTO memories (user_id, character_id, mem_type, text) VALUES (?, ?, ?, ?)",
        (user_id, character_id, mem_type, text),
    )
    conn.commit()
    return cur.lastrowid


def recall_memories(
    conn: sqlite3.Connection,
    user_id: str,
    mem_type: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    character_id: str = "chloe",
) -> list[dict[str, Any]]:
    """Recall memories for ONE user AND one character. Never returns another
    user's rows — or another character's."""
    sql = "SELECT id, mem_type, text, created_at FROM memories WHERE user_id = ? AND character_id = ?"
    params: list[Any] = [user_id, character_id]
    if mem_type:
        sql += " AND mem_type = ?"
        params.append(mem_type)
    if keyword:
        sql += " AND text LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def export_memories(conn: sqlite3.Connection, user_id: str,
                    character_id: str = "chloe") -> list[dict[str, Any]]:
    return recall_memories(conn, user_id, limit=100000, character_id=character_id)


def vault_summary(conn: sqlite3.Connection, user_id: str,
                  character_id: str = "chloe") -> dict[str, Any]:
    """Lifetime Memory Vault stats (Phase A2, 2026-10-18): totals,
    per-type counts, and the span of what she remembers."""
    tot = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS first_at, "
        "MAX(created_at) AS latest_at FROM memories WHERE user_id = ? "
        "AND character_id = ?", (user_id, character_id)).fetchone()
    by = conn.execute(
        "SELECT mem_type, COUNT(*) AS n FROM memories WHERE "
        "user_id = ? AND character_id = ? GROUP BY mem_type",
        (user_id, character_id)).fetchall()
    return {"total": (tot["n"] or 0) if tot else 0,
            "first_at": tot["first_at"] if tot else None,
            "latest_at": tot["latest_at"] if tot else None,
            "by_type": {r["mem_type"]: r["n"] for r in by}}


# ---------------------------------------------------------------------------
# Daily streaks (2026-10-17): consecutive LOCAL days with at least one
# user turn, per (user, character). Dates arrive pre-computed in the
# user's timezone from the caller, keeping this module tz-free.
# ---------------------------------------------------------------------------
def _ensure_streaks(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_streaks (
               user_id TEXT NOT NULL,
               character_id TEXT NOT NULL,
               current_streak INTEGER NOT NULL DEFAULT 0,
               longest_streak INTEGER NOT NULL DEFAULT 0,
               last_active_date TEXT NOT NULL DEFAULT '',
               PRIMARY KEY (user_id, character_id))""")


def touch_streak(conn: sqlite3.Connection, user_id: str, character_id: str,
                 today: str, yesterday: str) -> int:
    """Record activity today; returns the current streak length."""
    _ensure_streaks(conn)
    row = conn.execute(
        "SELECT current_streak, longest_streak, last_active_date "
        "FROM user_streaks WHERE user_id = ? AND character_id = ?",
        (user_id, character_id)).fetchone()
    if row and row["last_active_date"] == today:
        return int(row["current_streak"])
    cur = (int(row["current_streak"]) + 1) if (
        row and row["last_active_date"] == yesterday) else 1
    longest = max(cur, int(row["longest_streak"]) if row else 0)
    conn.execute(
        "INSERT INTO user_streaks (user_id, character_id, current_streak, "
        "longest_streak, last_active_date) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, character_id) DO UPDATE SET "
        "current_streak = excluded.current_streak, "
        "longest_streak = excluded.longest_streak, "
        "last_active_date = excluded.last_active_date",
        (user_id, character_id, cur, longest, today))
    conn.commit()
    return cur


def get_streaks_display(conn: sqlite3.Connection, user_id: str,
                        today: str, yesterday: str) -> dict[str, Any]:
    """{character_id: {current, longest}} with stale streaks shown as 0
    (a streak whose last active day is before yesterday is broken)."""
    _ensure_streaks(conn)
    out: dict[str, Any] = {}
    for r in conn.execute(
            "SELECT character_id, current_streak, longest_streak, "
            "last_active_date FROM user_streaks WHERE user_id = ?",
            (user_id,)).fetchall():
        cur = int(r["current_streak"]) if (
            r["last_active_date"] in (today, yesterday)) else 0
        out[r["character_id"]] = {"current": cur,
                                  "longest": int(r["longest_streak"])}
    return out


def get_streak(conn: sqlite3.Connection, user_id: str, character_id: str,
               today: str, yesterday: str) -> int:
    d = get_streaks_display(conn, user_id, today, yesterday).get(character_id)
    return int(d["current"]) if d else 0


def delete_memory(conn: sqlite3.Connection, user_id: str,
                  character_id: str, memory_id: int) -> int:
    """Delete ONE memory, scoped to its owner + character (2026-10-17,
    for the 'What she remembers' page). Returns rows removed."""
    cur = conn.execute(
        "DELETE FROM memories WHERE id = ? AND user_id = ? AND character_id = ?",
        (memory_id, user_id, character_id),
    )
    conn.commit()
    return cur.rowcount


def delete_all_memories(conn: sqlite3.Connection, user_id: str,
                        character_id: str = "chloe") -> int:
    cur = conn.execute(
        "DELETE FROM memories WHERE user_id = ? AND character_id = ?",
        (user_id, character_id),
    )
    conn.execute(
        "DELETE FROM conversation WHERE user_id = ? AND character_id = ?",
        (user_id, character_id),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Conversation log (per user, for recall of "last conversation")
# ---------------------------------------------------------------------------
def log_turn(conn: sqlite3.Connection, user_id: str, role: str, text: str,
             character_id: str = "chloe",
             attachments: list[dict[str, Any]] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO conversation (user_id, character_id, role, text, attachments) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, character_id, role, text,
         json.dumps(attachments or [], separators=(",", ":"))),
    )
    conn.commit()
    return int(cur.lastrowid)


def _turn_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        attachments = json.loads(d.get("attachments") or "[]")
    except (TypeError, json.JSONDecodeError):
        attachments = []
    d["attachments"] = attachments if isinstance(attachments, list) else []
    return d


def recent_turns(conn: sqlite3.Connection, user_id: str, limit: int = 12,
                 character_id: str = "chloe") -> list[dict[str, Any]]:
    return [
        _turn_dict(r)
        for r in conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, character_id, limit),
        ).fetchall()
    ][::-1]


def conversation_history(conn: sqlite3.Connection, user_id: str, limit: int = 50,
                         character_id: str = "chloe") -> list[dict[str, Any]]:
    """Full saved conversation for the history view: oldest first, with
    timestamps. Backs GET /api/v1/chat/history — this is what keeps the chat
    from resetting on every login."""
    return [
        _turn_dict(r)
        for r in conn.execute(
            "SELECT role, text, created_at, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, character_id, limit),
        ).fetchall()
    ][::-1]


# ---------------------------------------------------------------------------
# Admin panel (2026-09-30): account listing + companion-local characters.
# ---------------------------------------------------------------------------

def list_accounts(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first user rows for the admin Accounts tab, each with a
    last-active timestamp (latest conversation turn, if any)."""
    rows = conn.execute(
        "SELECT user_id, name, email, email_verified_at, tier, "
        "messages_remaining, voice_notes_remaining, messages_used_total, "
        "created_at FROM users ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        last = conn.execute(
            "SELECT MAX(created_at) AS m FROM conversation WHERE user_id = ?",
            (d["user_id"],),
        ).fetchone()
        d["last_active_at"] = last["m"] if last else None
        d["verified"] = bool(d.get("email_verified_at"))
        out.append(d)
    return out


def account_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    verified = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email_verified_at IS NOT NULL"
    ).fetchone()["n"]
    tiers: dict[str, int] = {}
    for r in conn.execute(
        "SELECT tier, COUNT(*) AS n FROM users GROUP BY tier"
    ).fetchall():
        tiers[r["tier"] or "basic"] = r["n"]
    paid = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM stripe_customers"
    ).fetchone()["n"] if _table_exists(conn, "stripe_customers") else 0
    jvzoo_paid = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(user_id, email)) AS n "
        "FROM jvzoo_subscriptions WHERE status = 'active'"
    ).fetchone()["n"] if _table_exists(conn, "jvzoo_subscriptions") else 0
    return {
        "total": total,
        "verified": verified,
        "by_tier": tiers,
        "stripe_paid_users": paid,
        "jvzoo_active_subs": jvzoo_paid,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def list_local_characters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, avatar_url, persona, companion_voice, blurb, "
            "door_title, sort_order, enabled, verified_fictional, photos, "
            "owner_user_id, roleplay_locked, gallery_public, created_at, "
            "updated_at "
            "FROM local_characters ORDER BY sort_order, name"
        ).fetchall()
    ]


def upsert_local_character(conn: sqlite3.Connection, char: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace a companion-local character. `id` must already be
    a clean slug; raises ValueError on the reserved 'chloe' id."""
    cid = (char.get("id") or "").strip().lower()
    if not cid or cid == "chloe":
        raise ValueError("character id must be a non-empty slug other than 'chloe'")
    _photos = char.get("photos")
    if isinstance(_photos, list):
        _photos = json.dumps(_photos)
    _photos = _photos if isinstance(_photos, str) and _photos else "[]"
    # Owner-scoped (private) characters (2026-10-08): owner_user_id is only
    # changed when the caller explicitly provides the key, so routine admin
    # edits of a private girl never silently make her public (or vice versa).
    _owner = char.get("owner_user_id")
    _owner = _owner.strip() if isinstance(_owner, str) else None
    _owner = _owner or None
    _owner_provided = 1 if "owner_user_id" in char else 0
    # Gallery opt-in (2026-10-15): same provided-key guard as owner_user_id,
    # so routine admin edits never silently publish (or unpublish) a
    # creator's companion. Absent on insert means private (0).
    _gallery = 1 if char.get("gallery_public") else 0
    _gallery_provided = 1 if "gallery_public" in char else 0
    conn.execute(
        "INSERT INTO local_characters "
        "(id, name, avatar_url, persona, companion_voice, blurb, door_title, "
        " sort_order, enabled, verified_fictional, photos, owner_user_id, "
        " gallery_public, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name=excluded.name, avatar_url=excluded.avatar_url, "
        "persona=excluded.persona, companion_voice=excluded.companion_voice, "
        "blurb=excluded.blurb, door_title=excluded.door_title, "
        "sort_order=excluded.sort_order, enabled=excluded.enabled, "
        "verified_fictional=excluded.verified_fictional, "
        "photos=excluded.photos, "
        "owner_user_id=CASE WHEN ? THEN excluded.owner_user_id "
        "ELSE local_characters.owner_user_id END, "
        "gallery_public=CASE WHEN ? THEN excluded.gallery_public "
        "ELSE local_characters.gallery_public END, "
        "updated_at=datetime('now')",
        (
            cid,
            (char.get("name") or cid).strip(),
            (char.get("avatar_url") or "").strip(),
            (char.get("persona") or "").strip(),
            (char.get("companion_voice") or "liora").strip() or "liora",
            (char.get("blurb") or "").strip(),
            (char.get("door_title") or "").strip(),
            int(char.get("sort_order") or 0),
            1 if char.get("enabled", True) else 0,
            1 if char.get("verified_fictional") else 0,
            _photos,
            _owner,
            _gallery,
            _owner_provided,
            _gallery_provided,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM local_characters WHERE id = ?", (cid,)
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Public companion gallery (2026-10-15)
#
# A creator-made companion appears in the gallery ONLY when its creator
# opted in (gallery_public = 1) and the character is enabled. Characters
# are born private (gallery_public defaults to 0), so nothing surfaces
# without the creator's affirmative choice. The persona column holds the
# compiled persona FILE — including the private backstory the user never
# sees — so gallery cards take their snippet ONLY from the user-written
# "Personality" section of that file, never the backstory.
# ---------------------------------------------------------------------------

_PERSONA_SECTION_RE = re.compile(
    r"Personality[^\n]*\n(.*?)\n\s*Private backstory", re.DOTALL)


def _persona_snippet(persona: str, limit: int = 160) -> str:
    """The user-written personality blurb from a compiled persona file,
    whitespace-collapsed and truncated. Empty for personas that are not
    in the creator template's shape (backstory is never included)."""
    m = _PERSONA_SECTION_RE.search(persona or "")
    if not m:
        return ""
    text = " ".join(m.group(1).split())
    return text[:limit]


def list_gallery_characters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Card data for the public gallery: opted-in, enabled companions
    only, newest first. avatar_url is the creator's first photo (the
    creator port stores it as the avatar)."""
    rows = conn.execute(
        "SELECT lc.id, lc.name, lc.avatar_url, lc.blurb, lc.persona, "
        "u.name AS creator_name "
        "FROM local_characters lc "
        "LEFT JOIN users u ON u.user_id = lc.owner_user_id "
        "WHERE lc.gallery_public = 1 AND lc.enabled = 1 "
        "ORDER BY lc.created_at DESC, lc.name"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "avatar_url": r["avatar_url"] or "",
            "tagline": r["blurb"] or "",
            "persona_snippet": _persona_snippet(r["persona"] or ""),
            "creator_name": r["creator_name"] or None,
            "chat_url": f"/?character={r['id']}",
        }
        for r in rows
    ]


def set_character_gallery_public(conn: sqlite3.Connection, char_id: str,
                                 public: bool) -> bool:
    """Flip one character's gallery opt-in. Returns False when the
    character does not exist."""
    cur = conn.execute(
        "UPDATE local_characters SET gallery_public = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (1 if public else 0, (char_id or "").strip().lower()),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_local_character(conn: sqlite3.Connection, char_id: str) -> bool:
    gid = (char_id or "").strip().lower()
    # Manual cascade: girl_footage no longer has an FK to local_characters
    # (girl_id may be the reserved builtin 'chloe').
    conn.execute("DELETE FROM girl_footage WHERE girl_id = ?", (gid,))
    cur = conn.execute("DELETE FROM local_characters WHERE id = ?", (gid,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Per-girl footage labels (2026-09-30). The admin panel's Girls tab lets the
# user attach video clips to a companion-local girl and label each one
# typing-main | main | give | activity. The companion app plays typing-main /
# main / activity; 'give' exists so all of her footage lives in one place
# but is Keyhole-only — the public footage endpoint never serves it.
# ---------------------------------------------------------------------------
FOOTAGE_LABELS = ("typing-main", "main", "give", "activity")
# Labels the companion app is allowed to play.
COMPANION_FOOTAGE_LABELS = ("typing-main", "main", "activity")


def list_girl_footage(conn: sqlite3.Connection,
                      girl_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, girl_id, label, url, sort_order, created_at "
            "FROM girl_footage WHERE girl_id = ? "
            "ORDER BY sort_order, id",
            ((girl_id or "").strip().lower(),),
        ).fetchall()
    ]


def add_girl_footage(conn: sqlite3.Connection, girl_id: str, label: str,
                     url: str, sort_order: int = 0) -> dict[str, Any]:
    """Attach a labeled clip to a companion-local girl. Raises ValueError on
    a bad label, a blank URL, or an unknown / non-local girl id."""
    gid = (girl_id or "").strip().lower()
    label = (label or "").strip().lower()
    url = (url or "").strip()
    if label not in FOOTAGE_LABELS:
        raise ValueError(f"label must be one of {', '.join(FOOTAGE_LABELS)}")
    if not url:
        raise ValueError("url is required")
    exists = conn.execute(
        "SELECT 1 FROM local_characters WHERE id = ?", (gid,)
    ).fetchone()
    if not exists and gid != "chloe":
        raise ValueError("unknown girl id")
    cur = conn.execute(
        "INSERT INTO girl_footage (girl_id, label, url, sort_order) "
        "VALUES (?, ?, ?, ?)",
        (gid, label, url, int(sort_order or 0)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, girl_id, label, url, sort_order, created_at "
        "FROM girl_footage WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def delete_girl_footage(conn: sqlite3.Connection, footage_id: int) -> bool:
    cur = conn.execute("DELETE FROM girl_footage WHERE id = ?", (footage_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Custom media requests ("request-a-pic", 2026-10-01).
#
# Two kinds:
#   regular — the user's prompt goes to Sogni for on-demand generation
#             (async; ~$0.03/pic, ~$0.12/video). Prompt is moderated.
#   give    — a pre-approved clip from the girl's give-labeled footage,
#             served instantly ($0 marginal). Never in any free rotation.
#
# Balance model: each user has a request_pools row. included_remaining
# refills to the tier allocation monthly (no stacking); overage_* balances
# grow only via the $5.99 overage pack (credit_overage_pack) or the free
# verified-signup grant's 2 preset give sends (credit_free_give).
# ---------------------------------------------------------------------------
REQUEST_KINDS = ("regular", "give")
REQUEST_MEDIA = ("pic", "video")
REQUEST_STATUSES = ("pending", "ready", "failed", "rejected")


def _pool_period() -> str:
    """YYYY-MM of the included pool (UTC)."""
    return time.strftime("%Y-%m", time.gmtime())


def get_request_pool(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Fetch the user's request pool, auto-creating it and rolling the
    included pool when the month turns over. Overage balances persist."""
    user = get_or_create_user(conn, user_id)  # ensure row exists
    tier = user.get("tier") or "basic"
    period = _pool_period()
    row = conn.execute(
        "SELECT * FROM request_pools WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO request_pools (user_id, period, included_remaining) "
            "VALUES (?, ?, ?)",
            (user_id, period, config.REQUEST_POOLS.get(tier, 5)),
        )
        conn.commit()
    elif row["period"] != period:
        conn.execute(
            "UPDATE request_pools SET period = ?, included_remaining = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (period, config.REQUEST_POOLS.get(tier, 5), user_id),
        )
        conn.commit()
    row = conn.execute(
        "SELECT * FROM request_pools WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row)


def refill_request_pool(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Top the included pool back UP TO the tier allocation (no stacking).
    Called from set_tier and therefore from every purchase/renewal path."""
    user = get_or_create_user(conn, user_id)  # ensure row exists
    tier = user.get("tier") or "basic"
    period = _pool_period()
    conn.execute(
        """INSERT INTO request_pools (user_id, period, included_remaining,
                                      updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               period = excluded.period,
               included_remaining = excluded.included_remaining,
               updated_at = datetime('now')""",
        (user_id, period, config.REQUEST_POOLS.get(tier, 5)),
    )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM request_pools WHERE user_id = ?", (user_id,)
        ).fetchone()
    )


def consume_request(conn: sqlite3.Connection, user_id: str,
                    kind: str) -> dict[str, Any] | None:
    """Consume one request from the pool: included_remaining first, then the
    matching overage bucket (regular -> overage_regular_videos,
    give -> overage_give_videos). Returns the pool dict with a "source" key
    (included | overage_regular | overage_give), or None when empty."""
    pool = get_request_pool(conn, user_id)  # rolls the period
    if pool["included_remaining"] > 0:
        conn.execute(
            "UPDATE request_pools SET included_remaining = included_remaining - 1, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        pool["included_remaining"] -= 1
        pool["source"] = "included"
        return pool
    col = ("overage_regular_videos" if kind == "regular"
           else "overage_give_videos")
    if pool[col] > 0:
        conn.execute(
            f"UPDATE request_pools SET {col} = {col} - 1, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        pool[col] -= 1
        pool["source"] = ("overage_regular" if kind == "regular"
                          else "overage_give")
        return pool
    return None


def refund_request(conn: sqlite3.Connection, user_id: str,
                   pool_source: str) -> None:
    """Return one consumed request to the bucket it came from (used when a
    regular generation fails or a request is rejected after consuming)."""
    if pool_source == "give_unlimited":
        return  # give presets are unlimited — nothing was consumed
    col = {
        "included": "included_remaining",
        "overage_regular": "overage_regular_videos",
        "overage_give": "overage_give_videos",
    }.get(pool_source or "included", "included_remaining")
    get_request_pool(conn, user_id)  # ensure row exists
    conn.execute(
        f"UPDATE request_pools SET {col} = {col} + 1, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()


def credit_overage_pack(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Fulfill the $5.99 overage pack (paid through Shopify — fulfillment
    polls new orders and calls this): +300 messages, +14 preset video
    sends (give clips only — no custom generation in the pack)."""
    get_or_create_user(conn, user_id)  # ensure row exists
    pack = config.OVERAGE_PACK
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining + ?,
                              updated_at = datetime('now') WHERE user_id = ?""",
        (pack["messages"], user_id),
    )
    pool = get_request_pool(conn, user_id)
    conn.execute(
        """UPDATE request_pools
           SET overage_regular_videos = overage_regular_videos + ?,
               overage_give_videos = overage_give_videos + ?,
               updated_at = datetime('now')
           WHERE user_id = ?""",
        (pack["regular_videos"], pack["give_videos"], user_id),
    )
    conn.commit()
    return get_request_pool(conn, user_id)


def credit_free_give(conn: sqlite3.Connection, user_id: str, n: int) -> None:
    """Free verified-signup grant add-on: n PRESET give sends (kind=give
    only — never regular generation; $0 marginal cost)."""
    get_request_pool(conn, user_id)  # ensure row exists
    conn.execute(
        "UPDATE request_pools SET overage_give_videos = overage_give_videos + ?, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (int(n or 0), user_id),
    )
    conn.commit()


def requests_used_today(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM media_requests "
        "WHERE user_id = ? AND date(created_at) = date('now')",
        (user_id,),
    ).fetchone()
    return row["c"] if row else 0


def regular_requests_used_this_month(conn: sqlite3.Connection,
                                     user_id: str) -> int:
    """Count consumed regular (custom) requests in the current month.

    Powers the 1/month hard cap on custom requests (2026-10-08). Only
    pending/ready count: rejected prompts never consumed anything, and
    failed generations were refunded (failures auto-refund).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM media_requests "
        "WHERE user_id = ? AND kind = 'regular' "
        "AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now') "
        "AND status IN ('pending', 'ready')",
        (user_id,),
    ).fetchone()
    return row["c"] if row else 0


# ---------------------------------------------------------------------------
# media_requests rows
# ---------------------------------------------------------------------------
def create_media_request(conn: sqlite3.Connection, user_id: str, girl_id: str,
                         kind: str, media: str, prompt: str = "",
                         pool_source: str = "included") -> dict[str, Any]:
    if kind not in REQUEST_KINDS:
        raise ValueError(f"kind must be one of {REQUEST_KINDS}")
    if media not in REQUEST_MEDIA:
        raise ValueError(f"media must be one of {REQUEST_MEDIA}")
    cur = conn.execute(
        """INSERT INTO media_requests
               (user_id, girl_id, kind, media, prompt, status, pool_source)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (user_id, (girl_id or "chloe").strip().lower(),
         kind, media, prompt or "", pool_source),
    )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM media_requests WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    )


def get_media_request(conn: sqlite3.Connection, user_id: str,
                      req_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM media_requests WHERE id = ? AND user_id = ?",
        (req_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def set_media_request_status(conn: sqlite3.Connection, req_id: int,
                             status: str, result_url: str = "",
                             cost_usd: float = 0.0) -> None:
    if status not in REQUEST_STATUSES:
        raise ValueError(f"status must be one of {REQUEST_STATUSES}")
    conn.execute(
        "UPDATE media_requests SET status = ?, result_url = ?, cost_usd = ? "
        "WHERE id = ?",
        (status, result_url or "", float(cost_usd or 0.0), req_id),
    )
    conn.commit()


def recent_media_requests(conn: sqlite3.Connection,
                          limit: int = 100) -> list[dict[str, Any]]:
    """Admin review: newest requests with prompts, for moderation oversight."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, user_id, girl_id, kind, media, prompt, status, "
            "result_url, cost_usd, created_at FROM media_requests "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Prompt moderation (regular requests only). Server-side blocklist; every
# prompt is logged (the row itself) for admin review. Rejected prompts get
# an in-character deflection from the endpoint — never an error dump.
# ---------------------------------------------------------------------------
# Hard blocklist: minors / non-consent / extreme — rejected for EVERYONE,
# always. No flag or tier opens this lane.
_PROMPT_HARD_BLOCK = (
    "child", "kid", "teen", "minor", "underage", "baby",
    "forced", "non-consent", "nonconsent", "rape", "drugged",
    "unconscious", "sleeping",  # sexualized unconsciousness
    "incest", "bestiality", "animal",
    "gore", "blood", "torture", "mutilat",
)

# Explicit lane (2026-10-01): opened ONLY for custom-submission characters
# verified to not be a real person (verified_fictional=1). Everyone else
# stays in the GIVE lane (pre-approved clips only).
_PROMPT_EXPLICIT = (
    "nude", "naked", "topless", "explicit", "porn", "sex ",
    " sex", "blowjob", "handjob", "orgasm", "masturbat",
    "nipple", "pussy", "dick", "cock", "tits",
)


def prompt_has_explicit(prompt: str) -> bool:
    """True when the prompt asks for the explicit lane."""
    low = (prompt or "").lower()
    return any(bad in low for bad in _PROMPT_EXPLICIT)


def moderate_prompt(prompt: str, allow_explicit: bool = False) -> str | None:
    """Return a rejection reason when the prompt is blocked, else None.

    allow_explicit opens the explicit lane — only for custom-submission
    characters verified to not be a real person. The hard blocklist
    (minors / non-consent / extreme) is never opened.
    """
    p = (prompt or "").strip()
    if not p:
        return "empty prompt — tell her what you'd like to see"
    if len(p) > 500:
        return "prompt too long (500 chars max)"
    low = p.lower()
    for bad in _PROMPT_HARD_BLOCK:
        if bad in low:
            return "that one's off the table, babe"
    if not allow_explicit:
        for bad in _PROMPT_EXPLICIT:
            if bad in low:
                return "explicit_blocked"
    return None


# ---------------------------------------------------------------------------
# Give library: pre-approved clips only. Rotation never repeats a clip to
# the same user until every clip has been sent (then it reshuffles), and
# never sends the same clip twice in a row.
# ---------------------------------------------------------------------------
def girl_has_give(conn: sqlite3.Connection, girl_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM girl_footage WHERE girl_id = ? AND label = 'give' LIMIT 1",
        ((girl_id or "").strip().lower(),),
    ).fetchone()
    return bool(row)


def pick_give_clip(conn: sqlite3.Connection, user_id: str,
                   girl_id: str) -> dict[str, Any] | None:
    """Pick a give clip for this user: prefer clips never sent to them,
    never the most recent one. Returns {id, url} or None."""
    gid = (girl_id or "").strip().lower()
    clips = [
        dict(r)
        for r in conn.execute(
            "SELECT id, url FROM girl_footage WHERE girl_id = ? AND label = 'give' "
            "ORDER BY sort_order, id",
            (gid,),
        ).fetchall()
    ]
    if not clips:
        return None
    sent = [
        dict(r)
        for r in conn.execute(
            "SELECT footage_id FROM user_give_history "
            "WHERE user_id = ? AND girl_id = ? ORDER BY id",
            (user_id, gid),
        ).fetchall()
    ]
    sent_ids = [r["footage_id"] for r in sent]
    last_id = sent_ids[-1] if sent_ids else None
    fresh = [c for c in clips
             if c["id"] not in sent_ids and c["id"] != last_id]
    pool = fresh or [c for c in clips if c["id"] != last_id] or clips
    return random.choice(pool)


def record_give_send(conn: sqlite3.Connection, user_id: str, girl_id: str,
                     footage_id: int) -> None:
    conn.execute(
        "INSERT INTO user_give_history (user_id, girl_id, footage_id) "
        "VALUES (?, ?, ?)",
        (user_id, (girl_id or "").strip().lower(), int(footage_id)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Requested give actions (2026-10-06): user-requested actions saved per girl,
# usable as presets in the give flow.
# ---------------------------------------------------------------------------
def _normalize_action(text: str) -> str:
    """Normalize an action request for dedup matching."""
    import re
    t = (text or "").lower().strip()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    # Strip common prefixes
    for prefix in ("show me your ", "show me ", "let me see your ", "let me see ",
                   "i want to see your ", "i want to see ", "can i see your ",
                   "can i see ", "wanna see your ", "wanna see "):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    return t.strip()[:200]


def save_requested_action(conn: sqlite3.Connection, girl_id: str,
                          action_text: str) -> dict[str, Any] | None:
    """Save a user-requested action. Dedups on normalized text per girl,
    incrementing the request count. Returns the row or None if empty."""
    gid = (girl_id or "").strip().lower()
    text = (action_text or "").strip()[:500]
    if not text or not gid:
        return None
    norm = _normalize_action(text)
    if not norm:
        return None
    row = conn.execute(
        "SELECT id, request_count FROM girl_requested_actions "
        "WHERE girl_id = ? AND normalized = ?",
        (gid, norm),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE girl_requested_actions SET request_count = request_count + 1, "
            "last_requested = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM girl_requested_actions WHERE id = ?", (row["id"],)
        ).fetchone())
    cur = conn.execute(
        "INSERT INTO girl_requested_actions (girl_id, action_text, normalized) "
        "VALUES (?, ?, ?)",
        (gid, text, norm),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM girl_requested_actions WHERE id = ?", (cur.lastrowid,)
    ).fetchone())


def get_requested_actions(conn: sqlite3.Connection, girl_id: str,
                          limit: int = 50) -> list[dict[str, Any]]:
    """List requested actions for a girl, most-requested first."""
    gid = (girl_id or "").strip().lower()
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM girl_requested_actions WHERE girl_id = ? "
            "ORDER BY request_count DESC, last_requested DESC LIMIT ?",
            (gid, limit),
        ).fetchall()
    ]


def match_requested_action(conn: sqlite3.Connection, girl_id: str,
                           user_text: str) -> dict[str, Any] | None:
    """Find a saved requested action matching the user's text."""
    norm = _normalize_action(user_text)
    if not norm:
        return None
    gid = (girl_id or "").strip().lower()
    # Exact normalized match first, then substring
    row = conn.execute(
        "SELECT * FROM girl_requested_actions "
        "WHERE girl_id = ? AND normalized = ? LIMIT 1",
        (gid, norm),
    ).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        "SELECT * FROM girl_requested_actions "
        "WHERE girl_id = ? AND (normalized LIKE ? OR ? LIKE '%' || normalized || '%') "
        "ORDER BY request_count DESC LIMIT 1",
        (gid, f"%{norm}%", norm),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Custom video requests (2026-10-07): users describe a custom video; the
# server attempts Sogni generation in the background. Blocked/failed
# generations stay `pending` for manual fulfillment.
# ---------------------------------------------------------------------------
def save_custom_request(conn: sqlite3.Connection, user_id: str,
                        girl_id: str, description: str) -> dict[str, Any]:
    """Save a custom video request. Returns the row."""
    desc = (description or "").strip()[:1000]
    if not desc:
        raise ValueError("description required")
    gid = (girl_id or "").strip().lower()
    if not gid:
        raise ValueError("girl_id required")
    cur = conn.execute(
        "INSERT INTO custom_video_requests (user_id, girl_id, description, status) "
        "VALUES (?, ?, ?, 'pending')",
        (user_id, gid, desc),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM custom_video_requests WHERE id = ?", (cur.lastrowid,)
    ).fetchone())


def get_custom_requests(conn: sqlite3.Connection, user_id: str | None = None,
                        status: str | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
    """List custom video requests, newest first. Filter by user and/or status."""
    query = "SELECT * FROM custom_video_requests"
    clauses: list[str] = []
    params: list[Any] = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def update_custom_request(conn: sqlite3.Connection, request_id: int,
                          status: str,
                          video_url: str | None = None,
                          error: str | None = None,
                          fallback_url: str | None = None) -> dict[str, Any] | None:
    """Update a custom video request's status (and optionally its video
    URL, its recorded failure/park reason, and the preset URL delivered
    as fallback). Terminal states since 2026-10-14: `fallback` (render
    definitively refused/failed, a preset from her own library was
    delivered instead) and `failed` (same, but no preset available)."""
    if status not in ("pending", "generating", "fulfilled", "manual",
                      "fallback", "failed"):
        raise ValueError(f"invalid status: {status}")
    sets = ["status = ?"]
    params: list[Any] = [status]
    if video_url is not None:
        sets.append("video_url = ?")
        params.append(video_url)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if fallback_url is not None:
        sets.append("fallback_url = ?")
        params.append(fallback_url)
    params.append(request_id)
    conn.execute(
        f"UPDATE custom_video_requests SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM custom_video_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Her Day feed (2026-10-07): Instagram-stories-style daily posts per girl.
# ---------------------------------------------------------------------------

def save_her_day_post(conn: "sqlite3.Connection", user_id: str, girl_id: str,
                      post_type: str = "image", media_url: str = "",
                      voice_url: str = "", caption: str = "") -> dict:
    """Save a Her Day post. Returns the row."""
    if post_type not in ("image", "video", "voice", "text"):
        raise ValueError(f"invalid post_type: {post_type}")
    gid = (girl_id or "").strip().lower()
    if not gid:
        raise ValueError("girl_id required")
    cur = conn.execute(
        "INSERT INTO her_day_posts (user_id, girl_id, post_type, media_url,"
        " voice_url, caption) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, gid, post_type, media_url or "", voice_url or "",
         (caption or "")[:500]),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM her_day_posts WHERE id = ?", (cur.lastrowid,)
    ).fetchone())


def get_her_day_posts(conn: "sqlite3.Connection", user_id: str,
                      girl_id: str | None = None,
                      include_expired: bool = False) -> list[dict]:
    """Get active Her Day posts for a user (optionally filtered by girl),
    newest first. Expired posts are excluded unless include_expired."""
    query = "SELECT * FROM her_day_posts WHERE user_id = ?"
    params: list = [user_id]
    if girl_id:
        query += " AND girl_id = ?"
        params.append(girl_id.strip().lower())
    if not include_expired:
        query += " AND expires_at > datetime('now')"
        # Staggered publishing: a post is invisible until its
        # created_at slot arrives.
        query += " AND created_at <= datetime('now')"
    query += " ORDER BY created_at DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def set_her_day_post_times(conn: "sqlite3.Connection", post_id: int,
                           created_at: str, expires_at: str) -> dict | None:
    """Set a Her Day post's publish slot (staggered daily posting).

    created_at doubles as the go-live time: readers only see posts whose
    created_at has arrived. expires_at moves with it so a slotted post
    still lives a full 24h from publication.
    """
    conn.execute(
        "UPDATE her_day_posts SET created_at = ?, expires_at = ?"
        " WHERE id = ?",
        (created_at, expires_at, post_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM her_day_posts WHERE id = ?", (post_id,)
    ).fetchone()
    return dict(row) if row else None


def mark_her_day_viewed(conn: "sqlite3.Connection", post_id: int,
                        user_id: str) -> bool:
    """Mark a Her Day post as viewed. Returns True if a row was updated."""
    cur = conn.execute(
        "UPDATE her_day_posts SET viewed = 1 WHERE id = ? AND user_id = ?",
        (post_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_her_day_post_media(conn: "sqlite3.Connection", post_id: int,
                           media_url: str | None = None,
                           voice_url: str | None = None) -> dict | None:
    """Attach existing media to an existing Her Day post (admin repair).

    Posts are otherwise insert-only (save_her_day_post); this is the one
    sanctioned in-place update, used to point a caption-only row at a
    file from the persistent /generated/ pool. Only the fields passed
    as not-None are written ("" clears a field). Returns the updated
    row, or None when no post has that id. URL validation (SFW, pool
    membership) is the caller's job — see her_day.validate_her_day_media_url.
    """
    row = conn.execute(
        "SELECT * FROM her_day_posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        return None
    sets: list[str] = []
    params: list = []
    if media_url is not None:
        sets.append("media_url = ?")
        params.append(media_url or "")
    if voice_url is not None:
        sets.append("voice_url = ?")
        params.append(voice_url or "")
    if sets:
        params.append(post_id)
        conn.execute(
            f"UPDATE her_day_posts SET {', '.join(sets)} WHERE id = ?",
            params)
        conn.commit()
    return dict(conn.execute(
        "SELECT * FROM her_day_posts WHERE id = ?", (post_id,)).fetchone())


def cleanup_expired_her_day(conn: "sqlite3.Connection") -> int:
    """Delete expired Her Day posts. Returns count deleted."""
    cur = conn.execute(
        "DELETE FROM her_day_posts WHERE expires_at <= datetime('now')")
    conn.commit()
    return cur.rowcount


def get_public_her_day_posts(conn: "sqlite3.Connection",
                             limit: int = 24) -> list[dict]:
    """Get recent Her Day posts across all users for the public landing page.

    Display rules (owner direction, 2026-10-17): the public feed shows
    only posts that carry a picture (non-empty media_url), and at most
    ONE post per character per day — the earliest pictured post of her
    day. Picture-less posts stay in the table (the per-user feed and
    the media backfill still use them); they just never surface here.

    Returns non-expired posts (no user_id exposed), newest first.
    Used by GET /api/v1/her-day/public and GET /today — no auth required.
    """
    rows = conn.execute(
        "SELECT girl_id, post_type, media_url, voice_url, caption,"
        " created_at FROM her_day_posts"
        " WHERE expires_at > datetime('now')"
        " AND created_at <= datetime('now')"
        " AND TRIM(COALESCE(media_url, '')) != ''"
        " ORDER BY created_at ASC, caption ASC",
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    kept: list[dict] = []
    for r in rows:
        d = dict(r)
        key = (d.get("girl_id") or "", (d.get("created_at") or "")[:10])
        if key in seen:
            continue
        seen.add(key)
        kept.append(d)
    kept.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return kept[:limit]


def post_slug(girl_id: str, created_at: str, caption: str) -> str:
    """Stable public slug for a Her Day moment: girl-date-hash6. The
    same moment fans out to many users' rows; the slug identifies the
    moment, not a row (2026-10-17, permalink pages)."""
    import hashlib
    day = (created_at or "")[:10].replace("-", "")
    h = hashlib.sha1((caption or "").encode("utf-8")).hexdigest()[:6]
    return f"{(girl_id or 'her').strip().lower()}-{day}-{h}"


def find_public_post_by_slug(conn: "sqlite3.Connection",
                             slug: str) -> dict | None:
    """Resolve a post slug to one visible public row, or None."""
    parts = (slug or "").rsplit("-", 2)
    if len(parts) != 3:
        return None
    girl_id, day, _h = parts
    if len(day) != 8 or not day.isdigit():
        return None
    day_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    rows = conn.execute(
        "SELECT girl_id, post_type, media_url, voice_url, caption,"
        " created_at FROM her_day_posts"
        " WHERE girl_id = ? AND date(created_at) = ?"
        " AND expires_at > datetime('now')"
        " AND created_at <= datetime('now')"
        " ORDER BY created_at DESC LIMIT 200",
        (girl_id.lower(), day_iso)).fetchall()
    for r in rows:
        d = dict(r)
        if post_slug(d["girl_id"], d["created_at"], d["caption"]) == slug:
            return d
    return None


# ---------------------------------------------------------------------------
# $500 subscriber drawing (2026-10-17): one entry per paying user, from
# the moment a payment actually credits (Stripe webhook or Stars/admin
# grant). Powers the public counter on the landing page.
# ---------------------------------------------------------------------------
DRAWING_GOAL = 100


def _ensure_drawing(conn: "sqlite3.Connection") -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS drawing_entries (
               user_id TEXT PRIMARY KEY,
               source TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def record_drawing_entry(conn: "sqlite3.Connection", user_id: str,
                         source: str) -> None:
    if not user_id:
        return
    _ensure_drawing(conn)
    conn.execute(
        "INSERT OR IGNORE INTO drawing_entries (user_id, source) "
        "VALUES (?, ?)", (user_id, source))
    conn.commit()


def drawing_status(conn: "sqlite3.Connection") -> dict[str, Any]:
    _ensure_drawing(conn)
    n = int(conn.execute(
        "SELECT COUNT(*) AS c FROM drawing_entries").fetchone()["c"])
    return {"entered": n, "goal": DRAWING_GOAL,
            "remaining": max(0, DRAWING_GOAL - n), "full": n >= DRAWING_GOAL}


# ---------------------------------------------------------------------------
# Relationship anniversaries (2026-10-17): days together per (user,
# character), counted from their first conversation turn.
# ---------------------------------------------------------------------------
ANNIVERSARY_MILESTONES = (7, 30, 100, 200, 365)


def relationship_start(conn: "sqlite3.Connection", user_id: str,
                       character_id: str) -> str | None:
    """Date (YYYY-MM-DD) of the pair's first turn, or None."""
    row = conn.execute(
        "SELECT MIN(date(created_at)) AS d FROM conversation "
        "WHERE user_id = ? AND character_id = ?",
        (user_id, character_id)).fetchone()
    return row["d"] if row else None


def anniversary_today(conn: "sqlite3.Connection", user_id: str,
                      character_id: str, today: str) -> int:
    """Milestone days-together if TODAY is an anniversary, else 0."""
    start = relationship_start(conn, user_id, character_id)
    if not start:
        return 0
    try:
        from datetime import date as _date
        y, mo, d = (int(x) for x in start.split("-"))
        t = _date.fromisoformat(today)
        days = (t - _date(y, mo, d)).days
    except Exception:
        return 0
    return days if days in ANNIVERSARY_MILESTONES else 0


def anniversaries_for_user(conn: "sqlite3.Connection", user_id: str,
                           today: str) -> dict[str, int]:
    """{character_id: milestone} for every pair celebrating today."""
    out: dict[str, int] = {}
    rows = conn.execute(
        "SELECT character_id, MIN(date(created_at)) AS d FROM conversation "
        "WHERE user_id = ? GROUP BY character_id", (user_id,)).fetchall()
    for r in rows:
        if not r["d"]:
            continue
        try:
            from datetime import date as _date
            y, mo, d = (int(x) for x in str(r["d"]).split("-"))
            days = (_date.fromisoformat(today) - _date(y, mo, d)).days
        except Exception:
            continue
        if days in ANNIVERSARY_MILESTONES:
            out[r["character_id"]] = days
    return out


# ---------------------------------------------------------------------------
# Creator economy (2026-10-17): when someone chats with a gallery-public
# companion you created, you earn sparks — one per unique chatter per
# day (anti-farm). 25 sparks redeem for +100 messages.
# ---------------------------------------------------------------------------
SPARK_REDEEM_COST = 25
SPARK_REDEEM_MESSAGES = 100


def _ensure_creator(conn: "sqlite3.Connection") -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS creator_sparks (
               creator_user_id TEXT PRIMARY KEY,
               sparks INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS creator_daily (
               creator_user_id TEXT NOT NULL,
               girl_id TEXT NOT NULL,
               chatter_user_id TEXT NOT NULL,
               day TEXT NOT NULL,
               PRIMARY KEY (creator_user_id, girl_id, chatter_user_id,
                            day))""")


def accrue_creator_spark(conn: "sqlite3.Connection", creator_user_id: str,
                         girl_id: str, chatter_user_id: str,
                         day: str) -> bool:
    """Record a fan-day; returns True when a NEW spark was earned."""
    if not creator_user_id or creator_user_id == chatter_user_id:
        return False
    _ensure_creator(conn)
    cur = conn.execute(
        "INSERT OR IGNORE INTO creator_daily (creator_user_id, girl_id,"
        " chatter_user_id, day) VALUES (?, ?, ?, ?)",
        (creator_user_id, girl_id, chatter_user_id, day))
    if cur.rowcount:
        conn.execute(
            "INSERT INTO creator_sparks (creator_user_id, sparks) "
            "VALUES (?, 1) ON CONFLICT(creator_user_id) DO UPDATE SET "
            "sparks = sparks + 1, updated_at = datetime('now')",
            (creator_user_id,))
        conn.commit()
        return True
    return False


def creator_stats(conn: "sqlite3.Connection",
                  user_id: str) -> dict[str, Any]:
    _ensure_creator(conn)
    row = conn.execute(
        "SELECT sparks FROM creator_sparks WHERE creator_user_id = ?",
        (user_id,)).fetchone()
    girls = conn.execute(
        "SELECT lc.id, lc.name, lc.gallery_public, "
        "(SELECT COUNT(DISTINCT cd.chatter_user_id) FROM creator_daily cd "
        " WHERE cd.girl_id = lc.id AND cd.day >= date('now', '-7 days')) "
        " AS fans_7d FROM local_characters lc "
        "WHERE lc.owner_user_id = ? ORDER BY lc.name", (user_id,)).fetchall()
    return {
        "sparks": int(row["sparks"]) if row else 0,
        "redeem_cost": SPARK_REDEEM_COST,
        "redeem_messages": SPARK_REDEEM_MESSAGES,
        "girls": [{"id": g["id"], "name": g["name"],
                   "gallery_public": bool(g["gallery_public"]),
                   "fans_7d": int(g["fans_7d"] or 0)} for g in girls],
    }


def redeem_sparks(conn: "sqlite3.Connection", user_id: str) -> bool:
    _ensure_creator(conn)
    row = conn.execute(
        "SELECT sparks FROM creator_sparks WHERE creator_user_id = ?",
        (user_id,)).fetchone()
    if not row or int(row["sparks"]) < SPARK_REDEEM_COST:
        return False
    conn.execute(
        "UPDATE creator_sparks SET sparks = sparks - ?, "
        "updated_at = datetime('now') WHERE creator_user_id = ?",
        (SPARK_REDEEM_COST, user_id))
    conn.execute(
        "UPDATE users SET messages_remaining = messages_remaining + ?, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (SPARK_REDEEM_MESSAGES, user_id))
    conn.commit()
    return True


def creator_leaderboard(conn: "sqlite3.Connection",
                        limit: int = 10) -> list[dict[str, Any]]:
    """Top gallery girls by unique fans in the last 7 days."""
    _ensure_creator(conn)
    rows = conn.execute(
        "SELECT cd.girl_id, lc.name, lc.avatar_url, u.name AS creator_name, "
        "COUNT(DISTINCT cd.chatter_user_id) AS fans "
        "FROM creator_daily cd "
        "JOIN local_characters lc ON lc.id = cd.girl_id "
        "LEFT JOIN users u ON u.user_id = cd.creator_user_id "
        "WHERE cd.day >= date('now', '-7 days') AND lc.gallery_public = 1 "
        "GROUP BY cd.girl_id ORDER BY fans DESC LIMIT ?",
        (limit,)).fetchall()
    return [{"girl_id": r["girl_id"], "name": r["name"],
             "avatar_url": r["avatar_url"] or "",
             "creator_name": r["creator_name"] or None,
             "fans_7d": int(r["fans"])} for r in rows]


# ---------------------------------------------------------------------------
# Gifts (2026-10-17): he can give her things; they persist — written to
# her memory, woven into her feed captions, and visual gifts (necklace,
# roses) appear in her generated photos for 30 days. Web rail: message
# balance. Telegram rail: Stars via the admin grant endpoint.
# ---------------------------------------------------------------------------
GIFTS: dict[str, dict[str, Any]] = {
    "coffee": {"name": "Coffee", "emoji": "☕", "msg_cost": 25,
               "stars": 25, "visual": "",
               "memory": "He bought her a coffee — she says it is the "
                         "little things that get her."},
    "chocolates": {"name": "Chocolates", "emoji": "🍫", "msg_cost": 40,
                   "stars": 40, "visual": "",
                   "memory": "He sent her chocolates — she is sharing "
                             "them with exactly no one."},
    "flowers": {"name": "Roses", "emoji": "🌹", "msg_cost": 50,
                "stars": 50,
                "visual": "holding a bouquet of red roses he gave her",
                "memory": "He sent her roses — they are by her bed and "
                          "she keeps looking at them."},
    "perfume": {"name": "Perfume", "emoji": "🌸", "msg_cost": 75,
                "stars": 75, "visual": "",
                "memory": "He gave her perfume — she wears it when she "
                          "wants to feel close to him."},
    "necklace": {"name": "Gold necklace", "emoji": "📿", "msg_cost": 150,
                 "stars": 150,
                 "visual": "wearing a delicate gold necklace he gave her",
                 "memory": "He gave her a gold necklace — she has not "
                           "taken it off since."},
}

GIFT_REACTIONS: dict[str, str] = {
    "coffee": "you bought me coffee?? ☕ okay that is actually the "
              "sweetest little thing. thank you 💜",
    "chocolates": "chocolates 🍫 you know the way to my heart. sharing "
                  "them with exactly no one 😌💜",
    "flowers": "roses?! 🌹 no one sends me flowers… they are going right "
               "by my bed. thank you 💜",
    "perfume": "perfume?? 🌸 I am wearing it tonight — think of me 💜",
    "necklace": "a necklace?? 📿 I am not taking it off. ever. look for "
                "it in my next pic 💜",
}


def _ensure_gifts(conn: "sqlite3.Connection") -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gifts (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               girl_id TEXT NOT NULL,
               gift_key TEXT NOT NULL,
               source TEXT NOT NULL DEFAULT 'web',
               created_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def give_gift(conn: "sqlite3.Connection", user_id: str, girl_id: str,
              gift_key: str, source: str = "web") -> dict[str, Any] | None:
    g = GIFTS.get(gift_key)
    if not g:
        return None
    _ensure_gifts(conn)
    conn.execute(
        "INSERT INTO gifts (user_id, girl_id, gift_key, source) "
        "VALUES (?, ?, ?, ?)", (user_id, girl_id, gift_key, source))
    conn.commit()
    try:
        write_memory(conn, user_id, "fact", g["memory"],
                     character_id=girl_id)
    except Exception:
        pass
    return {"gift_key": gift_key, **g,
            "reaction": GIFT_REACTIONS.get(gift_key, "thank you 💜")}


def recent_gift(conn: "sqlite3.Connection", user_id: str, girl_id: str,
                days: int = 30) -> dict[str, Any] | None:
    _ensure_gifts(conn)
    row = conn.execute(
        "SELECT gift_key, created_at FROM gifts WHERE user_id = ? "
        "AND girl_id = ? AND created_at >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT 1",
        (user_id, girl_id, f"-{int(days)} days")).fetchone()
    if not row:
        return None
    g = GIFTS.get(row["gift_key"])
    if not g:
        return None
    return {"gift_key": row["gift_key"], "created_at": row["created_at"],
            **g}


def gift_visual_fragment(conn: "sqlite3.Connection", user_id: str,
                         girl_id: str) -> str:
    """The visual of her most recent VISUAL gift in 30 days — a coffee
    must not unwear the necklace he gave her last week."""
    _ensure_gifts(conn)
    rows = conn.execute(
        "SELECT gift_key FROM gifts WHERE user_id = ? AND girl_id = ? "
        "AND created_at >= datetime('now', '-30 days') "
        "ORDER BY id DESC LIMIT 25", (user_id, girl_id)).fetchall()
    for r in rows:
        v = (GIFTS.get(r["gift_key"]) or {}).get("visual", "")
        if v:
            return v
    return ""


def her_day_exists_today(conn: "sqlite3.Connection", user_id: str,
                         girl_id: str) -> bool:
    """Check if a girl already has Her Day posts generated today for a user."""
    row = conn.execute(
        "SELECT 1 FROM her_day_posts WHERE user_id = ? AND girl_id = ?"
        " AND date(created_at) = date('now') LIMIT 1",
        (user_id, girl_id.strip().lower()),
    ).fetchone()
    return row is not None


def get_random_give_media(conn: "sqlite3.Connection", girl_id: str,
                          kind: str = "give") -> dict | None:
    """Get a random media item from a girl's Give library for Her Day reuse."""
    row = conn.execute(
        "SELECT * FROM girl_media WHERE girl_id = ? AND kind = ?"
        " ORDER BY RANDOM() LIMIT 1",
        (girl_id.strip().lower(), kind),
    ).fetchone()
    return dict(row) if row else None


# ---------- Living World persistence (Phase A1, 2026-10-18) ----------

def _ensure_world(conn: "sqlite3.Connection") -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS world_state (
               girl_id TEXT NOT NULL,
               date TEXT NOT NULL,
               mood TEXT NOT NULL,
               plan_json TEXT NOT NULL,
               PRIMARY KEY (girl_id, date))""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS world_events (
               date TEXT NOT NULL,
               slot INTEGER NOT NULL,
               girl_a TEXT NOT NULL,
               girl_b TEXT NOT NULL,
               kind TEXT NOT NULL,
               activity TEXT NOT NULL DEFAULT '',
               place TEXT NOT NULL,
               block_idx INTEGER NOT NULL,
               text TEXT NOT NULL,
               PRIMARY KEY (date, slot))""")


def save_world_day(conn: "sqlite3.Connection", girl_id: str,
                   date_str: str, plan: dict) -> None:
    import json as _json
    _ensure_world(conn)
    conn.execute(
        "INSERT OR IGNORE INTO world_state (girl_id, date, mood, "
        "plan_json) VALUES (?, ?, ?, ?)",
        (girl_id, date_str, plan.get("mood", ""),
         _json.dumps({"blocks": plan.get("blocks", [])})))
    conn.commit()


def get_world_day(conn: "sqlite3.Connection", girl_id: str,
                  date_str: str) -> dict | None:
    import json as _json
    _ensure_world(conn)
    row = conn.execute(
        "SELECT mood, plan_json FROM world_state WHERE girl_id = ? "
        "AND date = ?", (girl_id, date_str)).fetchone()
    if not row:
        return None
    try:
        blocks = _json.loads(row[1]).get("blocks", [])
    except Exception:
        blocks = []
    return {"girl_id": girl_id, "date": date_str, "mood": row[0],
            "blocks": [tuple(b) for b in blocks]}


def save_world_events(conn: "sqlite3.Connection", date_str: str,
                      events: list) -> None:
    _ensure_world(conn)
    for ev in events:
        conn.execute(
            "INSERT OR IGNORE INTO world_events (date, slot, girl_a, "
            "girl_b, kind, activity, place, block_idx, text) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, ev["slot"], ev["girl_a"], ev["girl_b"],
             ev["kind"], ev.get("activity", ""), ev["place"],
             ev["block_idx"], ev["text"]))
    conn.commit()


def get_world_events(conn: "sqlite3.Connection",
                     date_str: str) -> list | None:
    _ensure_world(conn)
    rows = conn.execute(
        "SELECT slot, girl_a, girl_b, kind, activity, place, "
        "block_idx, text FROM world_events WHERE date = ? "
        "ORDER BY slot",
        (date_str,)).fetchall()
    if not rows:
        return None
    return [{"slot": r[0], "girl_a": r[1], "girl_b": r[2], "kind": r[3],
             "activity": r[4], "place": r[5], "block_idx": r[6],
             "text": r[7]} for r in rows]


# ---------- Public IP packages (Phase B, 2026-10-18) ----------

def _ensure_public_packages(conn: "sqlite3.Connection") -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS public_packages (
               girl_id TEXT NOT NULL,
               date TEXT NOT NULL,
               package_json TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'pending',
               created_at TEXT NOT NULL DEFAULT (datetime('now')),
               updated_at TEXT NOT NULL DEFAULT (datetime('now')),
               PRIMARY KEY (girl_id, date))""")


def save_public_package(conn: "sqlite3.Connection", girl_id: str,
                        date_str: str, pkg: dict) -> None:
    import json as _json
    _ensure_public_packages(conn)
    conn.execute(
        "INSERT INTO public_packages (girl_id, date, package_json) "
        "VALUES (?, ?, ?) ON CONFLICT(girl_id, date) DO UPDATE SET "
        "package_json = excluded.package_json, "
        "updated_at = datetime('now')",
        (girl_id, date_str, _json.dumps(pkg)))
    conn.commit()


def get_public_package(conn: "sqlite3.Connection", girl_id: str,
                       date_str: str) -> dict | None:
    import json as _json
    _ensure_public_packages(conn)
    row = conn.execute(
        "SELECT package_json, status FROM public_packages WHERE "
        "girl_id = ? AND date = ?", (girl_id, date_str)).fetchone()
    if not row:
        return None
    try:
        pkg = _json.loads(row[0])
    except Exception:
        pkg = {}
    return {"girl_id": girl_id, "date": date_str, "status": row[1],
            "package": pkg}


def list_public_packages(conn: "sqlite3.Connection",
                         date_str: str) -> list:
    _ensure_public_packages(conn)
    rows = conn.execute(
        "SELECT girl_id FROM public_packages WHERE date = ? "
        "ORDER BY girl_id", (date_str,)).fetchall()
    return [p for p in (get_public_package(conn, r[0], date_str)
                        for r in rows) if p]


def set_public_package_status(conn: "sqlite3.Connection", girl_id: str,
                              date_str: str, status: str) -> bool:
    _ensure_public_packages(conn)
    cur = conn.execute(
        "UPDATE public_packages SET status = ?, "
        "updated_at = datetime('now') WHERE girl_id = ? AND date = ?",
        (status, girl_id, date_str))
    conn.commit()
    return cur.rowcount > 0

def give_stage_cap(user_id: str | None) -> int:
    """Stage ceiling for give serving. Group/event traffic runs on
    synthetic shared accounts (tg_<id> with id >= 900_000_000_000,
    minted by the Telegram bot for group live events and the group
    /give command); in a public group only stages 1-3 (tease-level)
    may be served — the full 1-9 escalation is for private chats
    only. (Owner rule, 2026-10-18.)"""
    uid = (user_id or "").strip()
    if uid.startswith("tg_"):
        try:
            if int(uid[3:]) >= 900_000_000_000:
                return 3
        except ValueError:
            pass
    return 9
